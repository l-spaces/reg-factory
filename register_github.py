# -*- coding: utf-8 -*-
"""
GitHub 自动注册（探索版）

复用 common/ 基建: BitBrowser + stealth + Outlook 取验证码(浏览器登录) + cookie 保存。
邮箱来源: _outlook_pool/*.json（每个文件含 email/password/outlook_cookies，无 refresh_token，
          故验证码只能用浏览器登录 Outlook 取信 get_code_outlook_pw）。

GitHub 注册流程（github.com/signup 实测=单页表单，非多步）:
  一页内: Email(#email) + Password(#password) + Username(#login)
         + Country/Region 自定义下拉 + marketing 勾选框 + "Create account" 提交按钮
         （按钮 disabled，三字段都合法 + 国家选好后才解除）
  点 Create account 之后 -> Arkose FunCaptcha（旋转图片拼图，iframe 内）<<< 验证命门
  过验证 -> 邮件 launch code（6~8 位）-> 提交 -> 进站

坑: 页面顶部有 "Continue with Google" / "Continue with Apple" 第三方登录按钮，
    任何 "Continue" 子串匹配都会误点 Google -> 跳进 Google 注册流。提交只认 "Create account"。

本脚本默认 --explore: 把表单填到验证那一步就停，截图 + 保留窗口，便于研究怎么过验证。
加 --auto 则尝试走完（验证码当前未接打码，会停在 captcha）。

用法:
    python register_github.py                 # 探索：填到验证步停下，保留窗口
    python register_github.py --email a@b.com  # 指定邮箱
    python register_github.py --auto           # 尝试走完整流程
"""

import argparse
import asyncio
import glob
import json
import os
import random
import re
import string
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from playwright.async_api import async_playwright
import requests

from common.browser import open_and_connect, teardown, human_type, react_fill
from common.mailbox import get_code_outlook_pw
from common.cookies import save_platform_cookies
from common.agent_captcha import solve_puzzle_voting

try:
    from config import (CAPSOLVER_API_KEY, EZCAPTCHA_API_KEY, EZCAPTCHA_API_BASE,
                        YESCAPTCHA_API_KEY, YESCAPTCHA_API_BASE)
except Exception:
    CAPSOLVER_API_KEY = ""
    EZCAPTCHA_API_KEY = ""
    EZCAPTCHA_API_BASE = "https://api.ez-captcha.com"
    YESCAPTCHA_API_KEY = ""
    YESCAPTCHA_API_BASE = "https://api.yescaptcha.com"

PLATFORM = "github"
SIGNUP_URL = "https://github.com/signup"
# 登录态关键 cookie（GitHub 登录后种 user_session / logged_in=yes）
KEY_COOKIES = ["user_session", "__Host-user_session_same_site", "_gh_sess"]
REGISTER_TIMEOUT = 600
POOL_DIR = "_outlook_pool"
SCREENSHOT_DIR = "screenshots_github"

# GitHub 验证 = Arkose Labs FunCaptcha（实测抓到的固定参数）
# 触发: 填完表 -> idle 几秒等 enforcement 初始化 -> 点 Create account -> "Verify your account"
ARKOSE_PUBLIC_KEY = "747B83EC-2CA3-43AD-A7DF-701F286FBABA"
ARKOSE_API_SUBDOMAIN = "github-api.arkoselabs.com"

# GitHub 发件人 / launch code 邮件特征
GH_SENDER = ("github.com", "noreply@github.com", "notifications@github.com")
GH_SUBJECT = ("launch code", "github", "verify", "verification", "code")


def _solve_funcaptcha_yescaptcha(public_key, page_url, subdomain, blob=None, max_wait=200):
    """YesCaptcha 解 Arkose FunCaptcha，返回 token 或 None。
    API 与 CapSolver 兼容：createTask/getTaskResult，type=FunCaptchaTaskProxyless。
    blob = GitHub #funcaptcha 的 data-data-exchange-payload，必传，否则 token 不被 GitHub 接受。"""
    if not YESCAPTCHA_API_KEY:
        return None
    try:
        task = {
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websitePublicKey": public_key,
            "funcaptchaApiJSSubdomain": f"https://{subdomain}",
        }
        if blob:
            # data-exchange blob：YesCaptcha/CapSolver 都通过 task.data 传，值是 JSON 串 {"blob":"..."}
            task["data"] = json.dumps({"blob": blob})
        resp = requests.post(f"{YESCAPTCHA_API_BASE}/createTask",
                             json={"clientKey": YESCAPTCHA_API_KEY, "task": task}, timeout=30)
        data = resp.json()
        if data.get("errorId", 1) != 0:
            print(f"  [yescaptcha] create error: {data.get('errorDescription', data)}")
            return None
        task_id = data["taskId"]
        print(f"  [yescaptcha] funcaptcha task: {task_id}")
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(6)
            r = requests.post(f"{YESCAPTCHA_API_BASE}/getTaskResult",
                              json={"clientKey": YESCAPTCHA_API_KEY, "taskId": task_id}, timeout=30).json()
            st = r.get("status")
            if st == "ready":
                sol = r.get("solution", {})
                tok = sol.get("token") or sol.get("gRecaptchaResponse")
                print(f"  [yescaptcha] solved (token len={len(tok or '')})")
                return tok
            if st == "failed" or r.get("errorId"):
                print(f"  [yescaptcha] failed: {r.get('errorDescription', '')}")
                return None
        print("  [yescaptcha] timeout")
        return None
    except Exception as e:
        print(f"  [yescaptcha] error: {str(e)[:80]}")
        return None


def _solve_funcaptcha_capsolver(public_key, page_url, subdomain, blob=None, max_wait=180):
    """CapSolver 解 Arkose FunCaptcha，返回 token 或 None。
    GitHub 用 FunCaptchaTaskProxyLess（带 publicKey + apiJSSubdomain + data-exchange blob）。"""
    if not CAPSOLVER_API_KEY:
        return None
    try:
        task = {
            "type": "FunCaptchaTaskProxyLess",
            "websiteURL": page_url,
            "websitePublicKey": public_key,
            "funcaptchaApiJSSubdomain": f"https://{subdomain}",
        }
        if blob:
            task["data"] = json.dumps({"blob": blob})
        resp = requests.post("https://api.capsolver.com/createTask",
                             json={"clientKey": CAPSOLVER_API_KEY, "task": task}, timeout=30)
        data = resp.json()
        if data.get("errorId", 1) != 0:
            print(f"  [capsolver] create error: {data.get('errorDescription', data)}")
            return None
        task_id = data["taskId"]
        print(f"  [capsolver] funcaptcha task: {task_id}")
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(6)
            r = requests.post("https://api.capsolver.com/getTaskResult",
                              json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id}, timeout=30).json()
            st = r.get("status")
            if st == "ready":
                tok = r.get("solution", {}).get("token")
                print(f"  [capsolver] solved (token len={len(tok or '')})")
                return tok
            if st == "failed" or r.get("errorId"):
                print(f"  [capsolver] failed: {r.get('errorDescription', '')}")
                return None
        print("  [capsolver] timeout")
        return None
    except Exception as e:
        print(f"  [capsolver] error: {str(e)[:80]}")
        return None


def _solve_funcaptcha_ezcaptcha(public_key, page_url, subdomain, blob=None, max_wait=180):
    """EZ-Captcha 解 FunCaptcha（备用）。"""
    if not EZCAPTCHA_API_KEY:
        return None
    try:
        task = {
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websitePublicKey": public_key,
            "funcaptchaApiJSSubdomain": f"https://{subdomain}",
        }
        if blob:
            task["data"] = json.dumps({"blob": blob})
        resp = requests.post(f"{EZCAPTCHA_API_BASE}/createTask", json={
            "clientKey": EZCAPTCHA_API_KEY,
            "task": task,
        }, timeout=30)
        data = resp.json()
        if data.get("errorId", 1) != 0:
            print(f"  [ezcaptcha] create error: {data.get('errorDescription', data)}")
            return None
        task_id = data["taskId"]
        print(f"  [ezcaptcha] funcaptcha task: {task_id}")
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(6)
            r = requests.post(f"{EZCAPTCHA_API_BASE}/getTaskResult",
                              json={"clientKey": EZCAPTCHA_API_KEY, "taskId": task_id}, timeout=30).json()
            st = r.get("status")
            if st == "ready":
                tok = r.get("solution", {}).get("token")
                print(f"  [ezcaptcha] solved (token len={len(tok or '')})")
                return tok
            if st == "failed" or r.get("errorId"):
                print(f"  [ezcaptcha] failed: {r.get('errorDescription', '')}")
                return None
        print("  [ezcaptcha] timeout")
        return None
    except Exception as e:
        print(f"  [ezcaptcha] error: {str(e)[:80]}")
        return None


async def click_visual_puzzle(page, max_wait=50):
    """点 octocaptcha 里的 "Visual puzzle" 按钮。
    关键：这一步才会触发 loadFunCaptchaV2 —— 建立 Arkose onCompleted 回调（解完
    postMessage captcha-complete 给 GitHub）+ 创建 #funcaptcha 元素（带 data-target-origin）。

    实测时序坑：点 Create account 后 Arkose 先跑 ~16s "Verifying browser..."(proof-of-work)，
    之后才出现 "Verify your account / Visual puzzle / Audio puzzle" 选择页。所以这里要轮询
    足够久（默认 50s）等 PoW 跑完。按钮文本在最深的 Arkose game frame(index.html?session=...)
    里，用 get_by_text 跨 frame 找最稳。"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        for fr in page.frames:
            u = fr.url or ""
            if any(k in u for k in ["octocaptcha", "arkose", "funcaptcha"]):
                try:
                    el = fr.get_by_text("Visual puzzle", exact=False).first
                    if await el.count() > 0:
                        await el.click(timeout=4000)
                        print("  [arkose] clicked 'Visual puzzle'")
                        return True
                except Exception:
                    pass
        await asyncio.sleep(3)
    print("  [arkose] Visual puzzle 按钮没等到（可能已直接进拼图或免选择）")
    return False


async def solve_arkose(page, max_wait=200):
    """拿到 FunCaptcha token 并回灌。GitHub 的 Arkose token 要喂回 octocaptcha 的
    回调/隐藏字段，验证才算过。无打码 key 则返回 False（留给人工或被动过）。
    返回是否拿到并注入 token。优先 YesCaptcha（选定平台），CapSolver/ezCaptcha 兜底。"""
    if not (YESCAPTCHA_API_KEY or CAPSOLVER_API_KEY or EZCAPTCHA_API_KEY):
        print("  [arkose] 无打码 key（YESCAPTCHA/CAPSOLVER/EZCAPTCHA_API_KEY），跳过自动解码")
        return False
    # 先点 Visual puzzle，触发 loadFunCaptchaV2 -> 建立 onCompleted 回调 + #funcaptcha 元素
    await click_visual_puzzle(page)
    await asyncio.sleep(3)

    # 抽取 data-exchange blob —— GitHub 把它放在 #funcaptcha 的 data-data-exchange-payload，
    # 打码必须带上这个 blob，否则解出来的 token GitHub 不认（"挑战没通过"的根因）。
    blob = None
    for fr in page.frames:
        u = fr.url or ""
        if "octocaptcha" in u or "arkose" in u or "funcaptcha" in u:
            try:
                b = await fr.evaluate(
                    """() => { const el=document.querySelector('#funcaptcha');
                              return el ? (el.getAttribute('data-data-exchange-payload')||'') : ''; }"""
                )
                if b and b.strip():
                    blob = b.strip()
                    break
            except Exception:
                pass
    print(f"  [arkose] data-exchange blob: {'got len='+str(len(blob)) if blob else 'NONE (token 可能不被接受)'}")

    print(f"  [arkose] solving FunCaptcha (pk={ARKOSE_PUBLIC_KEY})...")
    loop = asyncio.get_event_loop()
    token = await loop.run_in_executor(
        None, _solve_funcaptcha_yescaptcha, ARKOSE_PUBLIC_KEY, SIGNUP_URL, ARKOSE_API_SUBDOMAIN, blob, max_wait)
    if not token:
        token = await loop.run_in_executor(
            None, _solve_funcaptcha_capsolver, ARKOSE_PUBLIC_KEY, SIGNUP_URL, ARKOSE_API_SUBDOMAIN, blob, max_wait)
    if not token:
        token = await loop.run_in_executor(
            None, _solve_funcaptcha_ezcaptcha, ARKOSE_PUBLIC_KEY, SIGNUP_URL, ARKOSE_API_SUBDOMAIN, blob, max_wait)
    if not token:
        print("  [arkose] 打码失败")
        return False
    # 回灌 token —— 精确点（从 octocaptcha 的 loadFunCaptchaV2 源码逆出）：
    # octocaptcha 正常流程是 Arkose onCompleted 时执行
    #     parent.postMessage({event:"captcha-complete", sessionToken: token}, target_origin)
    # target_origin = #funcaptcha 元素的 data-target-origin（即 https://github.com）。
    # 所以打码拿到 token 后，直接在 octocaptcha frame 里替它给 GitHub 父页发这条 message 即可。
    injected = False
    try:
        for fr in page.frames:
            if "octocaptcha" in (fr.url or ""):
                origin = await fr.evaluate(
                    """() => { const el=document.querySelector('#funcaptcha');
                              return el ? (el.getAttribute('data-target-origin')||'') : ''; }"""
                )
                origin = origin or "https://github.com"
                await fr.evaluate(
                    """([tok, org]) => {
                        parent.postMessage({event:"captcha-complete", sessionToken: tok}, org || "*");
                    }""", [token, origin]
                )
                print(f"  [arkose] posted captcha-complete to parent (origin={origin})")
                injected = True
                break
    except Exception as e:
        print(f"  [arkose] frame postMessage error: {str(e)[:80]}")

    if not injected:
        # 兜底：octocaptcha frame 没拿到，就直接向 GitHub 父页/所有 iframe 广播同格式 message
        try:
            await page.evaluate(
                """(tok) => {
                    const msg = {event:"captcha-complete", sessionToken: tok};
                    window.postMessage(msg, "*");
                    document.querySelectorAll('iframe').forEach(f => {
                        try { f.contentWindow.postMessage(msg, "*"); } catch(e){}
                    });
                }""", token)
            print("  [arkose] fallback: broadcast captcha-complete")
        except Exception as e:
            print(f"  [arkose] fallback inject error: {str(e)[:80]}")
    return True


def rand_password():
    # GitHub 要求 >=15 位，或 >=8 位含数字+小写。给足 16 位混合最稳。
    return "Gh1!" + "".join(random.choices(string.ascii_letters + string.digits, k=14))


def rand_username():
    # GitHub 用户名: 字母数字+连字符，不能以连字符开头/结尾，<=39 位
    adj = random.choice(["cool", "fast", "blue", "red", "neo", "sky", "dev", "byte", "code", "pixel"])
    noun = random.choice(["fox", "wolf", "cat", "owl", "bear", "hawk", "lion", "frog", "deer", "crab"])
    return f"{adj}{noun}{random.randint(1000, 9999)}"


def load_pool_accounts():
    """读 _outlook_pool/*.json -> [(email, password, cookies, refresh_token)]，最新优先。"""
    files = sorted(glob.glob(os.path.join(POOL_DIR, "*.json")), reverse=True)
    out = []
    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
            email = d.get("email")
            pw = d.get("password")
            if email and pw:
                out.append((
                    email,
                    pw,
                    d.get("outlook_cookies"),
                    d.get("refresh_token")  # 新增：读取 refresh_token
                ))
        except Exception:
            continue
    return out


async def dump_state(page, tag=""):
    """打印当前页面状态 + 截图，便于首跑适配 GitHub 真实布局。"""
    try:
        print(f"  --- state {tag} ---")
        print(f"  url: {page.url}")
        n = await page.locator("input").count()
        for i in range(min(n, 8)):
            el = page.locator("input").nth(i)
            try:
                print(f"    input[{i}] type={await el.get_attribute('type')} "
                      f"name={await el.get_attribute('name')} "
                      f"id={await el.get_attribute('id')} "
                      f"autocomplete={await el.get_attribute('autocomplete')}")
            except Exception:
                pass
        nb = await page.locator("button").count()
        btxt = []
        for i in range(min(nb, 12)):
            try:
                t = (await page.locator("button").nth(i).inner_text()).strip()[:30]
                if t:
                    btxt.append(t)
            except Exception:
                pass
        print(f"    buttons: {btxt}")
        body = (await page.locator("body").inner_text())[:280].replace("\n", " | ")
        print(f"    body: {body}")
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        await page.screenshot(path=f"{SCREENSHOT_DIR}/{tag or 'state'}.png")
    except Exception as e:
        print(f"  dump_state error: {e}")


async def fill_launch_code(page, code):
    """填写 GitHub launch code（支持单输入框和多输入框两种模式）。

    GitHub 验证码页面可能是：
      1. 单个输入框：直接填入完整验证码
      2. 多个输入框：每个输入框填一位数字（如 launch-code-0 到 launch-code-7，共8位）
    """
    code = str(code).strip()
    if not code or not code.isdigit():
        print(f"  [code] invalid code format: {code}")
        return False

    try:
        # 方案1：检测是否有多个独立输入框（GitHub 当前用的方式 - 8个输入框）
        multi_inputs = await page.locator('input[name="launch_code[]"]').all()

        if multi_inputs and len(multi_inputs) >= len(code):
            # 多输入框模式：每个框填一位
            print(f"  [code] detected {len(multi_inputs)} separate input boxes, filling {len(code)} digits")

            for i, digit in enumerate(code):
                if i >= len(multi_inputs):
                    break
                try:
                    input_box = multi_inputs[i]
                    await input_box.click()
                    await input_box.fill(digit)
                    await asyncio.sleep(0.3)
                except Exception as e:
                    print(f"  [code] error filling box {i}: {str(e)[:50]}")

            print(f"  [code] filled {len(code)} digits into separate boxes")

            # 等待一下让页面处理
            await asyncio.sleep(2)

            # 查找并点击 Continue 按钮
            try:
                continue_btn = page.get_by_role("button", name="Continue", exact=True).first
                if await continue_btn.count() > 0:
                    await continue_btn.click(timeout=5000)
                    print("  [code] clicked 'Continue' button")
                    await asyncio.sleep(3)
            except Exception as e:
                print(f"  [code] continue button error: {str(e)[:50]}")

            return True

        # 方案2：单输入框模式（备用）
        single_selectors = [
            'input[name="otp"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[type="text"][maxlength]'
        ]

        for sel in single_selectors:
            if await page.locator(sel).count() > 0:
                print(f"  [code] detected single input box: {sel}")
                ok = await react_fill(page, sel, code, tries=3)
                if ok:
                    print(f"  [code] filled code into single box")
                    await asyncio.sleep(2)
                    return True

        print("  [code] no matching input found")
        return False

    except Exception as e:
        print(f"  [code] fill error: {str(e)[:80]}")
        return False


async def detect_captcha(page, max_wait=20):
    """检测 Arkose FunCaptcha / octocaptcha 是否出现（轮询 page.frames）。
    实测：octocaptcha 是子 frame，主页面 body 文本不会变成 'verify your account'，
    所以靠 page.frames 里出现 octocaptcha/arkose 的 frame url 来判定，最可靠。"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            if any("octocaptcha" in (f.url or "") or "arkose" in (f.url or "") or "funcaptcha" in (f.url or "")
                   for f in page.frames):
                return True
            if await page.locator("iframe[src*=octocaptcha], iframe[src*=arkose]").count() > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(1.5)
    return False


async def detect_launch_code_page(page):
    """检测当前页面是否是 launch code / 验证码输入页"""
    try:
        # 方法1：检测 OTP/验证码输入框
        code_input_selectors = [
            'input[name="otp"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[type="text"][maxlength="6"]',  # 6位数字输入框
            'input[type="text"][maxlength="8"]',  # 8位数字输入框
        ]
        for sel in code_input_selectors:
            if await page.locator(sel).count() > 0:
                return True

        # 方法2：页面文本特征
        try:
            body = (await page.locator("body").inner_text())[:600].lower()
            # GitHub 验证页面的特征文本
            launch_code_keywords = [
                "launch code",
                "verification code",
                "we sent you",
                "we just sent",
                "enter the code",
                "check your email",
                "verify your account",
                "enter your code"
            ]
            if any(keyword in body for keyword in launch_code_keywords):
                return True
        except Exception:
            pass
    except Exception:
        pass

    return False


async def trigger_verify(page, max_clicks=4):
    """点 Create account 触发验证或直接跳转。
    GitHub 可能有两种流程：
      1. 弹出 Arkose 验证（返回 "CAPTCHA"）
      2. 直接跳转到验证页面如 account_verifications（返回 "LAUNCHED"）
    """
    initial_url = page.url

    for attempt in range(max_clicks):
        clicked = await click_create_account(page)
        if not clicked:
            # 按钮点击失败（可能表单不合法）
            continue

        # 等待8秒，同时检测多种情况
        deadline = time.time() + 8
        while time.time() < deadline:
            # 检测1：Arkose 验证是否出现
            try:
                if any("octocaptcha" in (f.url or "") or "arkose" in (f.url or "") or "funcaptcha" in (f.url or "")
                       for f in page.frames):
                    print(f"  [verify] octocaptcha triggered after {attempt+1} click(s)")
                    return "CAPTCHA"
                if await page.locator("iframe[src*=octocaptcha], iframe[src*=arkose]").count() > 0:
                    print(f"  [verify] octocaptcha iframe found after {attempt+1} click(s)")
                    return "CAPTCHA"
            except Exception:
                pass

            # 检测2：URL 是否变化（跳转到验证页面）
            try:
                current_url = page.url
                if current_url != initial_url:
                    print(f"  [verify] page navigated after {attempt+1} click(s): {current_url}")

                    # GitHub 验证页面的 URL 模式
                    if any(pattern in current_url for pattern in [
                        "account_verifications",  # 主要验证页
                        "check_email",            # 备用
                        "sessions/verified"       # 备用
                    ]):
                        print(f"  [verify] -> detected verification page")
                        return "LAUNCHED"

                    # 检测页面上是否有验证码输入框
                    if await detect_launch_code_page(page):
                        print(f"  [verify] -> detected launch code input")
                        return "LAUNCHED"

                    # 其他页面
                    print(f"  [verify] -> navigated to unknown page")
                    return "NAVIGATED"
            except Exception:
                pass

            # 检测3：即使 URL 没变，也检测输入框（SPA 可能不改 URL）
            try:
                if await detect_launch_code_page(page):
                    print(f"  [verify] -> detected launch code input after {attempt+1} click(s)")
                    return "LAUNCHED"
            except Exception:
                pass

            await asyncio.sleep(0.5)

    print(f"  [verify] no response after {max_clicks} clicks")
    return None


async def describe_captcha(page):
    """探测验证挑战的具体形态：列出所有 iframe 的 src/title，判断是哪家打码。"""
    try:
        frames = await page.evaluate(
            """() => [...document.querySelectorAll('iframe')].map(f => ({
                src: (f.src||'').slice(0,120), title: f.title||'', id: f.id||'',
                w: f.offsetWidth, h: f.offsetHeight
            }))"""
        )
        print("  [captcha] iframes on page:")
        for fr in frames:
            if fr["src"] or fr["title"] or fr["id"]:
                print(f"    - id={fr['id']} title={fr['title']!r} {fr['w']}x{fr['h']} src={fr['src']}")
        # 找出验证相关文案
        body = (await page.locator("body").inner_text())[:400]
        if any(k in body.lower() for k in ["verify", "puzzle", "captcha", "human", "robot"]):
            print(f"  [captcha] page text hint: {body[:200].strip()}")
    except Exception as e:
        print(f"  describe_captcha error: {e}")


async def fill_step(page, selector, value, label, settle=0.5):
    """填一个字段并回读校验（GitHub 表单是受控输入，沿用 react_fill 逻辑）。"""
    ok = await react_fill(page, selector, value, tries=3, settle=settle, verbose=False)
    print(f"  [form] {label}={value} -> {'OK' if ok else 'FAILED'}")
    return ok


async def select_country(page, country="United States of America"):
    """选 Country/Region 自定义下拉：点开下拉 -> 过滤框输入 -> 点国家项。
    GitHub 这是个自定义 button+listbox（非原生 select），国家项是带 id=item-* 的 button。"""
    try:
        # 打开下拉：通常是 label 'Your Country/Region' 旁的 button，或含 'Country' 的按钮
        opener = page.locator('button:has-text("Country"), button:has-text("Region"), [aria-label*="Country" i]').first
        if await opener.count() == 0:
            # 退化：找 combobox 角色
            opener = page.get_by_role("combobox").first
        if await opener.count() > 0:
            await opener.click(timeout=4000)
            await asyncio.sleep(1)
        # 过滤框
        filt = page.locator('input[placeholder*="Filter" i], input[aria-label*="Filter" i]').first
        if await filt.count() > 0:
            await filt.fill(country[:12])
            await asyncio.sleep(1)
        # 点国家项（按钮文本完全等于国家名）
        item = page.get_by_role("button", name=country, exact=True).first
        if await item.count() == 0:
            item = page.locator(f'button:has-text("{country}")').first
        if await item.count() > 0:
            await item.click(timeout=4000)
            print(f"  [form] country selected: {country}")
            await asyncio.sleep(1)
            return True
    except Exception as e:
        print(f"  [form] select_country failed: {str(e)[:70]}")
    return False


async def click_create_account(page):
    """提交注册：只认 'Create account'（绝不匹配顶部的 Continue with Google/Apple）。
    按钮在三字段+国家合法前是 disabled，故等它可用再点。
    返回 True 表示点击成功，False 表示超时/按钮不可用。"""
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            btn = page.get_by_role("button", name="Create account", exact=True)
            if await btn.count() > 0:
                b = btn.last  # 页面有 button 和 submit 两个同名，submit 是真正提交
                disabled = await b.get_attribute("disabled")
                aria = await b.get_attribute("aria-disabled")
                if disabled is None and aria != "true":
                    await b.click(timeout=6000)
                    print("  [form] clicked 'Create account'")
                    await asyncio.sleep(2)  # 点击后等2秒让页面响应
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)
    print("  [form] 'Create account' 超时或一直 disabled，可能某字段不合法")
    return False


async def register_one(email, password, cookies, p, refresh_token=None, auto=False, keep=True):
    start = time.time()

    def check_timeout():
        if time.time() - start > REGISTER_TIMEOUT:
            raise TimeoutError(f"timeout {REGISTER_TIMEOUT}s")

    gh_password = rand_password()
    username = rand_username()
    print(f"\n>>> github signup: email={email} user={username} pass={gh_password}")

    name = f"github_{time.strftime('%m%d_%H%M%S')}"
    bb = pid = None
    skip_variant = False
    try:
        bb, pid, browser, ctx, page = await open_and_connect(name=name, p=p)
        await ctx.clear_cookies()

        # Step 1: 打开注册页（带重试）
        print("  [1] goto signup")
        goto_ok = False
        for attempt in range(4):
            try:
                await page.goto(SIGNUP_URL, timeout=60000, wait_until="domcontentloaded")
                goto_ok = True
                break
            except Exception as e:
                print(f"  goto retry {attempt+1}/4: {str(e)[:70]}")
                await asyncio.sleep(4)
        if not goto_ok:
            print("  goto failed after retries")
            return None
        await asyncio.sleep(5)
        await dump_state(page, "01_after_load")

        # Step 2: 单页表单 —— Email + Password + Username 一起填
        print("  [2] fill single-page form")
        email_sel = 'input#email, input[name="user[email]"], input[type="email"]'
        if await page.locator(email_sel).count() == 0:
            print("  email input not found — GitHub 布局可能变了，dump 后停下")
            await dump_state(page, "02_no_email")
            return None
        await fill_step(page, email_sel, email, "email")

        pw_sel = 'input#password, input[name="user[password]"], input[type="password"]'
        await fill_step(page, pw_sel, gh_password, "password")

        # 用户名：填后 GitHub 异步校验可用性，重名则换
        user_sel = 'input#login, input[name="user[login]"]'
        for _ in range(3):
            await fill_step(page, user_sel, username, "username")
            await asyncio.sleep(2.5)
            body = (await page.locator("body").inner_text()).lower()
            if any(k in body for k in ["unavailable", "already taken", "not available", "is already"]):
                username = rand_username()
                print(f"  [2] username taken, retry -> {username}")
                continue
            break
        await dump_state(page, "03_form_filled")
        check_timeout()

        # 国家/地区下拉（不选则 Create account 保持 disabled）
        print("  [3] select country")
        await select_country(page, "United States of America")

        # marketing 勾选框：默认未勾，无需动；这里确保不勾（opt-out）
        try:
            cb = page.locator('input#user_signup\\[marketing_consent\\], input[name="user_signup[marketing_consent]"]').first
            if await cb.count() > 0 and await cb.is_checked():
                await cb.uncheck(timeout=3000)
        except Exception:
            pass
        await dump_state(page, "04_before_submit")
        check_timeout()

        # 关键：填完别急着点。GitHub 的 Arkose enforcement 脚本要几秒初始化。
        print("  [3.5] settling for Arkose enforcement to init...")
        await asyncio.sleep(10)

        # Step 4: 提交触发验证（可能直接跳转或弹出验证）
        print("  [4] click Create account -> trigger verify")
        verify_result = await trigger_verify(page)  # 返回 "CAPTCHA" / "LAUNCHED" / "NAVIGATED" / None
        await asyncio.sleep(3)
        await dump_state(page, "05_after_submit")
        check_timeout()

        # Step 5: 根据结果分流处理
        print("  [5] verification check")

        if verify_result == "CAPTCHA":
            # 情况1：出现 Arkose 验证
            print("  [!!!] Arkose 验证出现，启用视觉投票求解器")
            await dump_state(page, "06_CAPTCHA")
            if auto:
                # 视觉投票求解器：内部等 PoW→点 Visual puzzle→逐轮投票→提交
                solved = await solve_puzzle_voting(page, shot_dir=SCREENSHOT_DIR, max_rounds=12)
                if solved == "SKIP_VARIANT":
                    print("  [5] 遇到难变体(character)，本窗口作废，换一批验证")
                    skip_variant = True
                    return "SKIP_VARIANT"
                print(f"  [5] 验证结果: {'通过' if solved else '未通过'}")
                if solved:
                    await asyncio.sleep(6)
                    await dump_state(page, "06b_after_solve")
                else:
                    await dump_state(page, "06b_solve_failed")
                    return None
            else:
                print("  [explore] 停在验证步，窗口保留。")
                return "CAPTCHA_REACHED"

        elif verify_result == "LAUNCHED":
            # 情况2：直接跳转到验证码输入页（account_verifications 等）
            print("  [5] 直接跳转到验证码页，无需 Arkose 验证")
            print(f"  [5] current page: {page.url}")
            await dump_state(page, "06_no_captcha_direct_launch")

            if not auto:
                print("  [explore] --auto 未开，到此为止（保留窗口）。")
                return "FORM_DONE"

            # auto 模式：直接进入邮件验证码流程（跳到 Step 8）
            print("  [5] proceeding to email verification code...")

        elif verify_result == "NAVIGATED":
            # 情况3：跳转到其他页面
            print("  [5] 跳转到未知页面，检查状态")
            print(f"  [5] current page: {page.url}")
            await dump_state(page, "06_unknown_page")

            # 尝试检测是否有验证码输入框
            if await detect_launch_code_page(page):
                print("  [5] 检测到验证码输入框，继续流程")
                if not auto:
                    return "FORM_DONE"
            else:
                print("  [5] 未知页面状态，可能出错")
                return None

        else:
            # 情况4：提交失败或超时
            print("  [5] 提交失败，按钮未响应或页面未变化")
            await dump_state(page, "06_submit_failed")
            return None

        # 探索模式到此结束
        if not auto:
            print("  [explore] --auto 未开，到此为止（保留窗口）。")
            return "FORM_DONE"

        # ===== auto 模式：继续取邮件验证码 =====
        await asyncio.sleep(4)
        await dump_state(page, "07_before_code")
        check_timeout()

        # Step 8: 邮件 launch code（8位数字，GitHub 当前格式）
        print("  [8] waiting for GitHub launch code (smart mode: Graph API → browser fallback)...")
        mail_page = await ctx.new_page()
        try:
            # 使用智能选择：优先 Graph API，失败后降级到浏览器登录
            from common.mailbox import get_code_smart
            code = await get_code_smart(
                mail_page, email, password,
                refresh_token=refresh_token,  # 传入 refresh_token（可能为 None）
                sender_hint=("github", "noreply@github.com", "notifications"),
                subject_hint=("launch code", "github", "verify", "code"),
                code_regex=r"\b(\d{6,8})\b",
                max_wait=180,
                poll=8,
            )
        finally:
            try:
                await mail_page.close()
            except Exception:
                pass
        await page.bring_to_front()

        if code:
            print(f"  got launch code: {code}")
            # 使用新的填写函数，支持多输入框
            fill_ok = await fill_launch_code(page, code)
            if fill_ok:
                print("  [8] launch code filled successfully")
            else:
                print("  [8] launch code fill failed, check page structure")
            await asyncio.sleep(4)
            await dump_state(page, "09_after_code")
        else:
            print("  no launch code received")

        # Step 9: 跳主页确保 cookie 落域，保存
        try:
            await page.goto("https://github.com/", timeout=45000, wait_until="domcontentloaded")
            await asyncio.sleep(4)
        except Exception:
            pass
        await dump_state(page, "10_final")
        key_val, _ = await save_platform_cookies(
            ctx, PLATFORM, pid, email=email, password=gh_password, key_cookie_names=KEY_COOKIES
        )
        if key_val:
            print(f"  [OK] github session cookie saved")
            return key_val
        print("  [FAIL] no session cookie")
        return None

    except Exception as e:
        print(f"  ERROR: {e}")
        return None
    finally:
        # 探索默认保留窗口（keep=True）；但遇到难变体跳过时必须删窗口好换新的
        if bb and pid and (skip_variant or not keep):
            await teardown(bb, pid, delete=True)
        elif bb and pid:
            print(f"  [keep] 窗口保留: {name} (id={pid}) — 在 BitBrowser 里可手动操作")


async def main():
    parser = argparse.ArgumentParser(description="GitHub Auto Register (explore)")
    parser.add_argument("--email", default=None, help="指定邮箱（默认从 _outlook_pool 随机取）")
    parser.add_argument("--password", default=None, help="指定邮箱密码")
    parser.add_argument("--auto", action="store_true", help="尝试走完整流程（含取 launch code）")
    parser.add_argument("--no-keep", action="store_true", help="结束后删除窗口（默认保留以便研究）")
    parser.add_argument("--timeout", "-t", type=int, default=600)
    args = parser.parse_args()

    global REGISTER_TIMEOUT
    REGISTER_TIMEOUT = args.timeout

    if args.email:
        email, password, cookies, refresh_token = args.email, args.password or "", None, None
    else:
        accounts = load_pool_accounts()
        if not accounts:
            print(f"  没有可用邮箱：{POOL_DIR} 为空")
            return
        email, password, cookies, refresh_token = random.choice(accounts)
        print(f"  从池中随机选中: {email} (池中共 {len(accounts)} 个)")
        if refresh_token:
            print(f"  ✓ refresh_token available (will use Graph API)")
        else:
            print(f"  ✗ no refresh_token (will use browser login)")

    print("=" * 56)
    print(f"  GitHub Auto Register  auto={args.auto} keep={not args.no_keep}")
    print("=" * 56)

    async with async_playwright() as p:
        # 遇到难变体(character)自动换窗口重试，最多 N 次，赌到 sequence/rotate 变体
        max_attempts = 8
        result = None
        for attempt in range(1, max_attempts + 1):
            print(f"\n----- 尝试 {attempt}/{max_attempts} -----")
            result = await register_one(
                email, password, cookies, p,
                refresh_token=refresh_token,  # 传递 refresh_token
                auto=args.auto, keep=not args.no_keep,
            )
            if result != "SKIP_VARIANT":
                break
            print(f"  难变体跳过，{2}s 后换新窗口重试...")
            await asyncio.sleep(2)
            await asyncio.sleep(2)
    print(f"\n{'='*56}\n  result: {result}\n{'='*56}")


if __name__ == "__main__":
    asyncio.run(main())
