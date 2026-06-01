# -*- coding: utf-8 -*-
"""
config.py — 全局配置。

所有密钥/凭据都从环境变量读取（默认空），不在仓库里留明文。
支持把变量写进同目录的 .env 文件（见 .env.example）；.env 只在对应环境
变量尚未设置时生效，不会覆盖真实的进程环境变量。
"""

import os


# ---------------------------------------------------------------- .env 加载
def _load_dotenv(path=None):
    """零依赖 .env 读取器：解析 KEY=VALUE，忽略空行与 # 注释。
    只在 os.environ 里尚未设置该 KEY 时填入（真实环境变量优先）。"""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


_load_dotenv()


def _env(name, default=""):
    return os.environ.get(name, default)


# ---------------------------------------------------------------- 本地基建
# BitBrowser 本地 API 地址
BITBROWSER_API = _env("BITBROWSER_API", "http://127.0.0.1:54345")

# Claude.ai 注册相关 URL
CLAUDE_LOGIN_URL = "https://claude.ai/login"

# Cookie 输出目录
COOKIE_OUTPUT_DIR = "cookies"

# ---------------------------------------------------------------- 域名邮箱（备用）
MAIL_DOMAIN = _env("MAIL_DOMAIN", "")
MAIL_API_BASE = _env("MAIL_API_BASE", "")
MAIL_ADMIN_USER = _env("MAIL_ADMIN_USER", "admin")
MAIL_ADMIN_PASS = _env("MAIL_ADMIN_PASS", "")
# JWT token（从浏览器抓取，可能会过期需要更新）
MAIL_AUTH_TOKEN = _env("MAIL_AUTH_TOKEN", "")
# 新建邮箱统一密码
MAIL_NEW_PASS = _env("MAIL_NEW_PASS", "")

# ---------------------------------------------------------------- Outlook 邮箱 API (闪客云邮箱)
OUTLOOK_API_BASE = _env("OUTLOOK_API_BASE", "http://api.shankeyun.com")
OUTLOOK_CARD = _env("OUTLOOK_CARD", "")  # 闪客云卡密
OUTLOOK_TYPE = _env("OUTLOOK_TYPE", "outlook")  # outlook / hotmail / any

# ---------------------------------------------------------------- 短信接码平台 (firefox.fun)
SMS_API_BASE = _env("SMS_API_BASE", "http://www.firefox.fun/yhapi.ashx")
SMS_TOKEN = _env("SMS_TOKEN", "")  # 接码平台 token
SMS_PROJECT_ID = _env("SMS_PROJECT_ID", "2313")  # claude 项目
# 优先国家列表，按顺序尝试，""=任意(排除黑名单)
SMS_COUNTRY_PREFER = ["60", "56", "57", "44", ""]  # 60=马来西亚 56=智利 57=哥伦比亚 44=英国 ""=任意
SMS_COUNTRY_BLACKLIST = ["63"]  # 菲律宾

# ---------------------------------------------------------------- 备用短信平台 (hero-sms.com)
HERO_SMS_API_BASE = _env("HERO_SMS_API_BASE", "https://hero-sms.com/stubs/handler_api.php")
HERO_SMS_API_KEY = _env("HERO_SMS_API_KEY", "")  # 备用接码 api_key
HERO_SMS_SERVICE = _env("HERO_SMS_SERVICE", "acz")  # Claude 专用服务
# 优先国家: 7=马来西亚 52=泰国 16=英国 56=西班牙 39=阿根廷 86=意大利 34=爱沙尼亚 49=立陶宛 36=中国
HERO_SMS_COUNTRY_PREFER = [7, 52, 16, 56, 39, 86, 34, 49, 36]

# ---------------------------------------------------------------- 打码平台
# CapSolver 验证码打码平台
CAPSOLVER_API_KEY = _env("CAPSOLVER_API_KEY", "")

# EZ-Captcha 验证码打码平台
EZCAPTCHA_API_KEY = _env("EZCAPTCHA_API_KEY", "")
EZCAPTCHA_API_BASE = _env("EZCAPTCHA_API_BASE", "https://api.ez-captcha.com")
