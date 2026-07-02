# -*- coding: utf-8 -*-
import os, tempfile
from common.store import AccountStore

tmp = tempfile.mkdtemp()

print("=== #1: list_emails(status=) 无 platform 是否重复 ===")
s = AccountStore(os.path.join(tmp, "t1.db"))
s.add_email("a@b.com", "pw")
s.next_email("claude"); s.mark_used("claude", "a@b.com")
s.next_email("github"); s.mark_used("github", "a@b.com")
s.next_email("grok");   s.mark_used("grok", "a@b.com")
rows = s.list_emails(status="ok")
print("  仅一个邮箱, 返回行数 =", len(rows), "| 邮箱 =", [r["email"] for r in rows])
print("  ->", "BUG(重复)" if len(rows) != 1 else "OK")

print()
print("=== #3: add_email 去重返回的 id 是否正确 ===")
s2 = AccountStore(os.path.join(tmp, "t2.db"))
id1 = s2.add_email("first@x.com")
id2 = s2.add_email("second@x.com")
id_dup = s2.add_email("First@x.com")
print(f"  first 真实 id={id1}, 去重再插返回 id={id_dup}")
print("  ->", f"BUG(应={id1} 实={id_dup})" if id_dup != id1 else "OK")

print()
print("=== #2: get_cookie 传不存在邮箱是否误取全局(NULL)cookie ===")
s3 = AccountStore(os.path.join(tmp, "t3.db"))
s3.save_cookie("github", "GLOBAL-NULL-COOKIE")
got = s3.get_cookie("github", email="nobody@nowhere.com")
print(f"  查不存在邮箱得到 = {got!r}")
print("  ->", "BUG(误取全局)" if got is not None else "OK")

print()
print("=== #4: 导入多份同平台 cookie(无邮箱) 是否互相覆盖 ===")
s4 = AccountStore(os.path.join(tmp, "t4.db"))
s4.save_cookie("github", "cookie-account-A")
s4.save_cookie("github", "cookie-account-B")
n = s4._conn.execute("SELECT COUNT(*) c FROM cookies WHERE platform='github'").fetchone()["c"]
print(f"  存 2 份, DB 实际留下 = {n} 份")
print("  ->", "数据丢失(后覆盖前)" if n == 1 else f"{n} 份")
