# -*- coding: utf-8 -*-
"""
common/emails.py — 邮箱供给（兼容层）

保留历史模块级函数签名（next_email / mark_used / mark_error），
内部委托 common.store 的全局 AccountStore 单例（data/accounts.db）。
现有调用方（register_chatgpt.py / register_grok.py / register_three_platforms.py /
register.py 等）导入方式与行为不变，数据落 SQLite。

⚠ 过渡硬门（修正 ⑨）：首次切到 DB 前必须先运行
    python -m tools.import_to_store
把现有 emails.txt / emails_used_*.txt / emails_error_*.txt 及 register.py 的
emails_used.txt / emails_error.txt 导入，否则库为空、next_email 一律返回 None
（线上取号会骤然全部失败）。
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
