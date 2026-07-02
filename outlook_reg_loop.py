"""Standalone Outlook registration loop. Continuously registers fresh
outlook accounts via BitBrowser + standalone register_outlook script, and
writes each success to _data_bundle/_outlook_pool/ as one JSON file per
record (email + password + session cookies).

The Replit batch (_batch_register.py / bs_register_step1.py) consumes these
via the `pool` email source — fully decoupled, so a slow self-reg attempt
never blocks the Replit signup pipeline.

Usage:
  python outlook_reg_loop.py                       # loop forever
  python outlook_reg_loop.py --count 20            # 20 attempts then exit
  python outlook_reg_loop.py --target-pool 10      # stop refilling once pool >= 10
  python outlook_reg_loop.py --max-press 5         # OUTLOOK_REG_MAX_PRESS
  python outlook_reg_loop.py --sleep 5             # gap between attempts (s)

Reads HTTP_PROXY env for Clash routing (host:port form). Set
SELF_REG_SCRIPT_PATH to override standalone script location.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import importlib.util
import urllib.error
import urllib.request
from urllib.parse import urlparse
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ARTIFACT_DIR = os.path.dirname(os.path.abspath(__file__))
POOL_DIR = os.path.join(ARTIFACT_DIR, "_outlook_pool")
# 账号注册侧消费的池（common/emails.next_email 读取），格式 email----password----token----clientid
EMAILS_POOL = os.path.join(ARTIFACT_DIR, "emails.txt")

STANDALONE_PATH = os.environ.get(
    "SELF_REG_SCRIPT_PATH",
    os.path.join(ARTIFACT_DIR, "register_outlook_standalone.py"),
)

# Optional Clash rotation between attempts. Without this, MS PerimeterX
# learns the egress IP after 1-2 signups and ERR_CONNECTION_CLOSEDs us out.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import _clash_verge  # type: ignore
except ImportError:
    _clash_verge = None


def log(msg, level="INFO"):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {msg}", flush=True)


def load_standalone():
    if not os.path.isfile(STANDALONE_PATH):
        log(f"standalone not found at {STANDALONE_PATH}", "ERR")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("_self_reg_standalone", STANDALONE_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    log(f"loaded standalone from {STANDALONE_PATH}")
    return m


def init_clash():
    """Connect to Clash controller. Returns (client, group_name) or (None, None)."""
    if _clash_verge is None:
        return None, None
    api = os.environ.get("CLASH_API", "").strip() or None
    secret = os.environ.get("CLASH_SECRET", "").strip()
    if not api:
        try:
            api = _clash_verge.auto_detect_api(secret=secret)
        except Exception as e:
            log(f"clash auto-detect failed: {e}", "WARN")
            return None, None
    if not api:
        return None, None
    try:
        client = _clash_verge.ClashClient(api=api, secret=secret)
    except Exception as e:
        log(f"clash client init failed: {e}", "WARN")
        return None, None
    group = (os.environ.get("CLASH_GROUP", "").strip() or "").strip()
    if not group or group.lower() == "auto":
        try:
            group = _clash_verge.auto_pick_group(client) or ""
        except Exception as e:
            log(f"clash auto-pick group failed: {e}", "WARN")
    if not group:
        log("clash: no usable group", "WARN")
        return None, None
    log(f"clash ready: api={api} group={group!r}")
    return client, group


# Clash 节点轮换排除名单：国内直连/大陆节点从中国 IP 出口，Outlook(MS PerimeterX)
# 对中国 IP 的按住验证基本必挂，且轮到它纯浪费一次 attempt，故从 GLOBAL 轮换里剔除。
# 子串匹配（节点名含任一即排除）。可经 CLASH_EXCLUDE_NODES 环境变量追加（逗号分隔）。
_CN_EXCLUDE_HINTS = ("国内直连", "直连", "DIRECT", "大陆", "国内", "China", "回国")


def _rotate_excluded(client, group):
    """把 CN/直连子串提示解析成 GLOBAL 组里真实节点名集合（pick_node 用精确匹配，
    故必须先列出实际节点名再按子串挑出要排除的）。CLASH_EXCLUDE_NODES 追加精确名。"""
    ex = set()
    extra = (os.environ.get("CLASH_EXCLUDE_NODES") or "").strip()
    if extra:
        ex |= {x.strip() for x in extra.replace("，", ",").split(",") if x.strip()}
    try:
        for name in client.list_nodes(group):
            if any(h in name for h in _CN_EXCLUDE_HINTS):
                ex.add(name)
    except Exception as e:
        log(f"resolve excluded nodes err: {type(e).__name__}: {e}", "WARN")
    return ex


def maybe_rotate(client, group, strategy="round_robin", max_latency_ms=6000,
                 mixed_port=7897):
    """Rotate to a fresh Clash node and verify egress IP actually changed.
    Uses rotate_with_verify which recurses into nested selector groups when
    the outer switch hits another group (e.g. GLOBAL -> 📲 Telegram is just
    another selector, not a real node).

    排除国内直连/大陆节点（见 _CN_EXCLUDE_HINTS）：中国 IP 注册 Outlook 基本必挂。"""
    if client is None or not group:
        return None
    try:
        excluded = _rotate_excluded(client, group)
        if excluded:
            log(f"clash rotate excluding CN/direct nodes: {sorted(excluded)}")
        info = _clash_verge.rotate_with_verify(
            client, group, strategy=strategy,
            max_latency_ms=max_latency_ms,
            mixed_port=mixed_port,
            settle_sec=1.5,
            excluded=excluded,
        )
        if info.get("ip_changed"):
            log(f"clash IP {info.get('ip_before')} -> {info.get('ip_after')} (group={info.get('group')})")
        else:
            log(f"clash rotate: IP unchanged ({info.get('ip_before')})", "WARN")
        return info
    except Exception as e:
        log(f"clash rotate err: {type(e).__name__}: {e}", "WARN")
        return None


def clash_proxy_from_env():
    raw = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        or ""
    ).strip()
    if not raw:
        return None
    for pfx in ("http://", "https://", "socks5://"):
        if raw.lower().startswith(pfx):
            raw = raw[len(pfx):]
            break
    return raw.rstrip("/") or None


BB_API = "http://127.0.0.1:54345"
# Match bs_register_step1 — user's BitBrowser has Chromium 146 not 130.
BB_CORE_VERSION = os.environ.get("BB_CORE_VERSION", "146")


def _bb_call(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BB_API}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def bb_create_for_outlook_reg(name):
    """Mirror bs_register_step1.bb_create_ephemeral so we share the working
    fingerprint config (proxyType=noproxy + IP-derived locale; routes through
    Clash via TUN). Standalone's hardcoded coreVersion=130 returns 502 on
    BitBrowser builds that only have Chromium 146 installed."""
    body = {
        "name": name,
        "remark": "outlook reg loop — auto-deleted after use",
        "platform": "https://outlook.live.com",
        "platformIcon": "outlook.live.com",
        "proxyMethod": 2,
        "proxyType": "noproxy",
        "browserFingerPrint": {
            "ostype": "PC",
            "os": "Win32",
            "coreVersion": BB_CORE_VERSION,
            "isIpCreateTimeZone": True,
            "isIpCreateLanguage": True,
            "isIpCreateDisplayLanguage": True,
            "isIpCreatePosition": True,
            "isIpCountry": True,
        },
    }
    r = _bb_call("/browser/update", body)
    if not r.get("success"):
        raise RuntimeError(f"/browser/update failed: {r}")
    data = r.get("data") or {}
    pid = data.get("id") or data.get("browserId")
    if not pid:
        raise RuntimeError(f"/browser/update returned no id: {data}")
    return pid


def count_pool():
    if not os.path.isdir(POOL_DIR):
        return 0
    try:
        return sum(1 for f in os.listdir(POOL_DIR) if f.endswith(".json"))
    except Exception:
        return 0


def append_to_emails_pool(email, password):
    """把成功号桥接进 emails.txt 池，供账号注册侧 common/emails.next_email 消费。
    注册成功后立即用纯 HTTP OAuth 抽 Graph refresh_token（extract_graph_tokens.get_graph_token），
    写真 token/client_id —— 之后 ChatGPT 取码全走 Graph API，免浏览器登录/取码。
    抽取失败（偶发风控/网络）才回退占位符 fresh，消费侧届时退化到浏览器取码。"""
    token = client_id = "fresh"
    try:
        from extract_graph_tokens import get_graph_token
        # 抽取经代理偶发 TLS 抖动(SSLEOFError)，单试一次一抖就回退 fresh、白丢 token 快路；
        # 这里重试 3 次(短退避)，绝大多数抖动二/三次就过。
        res = None
        for _try in range(3):
            res = get_graph_token(email, password)
            if res and res.get("refresh_token"):
                break
            if _try < 2:
                # 抽取经代理偶发 TLS 抖动：第 2 次起先切 Clash 节点换出口再试(绕开坏节点)。
                log(f"graph token 抽取第{_try+1}次未成，切节点重试: {email}", "WARN")
                try:
                    from common import proxy_switch as _ps
                    import random as _rnd
                    _cur = _ps.current_node()
                    _cands = [n for n in _ps.concrete_nodes() if n != _cur]
                    if _cands:
                        _ps.set_node(_rnd.choice(_cands))
                except Exception as _e:
                    log(f"切节点失败(忽略): {str(_e)[:50]}", "WARN")
                time.sleep(3 * (_try + 1))
        if res and res.get("refresh_token"):
            token = res["refresh_token"]
            client_id = res.get("client_id") or "fresh"
            log(f"graph token extracted for {email}", "OK")
        else:
            log(f"graph token 抽取失败(3 次)，回退 fresh: {email}", "WARN")
    except Exception as e:
        log(f"graph token 抽取异常，回退 fresh: {type(e).__name__}: {e}", "WARN")
    try:
        existing = set()
        if os.path.isfile(EMAILS_POOL):
            with open(EMAILS_POOL, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        existing.add(line.split("----")[0].strip().lower())
        if email.lower() in existing:
            return
        with open(EMAILS_POOL, "a", encoding="utf-8") as f:
            f.write(f"{email}----{password}----{token}----{client_id}\n")
        log(f"emails.txt += {email} (token={'yes' if token != 'fresh' else 'fresh'})", "OK")
        # ⑩：同步桥接进账号中心 DB(幂等 upsert)，让 --from-pool 消费方(register_chatgpt/
        # grok/three)即时可见新号，不再依赖手动重跑 import。store 写失败仅告警，
        # 不影响 emails.txt 主路径（token/client_id 原样写入，含 "fresh" 哨兵值）。
        try:
            from common.store import get_store
            get_store().add_email(email, password, token, client_id, source="outlook_reg")
        except Exception as _e:
            log(f"account-store 桥接失败(忽略): {type(_e).__name__}: {_e}", "WARN")
    except Exception as e:
        log(f"append_to_emails_pool failed: {type(e).__name__}: {e}", "WARN")


def _build_outlook_upload_url(raw_url):
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return ""
    if "://" not in raw_url:
        raw_url = "http://" + raw_url
    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        return raw_url
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/api/external/outlook/upload"):
        return raw_url
    return raw_url.rstrip("/") + "/api/external/outlook/upload"


def upload_outlook_account(raw_url, api_key, email, password):
    """Upload one newly-created Outlook account to outlookEmail external API."""
    url = _build_outlook_upload_url(raw_url)
    api_key = (api_key or "").strip()
    if not url:
        return False, "缺少 outlook邮箱url"
    if not api_key:
        return False, "缺少 apikey"
    body = {
        "email": email,
        "password": password,
        "remark": f"reg-factory self-loop {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read(8192).decode("utf-8", "replace")
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                payload = {}
            if 200 <= r.status < 300:
                added = payload.get("added") if isinstance(payload, dict) else None
                duplicate = payload.get("duplicate") if isinstance(payload, dict) else None
                if added is not None or duplicate is not None:
                    return True, f"added={added or 0}, duplicate={duplicate or 0}"
                return True, "上传成功"
            return False, f"HTTP {r.status}: {text[:200]}"
    except urllib.error.HTTPError as e:
        text = ""
        try:
            text = e.read(8192).decode("utf-8", "replace")
        except Exception:
            pass
        return False, f"HTTP {e.code}: {text[:200] or e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def write_record(record):
    os.makedirs(POOL_DIR, exist_ok=True)
    safe = record["email"].replace("@", "_at_").replace("/", "_")
    fname = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:18] + f"_{safe}.json"
    tmp = os.path.join(POOL_DIR, fname + ".tmp")
    dst = os.path.join(POOL_DIR, fname)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.rename(tmp, dst)
    except Exception as e:
        log(f"write_record FAILED: {type(e).__name__}: {e}  (tmp={tmp})", "ERR")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise
    # Verify it actually landed.
    if os.path.isfile(dst):
        sz = os.path.getsize(dst)
        log(f"write_record OK: {dst}  ({sz} bytes)", "OK")
    else:
        log(f"write_record sus: {dst} missing right after rename!", "ERR")
    return fname


async def _run_outlook_on_ctx(mod, ctx, idx):
    """Scrub residual state -> 新页注册 -> 导出 outlook 相关 cookie。"""
    # Scrub Chromium residual state so signup.live.com doesn't see a
    # stale identity from a previous session.
    try:
        await ctx.clear_cookies()
        for _pg in ctx.pages:
            try:
                c = await ctx.new_cdp_session(_pg)
                await c.send("Network.clearBrowserCookies")
                await c.send("Network.clearBrowserCache")
                try: await c.detach()
                except Exception: pass
                break
            except Exception:
                pass
    except Exception:
        pass
    page = await ctx.new_page()
    email, password = await mod.register_outlook(page, ctx, idx)
    cookies = []
    if email:
        try:
            all_cookies = await ctx.cookies()
            keep_domains = (
                "outlook.", "live.com", "microsoftonline.",
                "microsoft.com", "office.com", ".office365.",
                "msn.com", "bing.com", "mail.live.com",
            )
            cookies = [
                c for c in all_cookies
                if any(d in (c.get("domain") or "") for d in keep_domains)
            ]
        except Exception as e:
            log(f"cookie export failed: {e}", "WARN")
    return email, password, cookies


async def one_attempt(mod, proxy_str, idx):
    """Mirrors bs_register_step1.fetch_email_from_self_register's inline
    flow, but doesn't carry the breaker state — we're a dedicated loop and
    want to keep trying."""
    profile_id = None
    bb = mod.BitBrowserClient()
    try:
        ts = datetime.now().strftime("%m%d_%H%M%S")
        for _r in range(5):
            try:
                # Use our own create that picks coreVersion=146 (matches the
                # BitBrowser install on this machine). Standalone's hardcoded
                # 130 makes BB return 502.
                profile_id = bb_create_for_outlook_reg(f"outlook_loop_{ts}_{idx}")
                break
            except Exception as e:
                m = str(e)
                if "最大" in m or "超过" in m:
                    log("BitBrowser quota — cleanup_browsers(keep=2)", "WARN")
                    try: bb.cleanup_browsers(keep=2)
                    except Exception: pass
                    await asyncio.sleep(3)
                    continue
                if _r >= 4:
                    raise
                log(f"create_browser err (try {_r+1}/5): {m[:200]}", "WARN")
                await asyncio.sleep(3 + _r)
        if not profile_id:
            return None, None, []
        info = bb.open_browser(profile_id)
        ws = info.get("ws", "")
        if not ws:
            return None, None, []
        from playwright.async_api import async_playwright as _apw
        async with _apw() as p:
            browser = await p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            email, password, cookies = await _run_outlook_on_ctx(mod, ctx, idx)
        return email, password, cookies
    finally:
        if profile_id:
            try:
                bb.close_browser(profile_id)
                await asyncio.sleep(2)
                bb.delete_browser(profile_id)
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=0,
                    help="run this many attempts then exit (0 = loop forever)")
    ap.add_argument("--target-pool", type=int, default=0,
                    help="stop registering once pool dir has this many records "
                         "(0 = no cap; producer always runs)")
    ap.add_argument("--max-press", default="3",
                    help="OUTLOOK_REG_MAX_PRESS — captcha press-and-hold cap")
    ap.add_argument("--timeout", type=int, default=180,
                    help="hard cap per attempt (seconds)")
    ap.add_argument("--sleep", type=int, default=5,
                    help="seconds between attempts (after fail or success)")
    ap.add_argument("--sleep-when-full", type=int, default=60,
                    help="seconds to sleep when pool is at target")
    ap.add_argument("--upload", action="store_true",
                    help="upload each successful Outlook account to an external outlookEmail API")
    ap.add_argument("--outlook-email-url", default="",
                    help="outlookEmail base URL or full /api/external/outlook/upload endpoint")
    ap.add_argument("--apikey", default="",
                    help="API key for the outlookEmail external API")
    args = ap.parse_args()

    os.environ.setdefault("OUTLOOK_REG_MAX_PRESS", args.max_press)
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    mod = load_standalone()
    proxy = clash_proxy_from_env()
    if not proxy:
        log("HTTP_PROXY not set — running without proxy (signup will likely fail)", "WARN")
    else:
        log(f"using clash proxy: {proxy}")

    # Initialize Clash controller for per-attempt node rotation. MS PerimeterX
    # learns the egress IP fast — without rotation we get ERR_CONNECTION_CLOSED
    # after 1-2 signups from the same node.
    clash_client, clash_group = init_clash()

    log(f"pool dir: {POOL_DIR}")
    os.makedirs(POOL_DIR, exist_ok=True)
    log(f"current pool size: {count_pool()}")
    if args.upload:
        upload_url = _build_outlook_upload_url(args.outlook_email_url)
        if upload_url and args.apikey:
            log(f"outlook upload enabled: {upload_url}")
        else:
            log("outlook upload enabled but url/apikey is missing; uploads will be skipped", "WARN")

    n = 0
    succ = 0
    failed = 0
    while True:
        n += 1
        if args.count and n > args.count:
            log(f"reached --count {args.count}, exit (success={succ}, fail={failed})")
            break
        ps = count_pool()
        if args.target_pool and ps >= args.target_pool:
            log(f"pool at target ({ps}/{args.target_pool}) — sleep {args.sleep_when_full}s")
            time.sleep(args.sleep_when_full)
            continue
        # Rotate Clash node before each attempt so MS PX sees a fresh IP.
        maybe_rotate(clash_client, clash_group)
        log(f"=== attempt #{n}  (pool={ps}, succ={succ}, fail={failed}) ===")
        t0 = time.time()
        email = password = None
        cookies = []
        try:
            email, password, cookies = asyncio.run(
                asyncio.wait_for(one_attempt(mod, proxy, n), timeout=args.timeout)
            )
        except Exception as e:
            log(f"attempt raised {type(e).__name__}: {str(e)[:200]}", "WARN")
        elapsed = time.time() - t0
        if email and password:
            fname = write_record({
                "email": email,
                "password": password,
                "outlook_cookies": cookies,
                "source": "self-loop",
                "ts": datetime.now().isoformat(),
            })
            append_to_emails_pool(email, password)   # 桥接进账号注册池
            if args.upload:
                ok, msg = upload_outlook_account(args.outlook_email_url, args.apikey, email, password)
                log(f"outlook upload {'OK' if ok else 'FAILED'}: {email} ({msg})",
                    "OK" if ok else "WARN")
            succ += 1
            log(f"OK in {elapsed:.1f}s: {email} -> {fname} (pool now {count_pool()})", "OK")
        else:
            failed += 1
            log(f"FAIL in {elapsed:.1f}s (success rate {succ}/{n} = {100*succ/n:.0f}%)", "WARN")
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
