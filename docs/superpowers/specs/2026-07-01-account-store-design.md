# 统一账号中心（AccountStore）设计规格

日期：2026-07-01
分支：`feat/account-store`

## 背景与目标

当前 reg-factory 的邮箱与账号数据完全散落在纯文本文件中：

- 邮箱池 `emails.txt`（`email----password----refresh_token----client_id`）
- 各平台占用/失败记录 `emails_used_<platform>.txt` / `emails_error_<platform>.txt`（`----reserved/ok/error` 文本行标记）
- 新注册 Outlook 账号 `outlook_accounts/accounts_*.txt` 与 `graph_tokens_*.json`
- 各平台 cookie `cookies/**/full_*.json`、`accounts.json`

各模块（`register.py`、`outlook_reg_loop.py`、`mailbox_broker.py`、`webui/server.py`）各自 `open()` 读写，导致：

1. **数据散落难查** —— 无法快速回答"某个邮箱当前状态如何、用在哪些平台"。
2. **状态/去重不可靠** —— 靠文本行标记，并发下易重复占用同一邮箱。
3. **缺统一读写接口** —— 每处各写各的解析逻辑。

### 目标

建立**统一账号中心**：单一 SQLite 数据库 + 统一仓库 API（`AccountStore`），覆盖邮箱池、平台使用关联、cookie、新注册 Outlook 账号。

### 已确认的需求边界

- **核心痛点**：数据散落难查、状态/去重不可靠、缺统一读写接口。（明文加密**不是**本次重点）
- **并发形态**：单服务主导（broker 为中心，单写入点），SQLite WAL 足够。
- **范围**：全账号中心（邮箱池 + 平台使用关联 + cookie + 新注册 Outlook 账号）。
- **过渡策略**：先建库后接入 —— 先搭仓库/API/导入工具/WebUI 查询，暂不改现有注册脚本；通过兼容层让老代码透明受益，再逐步显式改用 store。

### 非目标（YAGNI）

- 不做敏感字段加密（明文存储，延续现状；仅靠目录隔离 + gitignore）。
- 不引入独立数据库服务（PostgreSQL/MySQL）——单机单服务场景属过度设计。
- 不在本期改造现有注册脚本的业务逻辑（仅通过兼容层受益）。

## 存储技术选型

**方案 A：SQLite + 统一仓库层**（已选定）

- 单文件数据库 `data/accounts.db`，WAL 模式。
- 零额外依赖（Python 内置 `sqlite3`）。
- SQL 直接满足关联查询；`UNIQUE` 约束 + 事务根治并发去重。
- 契合"单服务主导"形态。

（方案 B 结构化 JSON + 文件锁：查询/去重要手写，随数据量变慢易错，否决。方案 C 独立数据库服务：过度设计，否决。）

## 数据模型

数据库 `data/accounts.db`，WAL 模式，三张表。

### `emails` — 邮箱池主表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `email` | TEXT UNIQUE NOT NULL | 邮箱地址，**小写归一**，天然去重 |
| `password` | TEXT | 密码 |
| `refresh_token` | TEXT | Outlook refresh_token |
| `client_id` | TEXT | client_id |
| `source` | TEXT | 来源：`import` / `outlook_reg` / `shankeyun` 等 |
| `created_at` | TEXT | 入库时间（ISO8601 字符串） |

### `usages` — 邮箱在各平台的使用记录（一对多）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `email_id` | INTEGER NOT NULL, FK→emails.id | 关联邮箱 |
| `platform` | TEXT NOT NULL | claude / github / gmail... |
| `status` | TEXT NOT NULL | `reserved` / `ok` / `error` |
| `reason` | TEXT | 失败原因 |
| `updated_at` | TEXT | 状态变更时间 |

约束：`UNIQUE(email_id, platform)` —— 一个邮箱在一个平台只有一条记录（替代原来的 reserved/ok/error 文本行叠加）。

### `cookies` — 平台登录态

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | |
| `email_id` | INTEGER, FK→emails.id | 关联邮箱（可空） |
| `platform` | TEXT NOT NULL | |
| `payload` | TEXT | cookie JSON 原文 |
| `updated_at` | TEXT | |

约束：`UNIQUE(email_id, platform)` —— 同邮箱同平台一条 cookie，更新即覆盖。

### 关键设计点

- **原子取号**：`next_email` 在单个事务内 `SELECT` 一个"该平台无 usages 记录"的空闲邮箱 → `INSERT usages(status='reserved')`，靠 `UNIQUE(email_id, platform)` 约束 + 写锁串行化根治并发重复占用。
- **关联查询**：`SELECT platform, status FROM usages JOIN emails ON usages.email_id=emails.id WHERE emails.email=?` 一句 SQL 回答"这个邮箱用在哪些平台"。

## 统一仓库 API：`common/store.py`

`AccountStore` 类封装所有读写，所有模块只调用它。核心方法与现有 `common/emails.py` **签名兼容**。

```python
class AccountStore:
    def __init__(self, db_path="data/accounts.db"):
        # 自动建目录、建库、建表、启用 WAL；首次运行零配置
        ...

    # ---- 邮箱池 ----
    def add_email(self, email, password="", refresh_token="",
                  client_id="", source="import") -> int:
        # INSERT OR IGNORE；email 小写归一；返回 id；已存在则跳过（去重）
        ...

    def next_email(self, platform) -> tuple | None:
        # 原子事务：取一个该 platform 无 usages 记录的邮箱 → 插 reserved → 返回
        # 返回 (email, password, refresh_token, client_id)，与旧签名一致；无则 None
        ...

    def mark_used(self, platform, email, password=""):
        # usages.status='ok'（UPSERT）
        ...

    def mark_error(self, platform, email, password="", reason=""):
        # usages.status='error'，记录 reason（UPSERT）
        ...

    # ---- 关联查询（新能力）----
    def email_usages(self, email) -> list[dict]:
        # 这个邮箱用在哪些平台及各自状态
        ...

    def list_emails(self, platform=None, status=None) -> list[dict]:
        # 池概览/筛选
        ...

    def stats(self) -> dict:
        # 各平台 free/reserved/ok/error 计数
        ...

    # ---- cookie（全账号中心）----
    def save_cookie(self, platform, payload, email=None):
        # UPSERT
        ...

    def get_cookie(self, platform, email=None) -> str | None:
        ...
```

### 兼容层

保留 `common/emails.py` 的模块级函数 `next_email(platform)` / `mark_used(...)` / `mark_error(...)`，内部改为委托给一个全局 `AccountStore` 单例。现有 `register.py` 等**导入方式不变**，接入时零改动即可透明切到 DB。

### 线程安全

`sqlite3` 连接用 `check_same_thread=False` + 一个 `threading.Lock` 包裹写操作；契合"单服务主导"下 broker 多线程访问。

## 导入工具：`tools/import_to_store.py`

一次性且**可重复跑（幂等）**：

- 扫描根目录 `emails.txt` → `add_email(source="import")`
- 扫描 `emails_used_<platform>.txt` / `emails_error_<platform>.txt` → 解析平台名 + `----ok/reserved/error` 状态 → 写入对应 `usages`
- 扫描 `outlook_accounts/accounts_*.txt` + `graph_tokens_*.json` → 补全 refresh_token，`source="outlook_reg"`
- 扫描 `cookies/**/full_*.json`、`accounts.json` → `save_cookie`
- 全程 `INSERT OR IGNORE` + UNIQUE 约束，重复跑不产生重复数据。
- 结束打印导入统计（邮箱数、usages 数、cookie 数）。

## WebUI 查询

在 `webui/server.py` 增加只读接口：

- `GET /api/accounts?platform=&status=&q=` → `store.list_emails(...)`，按平台/状态/邮箱关键词筛选
- `GET /api/accounts/<email>` → `store.email_usages(email)`，单邮箱跨平台关联
- `GET /api/accounts/stats` → `store.stats()`，池概览面板
- 现有 `/api/mailpool` 导入接口改为写入 store（可选保留 `emails.txt` 追加做兼容导出）

## 错误处理

- DB 文件/目录不存在 → 自动建库建表（首次运行零配置）。
- `next_email` 取不到空闲邮箱 → 返回 `None`（与现有行为一致）。
- 写冲突（UNIQUE 违反）→ 视为"已占用/已存在"，安全跳过，不抛错。
- 所有写操作在事务内，异常回滚。

## 测试：`tests/test_store.py`

pytest，用临时 db 文件：

- `add_email` 去重：同邮箱插两次只有一条。
- `next_email` 原子性：并发多线程取号不重复、不超发。
- `mark_used` / `mark_error`：正确落 usages 状态与 reason。
- `email_usages`：返回跨平台关联。
- 导入工具幂等性：跑两次结果一致。

## 交付顺序（先建库后接入）

1. `common/store.py`（AccountStore + 建表）
2. `tests/test_store.py`（验证核心不变量）
3. `tools/import_to_store.py`（导入现有数据）
4. `common/emails.py` 兼容层委托 store
5. `webui/server.py` 只读查询接口
6. （后续，独立进行）逐个显式改造注册脚本改用 store
