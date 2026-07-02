# -*- coding: utf-8 -*-
"""
tests/test_store.py — AccountStore 单元测试。

运行：python -m unittest tests.test_store -v
覆盖：去重/upsert 补全(⑦)、原子取号、单/双实例并发(①)、状态标记、关联查询、
      stats 含 free(③)、cookie 幂等(②)、单例、兼容层、导入幂等(②)+graph_tokens(④)。
"""

import os
import tempfile
import unittest

from common.store import AccountStore


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.store = AccountStore(self.db_path)

    def tearDown(self):
        self.store.close()
        # Windows 下需先关连接再删文件
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


class TestAddAndList(StoreTestBase):
    def test_add_email_returns_id(self):
        eid = self.store.add_email("Foo@Bar.com", "pw", "tok", "cid", source="import")
        self.assertIsInstance(eid, int)
        self.assertGreater(eid, 0)

    def test_add_email_normalizes_lowercase(self):
        self.store.add_email("Foo@Bar.com", "pw")
        rows = self.store.list_emails()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "foo@bar.com")

    def test_add_email_dedupes(self):
        self.store.add_email("a@b.com", "pw1")
        self.store.add_email("A@B.com", "pw2")  # 同邮箱不同大小写
        rows = self.store.list_emails()
        self.assertEqual(len(rows), 1)

    def test_add_email_same_id_on_conflict(self):
        id1 = self.store.add_email("a@b.com", "pw1")
        id2 = self.store.add_email("A@B.com", "pw2")
        self.assertEqual(id1, id2)

    def test_add_email_upsert_fills_empty_fields(self):
        # ⑦：先空 token/client_id，后补全
        self.store.add_email("a@b.com", "pw")
        self.store.add_email("a@b.com", "pw", "tok", "cid")
        r = self.store._conn.execute(
            "SELECT refresh_token, client_id FROM emails WHERE email='a@b.com'"
        ).fetchone()
        self.assertEqual(r["refresh_token"], "tok")
        self.assertEqual(r["client_id"], "cid")

    def test_add_email_upsert_keeps_existing_nonempty(self):
        # ⑦：已有非空值不被覆盖
        self.store.add_email("a@b.com", "pw", "tok1")
        self.store.add_email("a@b.com", "pwX", "tok2")
        r = self.store._conn.execute(
            "SELECT password, refresh_token FROM emails WHERE email='a@b.com'"
        ).fetchone()
        self.assertEqual(r["password"], "pw")
        self.assertEqual(r["refresh_token"], "tok1")

    def test_list_emails_empty(self):
        self.assertEqual(self.store.list_emails(), [])


class TestNextEmail(StoreTestBase):
    def test_next_email_returns_tuple(self):
        self.store.add_email("a@b.com", "pw", "tok", "cid")
        got = self.store.next_email("claude")
        self.assertEqual(got, ("a@b.com", "pw", "tok", "cid"))

    def test_next_email_none_when_empty(self):
        self.assertIsNone(self.store.next_email("claude"))

    def test_next_email_skips_used_same_platform(self):
        self.store.add_email("a@b.com", "pw")
        self.store.next_email("claude")          # 预留 a
        self.assertIsNone(self.store.next_email("claude"))  # 无其它可用

    def test_next_email_independent_across_platforms(self):
        self.store.add_email("a@b.com", "pw")
        self.assertIsNotNone(self.store.next_email("claude"))
        self.assertIsNotNone(self.store.next_email("github"))  # 另一平台仍可用

    def test_mark_used_sets_ok(self):
        self.store.add_email("a@b.com", "pw")
        self.store.next_email("claude")
        self.store.mark_used("claude", "a@b.com", "pw")
        usages = self.store.email_usages("a@b.com")
        self.assertEqual(usages[0]["platform"], "claude")
        self.assertEqual(usages[0]["status"], "ok")

    def test_mark_error_sets_error_with_reason(self):
        self.store.add_email("a@b.com", "pw")
        self.store.next_email("claude")
        self.store.mark_error("claude", "a@b.com", "pw", reason="captcha")
        usages = self.store.email_usages("a@b.com")
        self.assertEqual(usages[0]["status"], "error")
        self.assertEqual(usages[0]["reason"], "captcha")

    def test_concurrent_next_email_no_duplicate(self):
        import threading as _t
        for i in range(20):
            self.store.add_email(f"u{i}@b.com", "pw")
        got = []
        got_lock = _t.Lock()

        def worker():
            r = self.store.next_email("claude")
            if r:
                with got_lock:
                    got.append(r[0])

        threads = [_t.Thread(target=worker) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(got), 20)          # 恰好 20 个被取走，不超发
        self.assertEqual(len(set(got)), 20)     # 无重复

    def test_concurrent_next_email_across_store_instances_no_crash(self):
        # ①：两个独立 AccountStore 实例（各自连接）并发取号，不崩不重号
        import threading as _t
        self.store.add_email("a@b.com", "pw")
        s1 = AccountStore(self.db_path)
        s2 = AccountStore(self.db_path)
        errors = []
        got = []
        lock = _t.Lock()

        def worker(store):
            try:
                r = store.next_email("claude")
                with lock:
                    got.append(r[0] if r else None)
            except Exception as e:
                with lock:
                    errors.append(type(e).__name__)

        threads = [_t.Thread(target=worker, args=(s,)) for s in (s1, s2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        s1.close()
        s2.close()
        self.assertEqual(errors, [])
        self.assertEqual([x for x in got if x], ["a@b.com"])  # 只有一个拿到
        self.assertEqual(got.count(None), 1)                  # 另一个拿到 None


class TestStatsAndCookie(StoreTestBase):
    def test_stats_counts_by_platform_status(self):
        for i in range(3):
            self.store.add_email(f"u{i}@b.com", "pw")
        self.store.next_email("claude")                  # 1 reserved
        self.store.mark_used("claude", "u0@b.com")       # 变 ok（同一邮箱）
        stats = self.store.stats()
        self.assertEqual(stats["total_emails"], 3)
        self.assertIn("claude", stats["platforms"])
        self.assertEqual(stats["platforms"]["claude"]["ok"], 1)

    def test_stats_includes_free_per_platform(self):
        # ③：3 邮箱、claude 取号 1 → free==2
        for i in range(3):
            self.store.add_email(f"u{i}@b.com", "pw")
        self.store.next_email("claude")
        stats = self.store.stats()
        self.assertEqual(stats["platforms"]["claude"]["free"], 2)

    def test_save_and_get_cookie(self):
        self.store.add_email("a@b.com", "pw")
        self.store.save_cookie("github", '{"c":1}', email="a@b.com")
        self.assertEqual(self.store.get_cookie("github", email="a@b.com"), '{"c":1}')

    def test_save_cookie_upsert_overwrites(self):
        self.store.add_email("a@b.com", "pw")
        self.store.save_cookie("github", "v1", email="a@b.com")
        self.store.save_cookie("github", "v2", email="a@b.com")
        self.assertEqual(self.store.get_cookie("github", email="a@b.com"), "v2")

    def test_save_cookie_without_email_upsert_overwrites(self):
        # ②：无邮箱 cookie 幂等——两次保存只留一条
        self.store.save_cookie("github", "v1")
        self.store.save_cookie("github", "v2")
        self.assertEqual(self.store.get_cookie("github"), "v2")
        rows = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM cookies "
            "WHERE platform='github' AND email_id IS NULL"
        ).fetchone()
        self.assertEqual(rows["c"], 1)

    def test_get_cookie_missing_returns_none(self):
        self.assertIsNone(self.store.get_cookie("github", email="x@y.com"))


class TestSingleton(unittest.TestCase):
    def test_get_store_returns_same_instance(self):
        from common import store as store_mod
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "s.db")
        store_mod._SINGLETON = None      # 重置
        a = store_mod.get_store(db)
        b = store_mod.get_store()
        self.assertIs(a, b)
        a.close()
        store_mod._SINGLETON = None


class TestEmailsCompat(unittest.TestCase):
    """common/emails.py 兼容层委托 store。"""

    def setUp(self):
        from common import store as store_mod
        self.tmp = tempfile.mkdtemp()
        db = os.path.join(self.tmp, "compat.db")
        store_mod._SINGLETON = None
        self.store = store_mod.get_store(db)

    def tearDown(self):
        from common import store as store_mod
        self.store.close()
        store_mod._SINGLETON = None

    def test_compat_next_email_and_mark(self):
        import common.emails as emails
        self.store.add_email("a@b.com", "pw", "tok", "cid")
        got = emails.next_email("claude")
        self.assertEqual(got, ("a@b.com", "pw", "tok", "cid"))
        emails.mark_used("claude", "a@b.com", "pw")
        usages = self.store.email_usages("a@b.com")
        self.assertEqual(usages[0]["status"], "ok")

    def test_compat_mark_error(self):
        import common.emails as emails
        self.store.add_email("c@d.com", "pw")
        emails.next_email("github")
        emails.mark_error("github", "c@d.com", "pw", reason="boom")
        usages = self.store.email_usages("c@d.com")
        self.assertEqual(usages[0]["status"], "error")
        self.assertEqual(usages[0]["reason"], "boom")


class TestImport(unittest.TestCase):
    """导入工具：幂等(②) + graph_tokens 补 token(④) + register 通用池→claude(⑨)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "imp.db")

    def _write(self, relname, content):
        p = os.path.join(self.tmp, relname)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(relname) else None
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_import_full_and_idempotent(self):
        import json
        from tools.import_to_store import import_all
        from common.store import AccountStore

        self._write("emails.txt",
                    "a@b.com----pw1----tok1----cid1\n"
                    "c@d.com----pw2\n")
        # ④：accounts 无 token，graph_tokens 提供 token
        self._write(os.path.join("outlook_accounts", "accounts_X.txt"),
                    "e@f.com----pw3\n")
        self._write(os.path.join("outlook_accounts", "graph_tokens_X.json"),
                    json.dumps([{"email": "e@f.com", "password": "pw3",
                                 "refresh_token": "tok3"}]))
        self._write("emails_used_claude.txt", "a@b.com----pw1----ok\n")
        self._write("emails_error_github.txt", "c@d.com----pw2----captcha\n")
        # ⑨：register.py 通用池（2 字段成功行）-> claude ok
        self._write("emails_used.txt", "e@f.com----pw3\n")
        self._write(os.path.join("cookies", "github", "full_x.json"), '{"c":1}')

        r1 = import_all(root=self.tmp, db_path=self.db)
        self.assertIn("emails", r1)

        s = AccountStore(self.db)
        try:
            self.assertEqual(len(s.list_emails()), 3)               # a,c,e 去重
            self.assertEqual(s.email_usages("a@b.com")[0]["status"], "ok")
            uc = s.email_usages("c@d.com")[0]
            self.assertEqual(uc["status"], "error")
            self.assertEqual(uc["reason"], "captcha")
            self.assertEqual(s.email_usages("e@f.com")[0]["status"], "ok")   # claude ok (⑨)
            self.assertEqual(s.email_usages("e@f.com")[0]["platform"], "claude")
            # ④：e@f.com 的 refresh_token 由 graph_tokens 补上
            row = s._conn.execute(
                "SELECT refresh_token FROM emails WHERE email='e@f.com'").fetchone()
            self.assertEqual(row["refresh_token"], "tok3")
        finally:
            s.close()

        # 幂等：再次导入，邮箱数不变、无邮箱 cookie 仍只 1 条(②)
        import_all(root=self.tmp, db_path=self.db)
        s2 = AccountStore(self.db)
        try:
            self.assertEqual(len(s2.list_emails()), 3)
            cnt = s2._conn.execute(
                "SELECT COUNT(*) AS c FROM cookies "
                "WHERE platform='github' AND email_id IS NULL").fetchone()["c"]
            self.assertEqual(cnt, 1)
        finally:
            s2.close()


class TestMarkRegistered(unittest.TestCase):
    """mark_registered 便捷助手测试。"""

    def setUp(self):
        from common import store as store_mod
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "mark.db")
        store_mod._SINGLETON = None
        self.store = store_mod.get_store(self.db)

    def tearDown(self):
        from common import store as store_mod
        self.store.close()
        store_mod._SINGLETON = None
        for suffix in ("", "-wal", "-shm"):
            p = self.db + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def test_mark_registered_adds_email_and_marks_usage(self):
        """登记后邮箱入库、平台标记为 ok。"""
        from common.store import mark_registered
        mark_registered("github", "test@outlook.com", password="pwd123", source="github_reg")

        # 验证邮箱已入库
        emails = self.store.list_emails()
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0]["email"], "test@outlook.com")
        self.assertEqual(emails[0]["password"], "pwd123")
        self.assertEqual(emails[0]["source"], "github_reg")

        # 验证 usage 标记为 ok
        usages = self.store.email_usages("test@outlook.com")
        self.assertEqual(len(usages), 1)
        self.assertEqual(usages[0]["platform"], "github")
        self.assertEqual(usages[0]["status"], "ok")

    def test_mark_registered_saves_cookie_dict(self):
        """提供 dict 格式 cookie，能正确保存和读取。"""
        from common.store import mark_registered
        cookie_data = {"key1": "value1", "key2": "value2"}
        mark_registered("github", "test@outlook.com", cookie_payload=cookie_data)

        # 验证 cookie 已保存
        saved = self.store.get_cookie("github", email="test@outlook.com")
        self.assertIsNotNone(saved)
        import json
        parsed = json.loads(saved)
        self.assertEqual(parsed, cookie_data)

    def test_mark_registered_saves_cookie_string(self):
        """提供 JSON 字符串格式 cookie，能正确保存。"""
        from common.store import mark_registered
        cookie_str = '{"token": "abc123"}'
        mark_registered("chatgpt", "user@test.com", cookie_payload=cookie_str)

        saved = self.store.get_cookie("chatgpt", email="user@test.com")
        self.assertEqual(saved, cookie_str)

    def test_mark_registered_idempotent(self):
        """重复调用幂等，不重复插入。"""
        from common.store import mark_registered
        mark_registered("github", "dup@test.com", password="pwd1")
        mark_registered("github", "dup@test.com", password="pwd2")

        emails = self.store.list_emails()
        self.assertEqual(len(emails), 1)
        # 密码应保留第一次的值（add_email 的 upsert 逻辑）
        self.assertEqual(emails[0]["password"], "pwd1")

    def test_mark_registered_no_cookie(self):
        """不提供 cookie_payload 时，只登记邮箱和 usage。"""
        from common.store import mark_registered
        mark_registered("grok", "nocookie@test.com", password="pass")

        # 验证邮箱和 usage
        emails = self.store.list_emails()
        self.assertEqual(len(emails), 1)
        usages = self.store.email_usages("nocookie@test.com")
        self.assertEqual(usages[0]["platform"], "grok")

        # 验证没有 cookie
        cookie = self.store.get_cookie("grok", email="nocookie@test.com")
        self.assertIsNone(cookie)


if __name__ == "__main__":
    unittest.main()
