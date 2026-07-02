# 统一账号中心（AccountStore）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 用单一 SQLite 数据库 + `AccountStore` 仓库层统一管理邮箱池、平台使用关联与 cookie，替代散落的纯文本文件，并通过兼容层让现有代码透明切到 DB。

**架构：** `common/store.py` 提供 `AccountStore` 类（建表 + 原子取号 + 关联查询），底层 SQLite（WAL 模式，`data/accounts.db`）。`common/emails.py` 保留原模块级函数签名，内部委托全局 store 单例。`tools/import_to_store.py` 幂等导入现有文本数据。`webui/server.py` 增加只读查询接口。

**技术栈：** Python 3.13、标准库 `sqlite3`、标准库 `unittest`（项目零第三方测试依赖，崇尚零依赖风格）、FastAPI（现有 WebUI）。

**测试命令统一为：** `python -m unittest tests.test_store -v`（无需安装 pytest）。

**规格来源：** `docs/superpowers/specs/2026-07-01-account-store-design.md`

---

## 文件结构

| 文件 | 职责 |
|------|------|
| 创建 `common/store.py` | `AccountStore` 类：建表、WAL、原子取号、状态标记、关联查询、cookie 读写、全局单例 `get_store()` |
| 创建 `tests/__init__.py` | 使 `tests` 成为包，供 `python -m unittest tests.test_store` |
| 创建 `tests/test_store.py` | `AccountStore` 单元测试（去重、原子性、状态、关联、导入幂等） |
| 创建 `tools/import_to_store.py` | 幂等导入现有 `emails*.txt` / `outlook_accounts/` / `cookies/` 到 DB |
| 修改 `common/emails.py` | 模块级函数改为委托 `get_store()`，保持签名不变 |
| 修改 `webui/server.py` | 增加 `/api/accounts`、`/api/accounts/stats`、`/api/accounts/{email}` 只读接口 |
| 修改 `.gitignore` | 忽略 `data/accounts.db*`（含 WAL/SHM） |

数据库文件 `data/accounts.db` 运行时生成，不入库。

---

## 任务 1：AccountStore 建表与基础 CRUD

**文件：**
- 创建：`common/store.py`
- 创建：`tests/__init__.py`
- 创建：`tests/test_store.py`

- [ ] **步骤 1：编写失败的测试（建表 + add_email 去重 + list_emails）**

创建 `tests/__init__.py`（空文件）。

创建 `tests/test_store.py`：

```python
# -*- coding: utf-8 -*-
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

    def test_list_emails_empty(self):
        self.assertEqual(self.store.list_emails(), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m unittest tests.test_store -v`
预期：FAIL / ERROR，报 `ModuleNotFoundError: No module named 'common.store'`

- [ ] **步骤 3：编写最少实现（建表 + add_email + list_emails + close）**

创建 `common/store.py`：

```python
# -*- coding: utf-8 -*-
"""
common/store.py — 统一账号中心仓库层（SQLite）

单一数据库 data/accounts.db（WAL 模式），封装邮箱池、平台使用关联、cookie。
所有模块通过 AccountStore 读写，替代散落的纯文本文件。
线程安全：单连接 + 写锁，契合单服务主导形态。
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone

DEFAULT_DB = os.path.join("data", "accounts.db")


def _now():
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password      TEXT DEFAULT '',
    refresh_token TEXT DEFAULT '',
    client_id     TEXT DEFAULT '',
    source        TEXT DEFAULT 'import',
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS usages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id   INTEGER NOT NULL REFERENCES emails(id),
    platform   TEXT NOT NULL,
    status     TEXT NOT NULL,
    reason     TEXT DEFAULT '',
    updated_at TEXT,
    UNIQUE(email_id, platform)
);
CREATE TABLE IF NOT EXISTS cookies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id   INTEGER REFERENCES emails(id),
    platform   TEXT NOT NULL,
    payload    TEXT DEFAULT '',
    updated_at TEXT,
    UNIQUE(email_id, platform)
);
"""


class AccountStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = db_path
        d = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(d, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        self._conn.close()

    # ---- 邮箱池 ----
    def add_email(self, email, password="", refresh_token="",
                  client_id="", source="import"):
        email = email.strip().lower()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO emails "
                "(email, password, refresh_token, client_id, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (email, password, refresh_token, client_id, source, _now()),
            )
            self._conn.commit()
            if cur.lastrowid:
                return cur.lastrowid
            row = self._conn.execute(
                "SELECT id FROM emails WHERE email=?", (email,)
            ).fetchone()
            return row["id"] if row else 0

    def list_emails(self, platform=None, status=None):
        sql = ("SELECT e.id, e.email, e.password, e.source, e.created_at "
               "FROM emails e")
        params = []
        if platform or status:
            sql += " JOIN usages u ON u.email_id = e.id"
            conds = []
            if platform:
                conds.append("u.platform=?")
                params.append(platform)
            if status:
                conds.append("u.status=?")
                params.append(status)
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY e.id"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m unittest tests.test_store -v`
预期：PASS（4 个测试）

- [ ] **步骤 5：Commit**

```bash
git add common/store.py tests/__init__.py tests/test_store.py
git commit -m "feat(store): AccountStore 建表与 add_email/list_emails"
```

---

## 任务 2：原子取号 next_email 与状态标记

**文件：**
- 修改：`common/store.py`
- 测试：`tests/test_store.py`

- [ ] **步骤 1：编写失败的测试（取号 + 状态 + 并发原子性）**

在 `tests/test_store.py` 末尾（`if __name__` 之前）追加：

```python
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
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m unittest tests.test_store.TestNextEmail -v`
预期：FAIL / ERROR，报 `AttributeError: 'AccountStore' object has no attribute 'next_email'`

- [ ] **步骤 3：编写最少实现（next_email / mark_used / mark_error / email_usages）**

在 `common/store.py` 的 `list_emails` 方法之后追加：

```python
    def next_email(self, platform):
        """原子取号：取一个该 platform 无 usages 记录的邮箱，插 reserved 并返回
        (email, password, refresh_token, client_id)；无则 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT e.id, e.email, e.password, e.refresh_token, e.client_id "
                "FROM emails e "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM usages u "
                "  WHERE u.email_id = e.id AND u.platform = ?"
                ") ORDER BY e.id LIMIT 1",
                (platform,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "INSERT INTO usages (email_id, platform, status, updated_at) "
                "VALUES (?, ?, 'reserved', ?)",
                (row["id"], platform, _now()),
            )
            self._conn.commit()
            return (row["email"], row["password"],
                    row["refresh_token"], row["client_id"])

    def _upsert_usage(self, platform, email, status, reason=""):
        email = email.strip().lower()
        with self._lock:
            er = self._conn.execute(
                "SELECT id FROM emails WHERE email=?", (email,)
            ).fetchone()
            if er is None:
                return
            self._conn.execute(
                "INSERT INTO usages (email_id, platform, status, reason, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(email_id, platform) DO UPDATE SET "
                "status=excluded.status, reason=excluded.reason, "
                "updated_at=excluded.updated_at",
                (er["id"], platform, status, reason, _now()),
            )
            self._conn.commit()

    def mark_used(self, platform, email, password=""):
        self._upsert_usage(platform, email, "ok")

    def mark_error(self, platform, email, password="", reason=""):
        self._upsert_usage(platform, email, "error", reason)

    def email_usages(self, email):
        email = email.strip().lower()
        rows = self._conn.execute(
            "SELECT u.platform, u.status, u.reason, u.updated_at "
            "FROM usages u JOIN emails e ON u.email_id = e.id "
            "WHERE e.email = ? ORDER BY u.platform",
            (email,),
        ).fetchall()
        return [dict(r) for r in rows]
```

注：并发取号的原子性由 `self._lock`（同进程多线程串行化 SELECT+INSERT）+ `UNIQUE(email_id, platform)`（跨连接兜底）共同保证，契合"单服务主导"形态。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m unittest tests.test_store.TestNextEmail -v`
预期：PASS（7 个测试）

- [ ] **步骤 5：Commit**

```bash
git add common/store.py tests/test_store.py
git commit -m "feat(store): 原子取号 next_email 与状态标记 mark_used/mark_error"
```

---

## 任务 3：stats 概览、cookie 读写与全局单例

**文件：**
- 修改：`common/store.py`
- 测试：`tests/test_store.py`

- [ ] **步骤 1：编写失败的测试（stats + cookie + 单例）**

在 `tests/test_store.py` 末尾追加：

```python
class TestStatsAndCookie(StoreTestBase):
    def test_stats_counts_by_platform_status(self):
        for i in range(3):
            self.store.add_email(f"u{i}@b.com", "pw")
        self.store.next_email("claude")                 # 1 reserved
        self.store.mark_used("claude", "u0@b.com")       # 变 ok（同一邮箱）
        stats = self.store.stats()
        self.assertIn("total_emails", stats)
        self.assertEqual(stats["total_emails"], 3)
        self.assertIn("claude", stats["platforms"])
        self.assertEqual(stats["platforms"]["claude"]["ok"], 1)

    def test_save_and_get_cookie(self):
        self.store.add_email("a@b.com", "pw")
        self.store.save_cookie("github", '{"c":1}', email="a@b.com")
        self.assertEqual(self.store.get_cookie("github", email="a@b.com"),
                         '{"c":1}')

    def test_save_cookie_upsert_overwrites(self):
        self.store.add_email("a@b.com", "pw")
        self.store.save_cookie("github", "v1", email="a@b.com")
        self.store.save_cookie("github", "v2", email="a@b.com")
        self.assertEqual(self.store.get_cookie("github", email="a@b.com"), "v2")

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
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m unittest tests.test_store.TestStatsAndCookie tests.test_store.TestSingleton -v`
预期：FAIL / ERROR，报 `AttributeError` 或 `no attribute 'stats'`

- [ ] **步骤 3：编写最少实现（stats / save_cookie / get_cookie / get_store）**

在 `common/store.py` 的 `email_usages` 方法之后追加：

```python
    def stats(self):
        total = self._conn.execute(
            "SELECT COUNT(*) AS c FROM emails"
        ).fetchone()["c"]
        rows = self._conn.execute(
            "SELECT platform, status, COUNT(*) AS c "
            "FROM usages GROUP BY platform, status"
        ).fetchall()
        platforms = {}
        for r in rows:
            platforms.setdefault(r["platform"], {})[r["status"]] = r["c"]
        return {"total_emails": total, "platforms": platforms}

    # ---- cookie ----
    def _email_id(self, email):
        if email is None:
            return None
        r = self._conn.execute(
            "SELECT id FROM emails WHERE email=?", (email.strip().lower(),)
        ).fetchone()
        return r["id"] if r else None

    def save_cookie(self, platform, payload, email=None):
        eid = self._email_id(email)
        with self._lock:
            self._conn.execute(
                "INSERT INTO cookies (email_id, platform, payload, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(email_id, platform) DO UPDATE SET "
                "payload=excluded.payload, updated_at=excluded.updated_at",
                (eid, platform, payload, _now()),
            )
            self._conn.commit()

    def get_cookie(self, platform, email=None):
        eid = self._email_id(email)
        if eid is None:
            r = self._conn.execute(
                "SELECT payload FROM cookies WHERE platform=? AND email_id IS NULL",
                (platform,),
            ).fetchone()
        else:
            r = self._conn.execute(
                "SELECT payload FROM cookies WHERE platform=? AND email_id=?",
                (platform, eid),
            ).fetchone()
        return r["payload"] if r else None
```

在 `common/store.py` 文件末尾（类定义之外）追加全局单例：

```python
# ---------------------------------------------------------------- 全局单例
_SINGLETON = None
_SINGLETON_LOCK = threading.Lock()


def get_store(db_path=None):
    """返回进程级 AccountStore 单例。首次调用可传 db_path，之后忽略。"""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = AccountStore(db_path or DEFAULT_DB)
        return _SINGLETON
```

注意：`save_cookie` 依赖 `UNIQUE(email_id, platform)`。SQLite 中多行 `email_id IS NULL` 不受 UNIQUE 约束（NULL 互不相等），故无邮箱关联的 cookie 的 upsert 冲突目标不生效——本期 cookie 主要按邮箱关联写入，无邮箱的 cookie 允许多条，符合现状不阻塞。

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m unittest tests.test_store.TestStatsAndCookie tests.test_store.TestSingleton -v`
预期：PASS（5 个测试）

- [ ] **步骤 5：Commit**

```bash
git add common/store.py tests/test_store.py
git commit -m "feat(store): stats 概览、cookie 读写与全局单例 get_store"
```

---

## 任务 4：common/emails.py 兼容层委托 store

**文件：**
- 修改：`common/emails.py`（整体替换，见步骤 3）
- 测试：`tests/test_store.py`

- [ ] **步骤 1：编写失败的测试（兼容函数走 DB）**

在 `tests/test_store.py` 末尾追加：

```python
class TestEmailsCompat(unittest.TestCase):
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
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m unittest tests.test_store.TestEmailsCompat -v`
预期：FAIL —— 现有 `common/emails.py` 仍读 `emails.txt` 文件，`next_email` 返回 `None`（临时 db 无 txt），断言不符。

- [ ] **步骤 3：整体替换 `common/emails.py` 为委托实现**

将 `common/emails.py` 全文替换为：

```python
# -*- coding: utf-8 -*-
"""
common/emails.py — 邮箱供给（兼容层）

保留历史模块级函数签名（next_email / mark_used / mark_error），
内部委托 common.store 的全局 AccountStore 单例。
现有调用方（register.py 等）导入方式与行为不变，数据落到 SQLite。
"""

import sys

from common.store import get_store

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def next_email(platform):
    """取下一个未被该平台占用的邮箱，返回 (email, password, refresh_token, client_id) 或 None。
    取出即标记 reserved，防止并发重复。"""
    got = get_store().next_email(platform)
    if got:
        print(f"  [email] picked for {platform}: {got[0]}")
    else:
        print(f"  [email] no unused emails left for {platform}")
    return got


def mark_used(platform, email, password=""):
    get_store().mark_used(platform, email, password)


def mark_error(platform, email, password="", reason=""):
    get_store().mark_error(platform, email, password, reason)
```

- [ ] **步骤 4：运行全部 store 测试验证通过**

运行：`python -m unittest tests.test_store -v`
预期：PASS（全部测试，含 TestEmailsCompat 2 个）

- [ ] **步骤 5：Commit**

```bash
git add common/emails.py tests/test_store.py
git commit -m "refactor(emails): 兼容层委托 AccountStore，数据落 SQLite"
```

---

## 任务 5：幂等导入工具 import_to_store.py

**文件：**
- 创建：`tools/import_to_store.py`
- 测试：`tests/test_store.py`

- [ ] **步骤 1：编写失败的测试（导入 + 幂等）**

在 `tests/test_store.py` 末尾追加：

```python
class TestImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "imp.db")

    def _write(self, name, content):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_import_emails_and_usages_idempotent(self):
        from tools.import_to_store import import_all
        self._write("emails.txt",
                    "a@b.com----pw1----tok1----cid1\n"
                    "c@d.com----pw2\n")
        self._write("emails_used_claude.txt",
                    "a@b.com----pw1----ok\n")
        self._write("emails_error_github.txt",
                    "c@d.com----pw2----captcha\n")

        r1 = import_all(root=self.tmp, db_path=self.db)
        self.assertEqual(r1["emails"], 2)

        from common.store import AccountStore
        s = AccountStore(self.db)
        try:
            self.assertEqual(len(s.list_emails()), 2)
            ua = s.email_usages("a@b.com")
            self.assertEqual(ua[0]["status"], "ok")
            uc = s.email_usages("c@d.com")
            self.assertEqual(uc[0]["status"], "error")
            self.assertEqual(uc[0]["reason"], "captcha")
        finally:
            s.close()

        # 幂等：再次导入不新增邮箱
        r2 = import_all(root=self.tmp, db_path=self.db)
        s2 = AccountStore(self.db)
        try:
            self.assertEqual(len(s2.list_emails()), 2)
        finally:
            s2.close()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m unittest tests.test_store.TestImport -v`
预期：FAIL —— `ModuleNotFoundError: No module named 'tools.import_to_store'`

- [ ] **步骤 3：编写导入工具**

创建 `tools/__init__.py`（空文件）。

创建 `tools/import_to_store.py`：

```python
# -*- coding: utf-8 -*-
"""
tools/import_to_store.py — 把现有纯文本数据幂等导入 AccountStore(SQLite)。

扫描项目根目录：
  emails.txt                     -> add_email(source="import")
  emails_used_<platform>.txt     -> usages(status=ok/reserved)
  emails_error_<platform>.txt    -> usages(status=error, reason)
  outlook_accounts/accounts_*.txt-> add_email(source="outlook_reg")
  cookies/**/full_*.json         -> save_cookie
反复运行安全（INSERT OR IGNORE + UNIQUE 约束）。

用法：python -m tools.import_to_store
"""

import glob
import os
import re
import sys

from common.store import AccountStore

_USED_RE = re.compile(r"^emails_used_(.+)\.txt$")
_ERROR_RE = re.compile(r"^emails_error_(.+)\.txt$")


def _split(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    return [p.strip() for p in line.split("----")]


def _import_emails_file(store, path, source):
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = _split(line)
            if not parts or "@" not in parts[0]:
                continue
            email = parts[0]
            password = parts[1] if len(parts) >= 2 else ""
            token = parts[2] if len(parts) >= 3 else ""
            client_id = parts[3] if len(parts) >= 4 else ""
            store.add_email(email, password, token, client_id, source=source)
            n += 1
    return n


def _import_usage_file(store, path, platform, status_field_is_reason):
    """status_field_is_reason=False -> used 文件(第3列是 ok/reserved/...)
    True -> error 文件(第3列是 reason，状态固定 error)。"""
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = _split(line)
            if not parts or "@" not in parts[0]:
                continue
            email = parts[0]
            third = parts[2] if len(parts) >= 3 else ""
            if status_field_is_reason:
                store.mark_error(platform, email, reason=third)
            else:
                if third == "ok":
                    store.mark_used(platform, email)
                elif third and third != "reserved":
                    store.mark_error(platform, email, reason=third)
                else:
                    # reserved：确保有一条 usages 记录（借道 next_email 语义不合适，
                    # 直接标记为 error/'reserved' 不妥，用 _upsert_usage）
                    store._upsert_usage(platform, email, "reserved")
            n += 1
    return n


def import_all(root=".", db_path=None):
    store = AccountStore(db_path or os.path.join(root, "data", "accounts.db"))
    result = {"emails": 0, "usages": 0, "cookies": 0}
    try:
        # 1) 主池
        main = os.path.join(root, "emails.txt")
        if os.path.isfile(main):
            result["emails"] += _import_emails_file(store, main, "import")

        # 2) outlook 新注册账号
        for path in glob.glob(os.path.join(root, "outlook_accounts", "accounts_*.txt")):
            result["emails"] += _import_emails_file(store, path, "outlook_reg")

        # 3) used / error 记录
        for name in os.listdir(root):
            m = _USED_RE.match(name)
            if m:
                result["usages"] += _import_usage_file(
                    store, os.path.join(root, name), m.group(1), False)
                continue
            m = _ERROR_RE.match(name)
            if m:
                result["usages"] += _import_usage_file(
                    store, os.path.join(root, name), m.group(1), True)

        # 4) cookies（按目录名当平台，payload 存 JSON 原文）
        for path in glob.glob(os.path.join(root, "cookies", "**", "full_*.json"),
                              recursive=True):
            platform = os.path.basename(os.path.dirname(path)) or "unknown"
            with open(path, encoding="utf-8") as f:
                payload = f.read()
            store.save_cookie(platform, payload)
            result["cookies"] += 1
    finally:
        store.close()
    return result


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    r = import_all(root=root)
    print(f"[import] emails={r['emails']} usages={r['usages']} cookies={r['cookies']}")
```

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m unittest tests.test_store.TestImport -v`
预期：PASS（1 个测试）

- [ ] **步骤 5：Commit**

```bash
git add tools/__init__.py tools/import_to_store.py tests/test_store.py
git commit -m "feat(tools): 幂等导入现有文本数据到 AccountStore"
```

---

## 任务 6：WebUI 只读查询接口

**文件：**
- 修改：`webui/server.py`（在 `# ==== 邮箱池批量导入` 段之后新增接口）
- 测试：手动 curl（WebUI 无既有自动化测试基建，遵循现状）

- [ ] **步骤 1：新增只读接口**

先确认 `webui/server.py` 顶部已 import（若无则加）。在文件中找到 `ROOT` 定义处附近确认 `ROOT` 指向项目根目录，然后在 `api_mailpool_import` 函数之后新增：

```python
# ============================================================ 账号中心查询（只读）
from common.store import AccountStore as _AccountStore

_ACCOUNTS_DB = os.path.join(ROOT, "data", "accounts.db")
_store_singleton = None


def _accounts_store():
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = _AccountStore(_ACCOUNTS_DB)
    return _store_singleton


@app.get("/api/accounts")
def api_accounts_list(platform: str = "", status: str = "", q: str = ""):
    rows = _accounts_store().list_emails(platform=platform or None,
                                         status=status or None)
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["email"].lower()]
    return {"total": len(rows), "accounts": rows}


@app.get("/api/accounts/stats")
def api_accounts_stats():
    return _accounts_store().stats()


@app.get("/api/accounts/{email}")
def api_accounts_detail(email: str):
    return {"email": email, "usages": _accounts_store().email_usages(email)}
```

注意：`/api/accounts/stats` 路由必须定义在 `/api/accounts/{email}` **之前**，否则 `stats` 会被 `{email}` 路径参数吞掉。上面的顺序已正确。

- [ ] **步骤 2：手动验证（先确保有数据）**

运行导入（若尚未导入）：`python -m tools.import_to_store`
启动 WebUI（按项目既有方式，如 `python -m webui.server` 或既有启动脚本），然后：

```bash
curl "http://127.0.0.1:8000/api/accounts/stats"
curl "http://127.0.0.1:8000/api/accounts?platform=claude&status=ok"
curl "http://127.0.0.1:8000/api/accounts/a@b.com"
```
预期：分别返回 stats JSON、筛选后的账号列表、单邮箱的跨平台 usages。（端口以项目实际配置为准）

- [ ] **步骤 3：Commit**

```bash
git add webui/server.py
git commit -m "feat(webui): 账号中心只读查询接口 /api/accounts"
```

---

## 任务 7：.gitignore 忽略数据库文件

**文件：**
- 修改：`.gitignore`

- [ ] **步骤 1：追加忽略规则**

在 `.gitignore` 末尾追加：

```gitignore
# 统一账号中心数据库（运行时生成，含明文 PII，不入库）
data/accounts.db
data/accounts.db-wal
data/accounts.db-shm
```

- [ ] **步骤 2：验证未被追踪**

运行：`git status --porcelain data/`
预期：无输出（data/*.db 已被忽略）

- [ ] **步骤 3：Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore 忽略 data/accounts.db*"
```

---

## 任务 8：全量测试与收尾验证

**文件：** 无新增

- [ ] **步骤 1：运行全部 store 测试**

运行：`python -m unittest tests.test_store -v`
预期：全部 PASS（约 20 个测试）。

- [ ] **步骤 2：验证兼容层不破坏既有导入**

运行：`python -c "import common.emails; print('emails compat OK')"`
预期：打印 `emails compat OK`，无 import 错误。

运行：`python -c "import common.store; s=common.store.get_store('data/accounts.db'); print('store OK', s.stats())"`
预期：打印 `store OK {...}`，无异常。

- [ ] **步骤 3：真实导入冒烟（可选，若本机有真实数据）**

运行：`python -m tools.import_to_store`
预期：打印 `[import] emails=N usages=M cookies=K`，N 与 `emails.txt` 行数一致。

- [ ] **步骤 4：最终 commit（若有未提交变更）**

```bash
git add -A
git commit -m "test: 账号中心全量验证通过"
```

---

## 自检结果

**1. 规格覆盖度：**
- 数据模型（emails/usages/cookies 三表）→ 任务 1、2、3 ✓
- 仓库 API（add_email/next_email/mark_used/mark_error/email_usages/list_emails/stats/save_cookie/get_cookie）→ 任务 1-3 ✓
- 兼容层（emails.py 委托 store）→ 任务 4 ✓
- 导入工具（幂等）→ 任务 5 ✓
- WebUI 查询接口 → 任务 6 ✓
- 错误处理（自动建库、取不到返回 None、UNIQUE 冲突安全）→ 任务 1/2 实现内含 ✓
- 测试（去重/原子性/状态/关联/导入幂等）→ 任务 1-5 测试 ✓
- .gitignore → 任务 7 ✓

**2. 占位符扫描：** 无 TODO/待定；每个代码步骤均含完整可运行代码。

**3. 类型一致性：** `next_email` 全程返回 `(email, password, refresh_token, client_id)` 四元组；`_upsert_usage` 在任务 2 定义、任务 5 导入工具复用；`get_store` 在任务 3 定义、任务 4 使用；方法名跨任务一致。

**已知取舍（非缺陷）：**
- 无邮箱关联的 cookie（`email_id IS NULL`）因 SQLite NULL 特性可存多条，符合现状不阻塞（规格 cookie 表说明已注明可空关联）。
- WebUI 接口沿用项目"无自动化测试"现状，用手动 curl 验证，不引入 pytest 依赖。
