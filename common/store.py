# -*- coding: utf-8 -*-
"""
common/store.py — 统一账号中心仓库层（SQLite）

单一数据库 data/accounts.db（WAL 模式），封装邮箱池、平台使用关联、cookie。
所有模块通过 AccountStore 读写，替代散落的纯文本文件。

并发（修正 ①⑧）：
- 连接 check_same_thread=False，读写均由 self._lock 串行化（读也进锁，杜绝单连接跨线程竞态）。
- next_email 用 BEGIN IMMEDIATE + busy_timeout 让 SQLite 跨连接/跨进程串行化取号事务，
  两个 AccountStore 实例并发取号不崩溃、不发重号。
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
        # timeout=5s: 跨进程写锁竞争时的连接级等待
        self._conn = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")  # ①：BEGIN IMMEDIATE 竞争时等待而非立即报错
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        self._conn.close()

    # ------------------------------------------------------------------ 邮箱池
    def add_email(self, email, password="", refresh_token="",
                  client_id="", source="import"):
        """插入邮箱；email 小写归一、UNIQUE 去重。
        修正 ⑦：已存在时用 upsert 补全「原值为空」的 password/refresh_token/client_id，
        非空旧值不覆盖；source/created_at 保留首次写入。返回 id。"""
        email = email.strip().lower()
        with self._lock:
            self._conn.execute(
                "INSERT INTO emails "
                "(email, password, refresh_token, client_id, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET "
                "  password=CASE WHEN emails.password='' "
                "    THEN excluded.password ELSE emails.password END, "
                "  refresh_token=CASE WHEN emails.refresh_token='' "
                "    THEN excluded.refresh_token ELSE emails.refresh_token END, "
                "  client_id=CASE WHEN emails.client_id='' "
                "    THEN excluded.client_id ELSE emails.client_id END",
                (email, password, refresh_token, client_id, source, _now()),
            )
            self._conn.commit()
            # DO UPDATE 时 lastrowid 不可靠，统一回查
            row = self._conn.execute(
                "SELECT id FROM emails WHERE email=?", (email,)
            ).fetchone()
            return row["id"] if row else 0

    def list_emails(self, platform=None, status=None):
        sql = ("SELECT e.id, e.email, e.password, e.refresh_token, e.client_id, "
               "e.source, e.created_at "
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
        with self._lock:  # ⑧：读也进锁
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- 取号 / 状态
    def next_email(self, platform):
        """原子取号：取一个该 platform 无 usages 记录的邮箱，插 reserved 并返回
        (email, password, refresh_token, client_id)；无则 None。
        修正 ①：BEGIN IMMEDIATE 让 SQLite 在事务起点即拿写锁，跨连接/跨进程串行化。"""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
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
                    self._conn.commit()
                    return None
                self._conn.execute(
                    "INSERT INTO usages (email_id, platform, status, updated_at) "
                    "VALUES (?, ?, 'reserved', ?)",
                    (row["id"], platform, _now()),
                )
                self._conn.commit()
                return (row["email"], row["password"],
                        row["refresh_token"], row["client_id"])
            except Exception:
                self._conn.rollback()
                raise

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
        with self._lock:  # ⑧
            rows = self._conn.execute(
                "SELECT u.platform, u.status, u.reason, u.updated_at "
                "FROM usages u JOIN emails e ON u.email_id = e.id "
                "WHERE e.email = ? ORDER BY u.platform",
                (email,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- 概览
    def stats(self):
        """返回 {total_emails, platforms: {plat: {ok, reserved, error, free}}}。
        修正 ③：每平台补 free = max(总邮箱 − 该平台已占用邮箱数, 0)。
        因 UNIQUE(email_id, platform)，已占用数 = 该平台各状态计数之和。"""
        with self._lock:  # ⑧
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
        for counts in platforms.values():
            counts["free"] = max(total - sum(counts.values()), 0)
        return {"total_emails": total, "platforms": platforms}

    # ---------------------------------------------------------------- cookie
    def _email_id(self, email):
        if email is None:
            return None
        with self._lock:  # ⑧
            r = self._conn.execute(
                "SELECT id FROM emails WHERE email=?", (email.strip().lower(),)
            ).fetchone()
        return r["id"] if r else None

    def save_cookie(self, platform, payload, email=None):
        """保存/覆盖 cookie。
        修正 ②：email_id IS NULL 时 SQLite 的 UNIQUE 不约束（NULL 互不相等），
        ON CONFLICT 不触发，故显式 UPDATE，rowcount==0 才 INSERT，保证幂等。"""
        eid = self._email_id(email)
        with self._lock:
            if eid is None:
                cur = self._conn.execute(
                    "UPDATE cookies SET payload=?, updated_at=? "
                    "WHERE platform=? AND email_id IS NULL",
                    (payload, _now(), platform),
                )
                if cur.rowcount == 0:
                    self._conn.execute(
                        "INSERT INTO cookies (email_id, platform, payload, updated_at) "
                        "VALUES (NULL, ?, ?, ?)",
                        (platform, payload, _now()),
                    )
            else:
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
        with self._lock:  # ⑧
            if eid is None:
                r = self._conn.execute(
                    "SELECT payload FROM cookies "
                    "WHERE platform=? AND email_id IS NULL",
                    (platform,),
                ).fetchone()
            else:
                r = self._conn.execute(
                    "SELECT payload FROM cookies WHERE platform=? AND email_id=?",
                    (platform, eid),
                ).fetchone()
        return r["payload"] if r else None

    def get_cookies_by_email(self, email):
        """查询该邮箱的所有 cookies。返回 [{platform, payload, updated_at}, ...]"""
        email = email.strip().lower()
        with self._lock:
            eid = self._conn.execute(
                "SELECT id FROM emails WHERE email=?", (email,)
            ).fetchone()
            if eid is None:
                return []
            rows = self._conn.execute(
                "SELECT platform, payload, updated_at FROM cookies "
                "WHERE email_id=? ORDER BY platform",
                (eid["id"],)
            ).fetchall()
        return [dict(r) for r in rows]


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


# ---------------------------------------------------------------- 便捷登记助手
def mark_registered(platform, email, password="", cookie_payload=None, source="reg"):
    """可复用登记助手：确保邮箱入库 → 标记平台使用 → 保存 cookie（若有）。

    用于注册流程成功时快速登记账号到 AccountStore。走全局单例。

    Args:
        platform: 平台名（如 "github"、"chatgpt"）
        email: 邮箱地址
        password: 邮箱密码（非平台密码）
        cookie_payload: cookie 数据（JSON 字符串或 dict），None 时不保存
        source: 来源标识（如 "github_reg"、"outlook_reg"）
    """
    store = get_store()
    # 1. 确保邮箱入库（幂等）
    store.add_email(email, password=password, source=source)
    # 2. 标记平台使用为 ok
    store.mark_used(platform, email)
    # 3. 保存 cookie（若提供）
    if cookie_payload is not None:
        import json
        if isinstance(cookie_payload, dict):
            cookie_payload = json.dumps(cookie_payload, ensure_ascii=False)
        store.save_cookie(platform, cookie_payload, email=email)
