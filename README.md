# reg-factory — 一号三用 注册套件

用一个 Outlook 邮箱在 **Claude / ChatGPT / Grok** 三个平台自动注册，并导出可直登的
cookie。底层用 **比特浏览器(BitBrowser)** 做指纹隔离、**Clash Verge** 做节点切换绕区域
封锁与 Cloudflare 风控、接码/打码平台过手机号与验证码。

> ⚠️ 仅供学习与授权测试使用。所有密钥通过环境变量提供，仓库内不含任何明文凭据。

---

## 1. 前置条件

### ① 比特浏览器 BitBrowser
- 安装并**启动**比特浏览器客户端，确保本地 API 在线（默认 `http://127.0.0.1:54345`）。
- 客户端要保持运行——脚本通过该 API 创建/打开/关闭浏览器窗口。

### ② Clash Verge（开启 API 权限）
- 安装 Clash Verge 并导入订阅。**推荐订阅链接（一键订阅）**：

  ```
  https://6661231.xyz
  ```

  > 在 Clash Verge → **订阅** → 粘贴上面的链接 → 导入即可；导入后记得选一个节点并开启「系统代理 / Tun 模式」。
  > 注册 Grok 需要能过 Cloudflare 的干净节点，本订阅含多地区节点，脚本会自动逐个试探可用节点。
- **设置 → External Controller**：开启外部控制器 API，并**设置一个 secret**。
  - 记下控制面端口（Clash Verge 默认 `9097`，mihomo 内核默认 `9090`）。
  - 记下混合代理端口（mixed-port，默认 `7897`）。
- 把 secret 填进 `.env` 的 `CLASH_SECRET`（见下）。

### ③ Python
- Python 3.10+。

---

## 2. 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## 3. 配置（密钥走环境变量）

复制模板并填写：

```bash
cp .env.example .env
```

`.env` 已被 `.gitignore` 忽略。真实的进程环境变量优先于 `.env`。

| 环境变量 | 说明 | 必填 |
|---|---|---|
| `CLASH_SECRET` | Clash Verge External Controller 的 secret | 走节点时必填 |
| `CLASH_API` | Clash 控制面地址（默认 `http://127.0.0.1:9097`） | 否 |
| `CLASH_PROXY` | Clash 混合端口代理（默认 `http://127.0.0.1:7897`） | 否 |
| `CLASH_GROUP` | 切换出口的代理组名（默认 `GLOBAL`） | 否 |
| `BITBROWSER_API` | 比特浏览器本地 API（默认 `http://127.0.0.1:54345`） | 否 |
| `SMS_TOKEN` | 接码平台 firefox.fun 的 token | 需手机号时必填 |
| `HERO_SMS_API_KEY` | 备用接码 hero-sms.com 的 api_key | 否 |
| `CAPSOLVER_API_KEY` | CapSolver 打码 key | 按需 |
| `EZCAPTCHA_API_KEY` | EZ-Captcha 打码 key | 按需 |
| `OUTLOOK_CARD` | 闪客云邮箱卡密（接口批量取号用） | 用接口取号时填 |
| `OUTLOOK_PROXIES` | Outlook 自注册住宅代理池，`user:pass@host:port`，换行/逗号分隔 | 否 |
| `MAIL_*` | 备用域名邮箱（一般用不到） | 否 |

---

## 4. 运行

### 端到端（注册邮箱 → 三平台注册）
```bash
python run_full_flow.py                       # 注册 1 个 outlook 号后在 claude 上注册
python run_full_flow.py --platforms claude chatgpt grok
python run_full_flow.py --skip-email --email a@outlook.com --password xxx
python run_full_flow.py --dry-run             # 只打印将执行的命令
```
> 自动注入 `HTTP(S)_PROXY` 与 `CLASH_API/SECRET/GROUP` 给子进程。

### 仅三平台注册（已有邮箱池 emails.txt）
```bash
python register_three_platforms.py --from-pool
python register_three_platforms.py --email a@outlook.com --password xxx --token <refresh>
python register_three_platforms.py --loop     # 常驻消费池
```
并行流水线模式下建议先起共享取码服务（避免三窗口并发登录同一邮箱）：
```bash
python mailbox_broker.py --port 8765
```

### 仅养号（持续自注册 Outlook，写入 _outlook_pool/ 与 emails.txt）
```bash
python outlook_reg_loop.py                     # 循环
python outlook_reg_loop.py --count 20          # 注册 20 个后退出
```

### 导出已注册账号 cookie（供直登扩展使用）
```bash
python export_accounts.py                      # 全部平台
python export_accounts.py claude chatgpt       # 指定平台
```

### Clash 节点自检
```bash
python -m common.proxy_switch list             # 列出 GLOBAL 组节点
python _clash_verge.py ping                    # 控制面连通性
```

---

## 5. 目录约定

| 路径 | 内容 |
|---|---|
| `emails.txt` | 邮箱池（`email----password----token----clientid`），运行时生成 |
| `cookies/` | 注册成功导出的 cookie（`full_*.json` / `sk_*.txt`） |
| `_outlook_pool/` | outlook_reg_loop 产出的待用号 |
| `tri_register_logs/` | 三平台注册日志 |
| `screenshots*/` | 调试截图 |

以上运行期数据均被 `.gitignore` 忽略，发布包内为空。

---

## 6. 常见问题

- **claude 报 app-unavailable-in-region**：claude.com 对本机 IP 区域封锁，需开 Clash 走干净
  节点（`run_full_flow` / `register.py` 的 `--node auto`）。
- **grok 全页 Cloudflare 拦截**：必须切 Clash 节点；`register_grok.py` 会用 curl_cffi 指纹
  逐个试节点找能过的。
- **三窗口登录同一 outlook 报并发登录**：用 `mailbox_broker.py` 共享取码（每号只登一次）。
- **缺 secret 连不上 Clash 控制面**：确认 External Controller 已开 API 且 `CLASH_SECRET` 正确。
