# -*- coding: utf-8 -*-
"""
tools/import_to_store.py — 把现有纯文本/JSON 数据幂等导入 AccountStore(SQLite)。

扫描项目根目录：
  emails.txt                       -> add_email(source="import")            四字段
  outlook_accounts/graph_tokens_*.json -> add_email(source="outlook_reg")   补 refresh_token (④)
  outlook_accounts/accounts_*.txt  -> add_email(source="outlook_reg")       email----password[----token]
  emails_used_<platform>.txt       -> usages(ok/reserved)                    common/emails.py 产物
  emails_error_<platform>.txt      -> usages(error, reason)
  emails_used.txt / emails_error.txt -> usages 平台="claude"                 register.py 通用池 (⑥⑨)
  cookies/<platform>/full_*.json   -> save_cookie(platform)                  (② 幂等)

幂等：add_email 用 ON CONFLICT 补全、mark_* 用 upsert、save_cookie 对无邮箱 cookie 显式
UPDATE/INSERT，反复运行不产生重复数据。

用法：python -m tools.import_to_store [root]
"""

import glob
import json
import os
import re
import sys

from common.store import AccountStore

_USED_RE = re.compile(r"^emails_used_(.+)\.txt$")
_ERROR_RE = re.compile(r"^emails_error_(.+)\.txt$")
# register.py 的通用池（无平台后缀）——映射到平台 "claude"（与 register.py ⑥ 的键一致）
_REGISTER_PLATFORM = "claude"


def _split(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    return [p.strip() for p in line.split("----")]


def _import_emails_file(store, path, source):
    """emails.txt / accounts_*.txt：email----password[----token[----client_id]]。"""
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


def _import_graph_tokens_json(store, path):
    """graph_tokens_*.json：[{email, password, refresh_token, ...}]（④ 补 refresh_token）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return 0
    if isinstance(data, dict):
        entries = data.get("accounts") or data.get("tokens") or []
    elif isinstance(data, list):
        entries = data
    else:
        entries = []
    n = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        email = (e.get("email") or "").strip()
        if "@" not in email:
            continue
        store.add_email(email, e.get("password", "") or "",
                        e.get("refresh_token", "") or "",
                        e.get("client_id", "") or "", source="outlook_reg")
        n += 1
    return n


def _import_used_file(store, path, platform):
    """used 文件：第3列 reserved -> reserved；其余(含 ok/空/register 2字段成功行) -> ok。"""
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = _split(line)
            if not parts or "@" not in parts[0]:
                continue
            email = parts[0]
            third = parts[2] if len(parts) >= 3 else ""
            if third == "reserved":
                store._upsert_usage(platform, email, "reserved")
            else:
                store.mark_used(platform, email)
            n += 1
    return n


def _import_error_file(store, path, platform):
    """error 文件：第3列为 reason，状态固定 error。"""
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = _split(line)
            if not parts or "@" not in parts[0]:
                continue
            email = parts[0]
            reason = parts[2] if len(parts) >= 3 else ""
            store.mark_error(platform, email, reason=reason)
            n += 1
    return n


def import_all(root=".", db_path=None):
    store = AccountStore(db_path or os.path.join(root, "data", "accounts.db"))
    result = {"emails": 0, "usages": 0, "cookies": 0}
    try:
        # 1) 邮箱池（先建号，供后续 usages 关联）
        main = os.path.join(root, "emails.txt")
        if os.path.isfile(main):
            result["emails"] += _import_emails_file(store, main, "import")

        # 2) outlook：graph_tokens_*.json（④ 补 token）+ accounts_*.txt
        for path in glob.glob(os.path.join(root, "outlook_accounts", "graph_tokens_*.json")):
            result["emails"] += _import_graph_tokens_json(store, path)
        for path in glob.glob(os.path.join(root, "outlook_accounts", "accounts_*.txt")):
            result["emails"] += _import_emails_file(store, path, "outlook_reg")

        # 3) used / error 记录 -> usages
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if not os.path.isfile(full):
                continue
            m = _USED_RE.match(name)
            if m:
                result["usages"] += _import_used_file(store, full, m.group(1))
                continue
            m = _ERROR_RE.match(name)
            if m:
                result["usages"] += _import_error_file(store, full, m.group(1))
                continue
            if name == "emails_used.txt":       # register.py 通用池 (⑥⑨)
                result["usages"] += _import_used_file(store, full, _REGISTER_PLATFORM)
            elif name == "emails_error.txt":
                result["usages"] += _import_error_file(store, full, _REGISTER_PLATFORM)

        # 4) cookies（目录名当平台，payload 存 JSON 原文；② 幂等）
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
