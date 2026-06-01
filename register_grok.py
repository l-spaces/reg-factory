# -*- coding: utf-8 -*-
"""
Grok (x.ai) 自动注册
关键: grok.com 有 Cloudflare 全页拦截，必须走 Clash 干净节点(换节点绕过)。

流程: 切Clash节点 -> BitBrowser走代理 -> grok.com -> 新規登録 -> accounts.x.ai
       -> メールで登録 -> 填邮箱 -> 邮件验证码(浏览器登录Outlook) -> 保存 cookie

界面是日文(节点地区导致)，按钮文本用 日文+英文 双匹配。

用法:
    python register_grok.py --count 1
    python register_grok.py --count 5 --node "美国 02"
"""

import argparse
import asyncio
import random
import string
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, ".")
from playwright.async_api import async_playwright

from bitbrowser import BitBrowser
from common.browser import inject_stealth, create_browser_with_retry, human_type
from common.mailbox import get_code_outlook_pw
from common.cookies import save_platform_cookies
from common import emails as email_pool
from common import proxy_switch

PLATFORM = "grok"
GROK_URL = "https://grok.com/"
CLASH_PROXY_HOST = "127.0.0.1"
CLASH_PROXY_PORT = "7897"
# 登录态关键 cookie（运行时确认，先放候选）
KEY_COOKIES = ["sso", "sso-rw", "__Secure-next-auth.session-token", "auth_token"]
REGISTER_TIMEOUT = 600
KEEP_ON_FAIL = False
FIXED_EMAIL = None
FIXED_PASSWORD = None

# 注册方式按钮（中文+日文+英文，不同节点地区界面语言不同）
SIGNUP_BTN = ["新規登録", "注册", "註冊", "Sign up", "サインアップ", "注册账号"]
EMAIL_SIGNUP_BTN = ["メールで登録", "用邮箱注册", "使用邮箱注册", "邮箱注册", "用電子郵件註冊", "Sign up with email", "Continue with email", "メールアドレスで登録", "使用电子邮件"]
CONTINUE_BTN = ["続行", "继续", "繼續", "Continue", "次へ", "下一步", "Next", "Sign up", "登録", "注册", "Verify", "確認", "确认", "验证"]
COOKIE_DISMISS = ["すべて拒否する", "全部拒絕", "全部拒绝", "拒绝所有", "Reject all", "接受所有 Cookie", "Accept all", "すべて許可する", "全部允許", "拒否", "同意"]
# 提交验证码按钮
VERIFY_BTN = ["メールを確認", "確認", "确认", "验证邮件", "验证", "驗證", "Verify", "Verify email", "続行", "继续", "Continue", "Submit"]
# 完成注册按钮（x.ai 验证码后的 givenName/familyName/password/Turnstile 页）
COMPLETE_BTN = ["登録を完了", "アカウントを作成", "Complete registration", "Complete sign up",
                "Create account", "Sign up", "完成注册", "完成註冊", "完成", "完了", "Done",
                "登録", "サインアップ", "Continue", "続行", "继续", "Next", "次へ"]

GROK_SENDER = ("x.ai", "grok", "noreply", "no-reply")
GROK_SUBJECT = ("code", "verify", "verification", "grok", "x.ai", "confirm", "確認", "認証", "コード", "验证", "驗證")


def rand_password():
    return "Aa1!" + "".join(random.choices(string.ascii_letters + string.digits, k=12))


async def wait_render(page, max_s=70):
    """grok 走代理渲染慢(可达30-40s)，轮询到出现交互元素"""
    for i in range(max_s // 3):
        await asyncio.sleep(3)
        try:
            cnt = await page.evaluate("() => document.querySelectorAll('button,input,textarea,a').length")
        except Exception:
            cnt = 0
        if cnt > 3:
            print(f"  SPA rendered ~{i*3}s (interactive={cnt})")
            return True
    print("  SPA render timeout")
    return False


async def click_any(page, labels, timeout=5000):
    """点任一匹配文本的按钮/链接(日文+英文)"""
    for label in labels:
        try:
            b = page.locator(f'button:has-text("{label}"), a:has-text("{label}"), [role=button]:has-text("{label}")').first
            if await b.count() > 0:
                await b.click(timeout=timeout)
                return label
        except Exception:
            pass
    return None


async def _wait_turnstile(page, max_s=90):
    """等 Cloudflare Turnstile token：hidden input[name=cf-turnstile-response] 有值即过。
    返回是否拿到 token。

    坑：x.ai 用的是**托管(managed)模式** Turnstile，多数情况无需交互、自己后台校验 IP/指纹后
    自动填 token —— 此时千万别去点它（旧实现无脑点 iframe 的 body/label，反而可能打断校验、
    且点了也拿不到 token，直接 'continuing anyway' 带空 token 提交 -> '完成注册' 被拦在原页）。
    正确做法：先耐心轮询 token；**只有**真出现可见复选框(交互式 challenge)才点一次，点完继续等。
    token 能否拿到强依赖出口 IP 信誉：数据中心节点常被 CF 判定需挑战甚至拿不到 token。"""
    clicked = False
    deadline = time.time() + max_s
    while time.time() < deadline:
        try:
            val = await page.evaluate(
                "() => { const e=document.querySelector('input[name=\"cf-turnstile-response\"],textarea[name=\"cf-turnstile-response\"]'); return e ? e.value : null; }"
            )
        except Exception:
            val = None
        if val:
            print(f"  [turnstile] passed (token len={len(val)})")
            return True
        # 仅当出现真正可见的复选框（交互式 challenge）才点一次；managed 模式没有复选框，不动它
        if not clicked:
            try:
                for fr in page.frames:
                    if "challenges.cloudflare.com" in (fr.url or ""):
                        cb = fr.locator('input[type="checkbox"]')
                        if await cb.count() > 0 and await cb.first.is_visible():
                            await cb.first.click(timeout=3000)
                            print("  [turnstile] clicked interactive checkbox")
                            clicked = True
                        break
            except Exception:
                pass
        await asyncio.sleep(2)
    print("  [turnstile] token NOT obtained (IP 可能被 CF 判定需挑战；换节点重试)")
    return False


async def dump_state(page, tag=""):
    try:
        info = await page.evaluate("""() => ({
            btns:[...document.querySelectorAll('button')].map(b=>b.innerText.trim()).filter(t=>t).slice(0,15),
            inputs:[...document.querySelectorAll('input,textarea')].map(i=>i.type+'/'+(i.placeholder||i.name||'')),
            url:location.href
        })""")
        print(f"  --- state {tag} ---")
        print(f"    url: {info['url']}")
        print(f"    btns: {info['btns']}")
        print(f"    inputs: {info['inputs']}")
    except Exception as e:
        print(f"  dump_state err: {e}")


async def get_code_via_direct_browser(email, email_pw, p):
    """单开一个 noproxy BitBrowser 窗口(本机直连)登录 Outlook 取验证码。
    注册浏览器走代理过 Grok CF，但 Outlook 界面走代理刷不出，故取信用直连。"""
    import os
    if os.environ.get("MAILBOX_BROKER"):
        # broker 模式：委托共享取码服务，不另开浏览器（Grok 用 outlook 注定超时，timeout 调短减少拖累）
        from common.mailbox import fetch_from_broker
        return await fetch_from_broker(
            email, email_pw, GROK_SENDER, GROK_SUBJECT,
            r"\b((?=[A-Z0-9-]*[A-Z])[A-Z0-9]{2,4}-[A-Z0-9]{2,4})\b", "code",
            int(os.environ.get("GROK_BROKER_TIMEOUT", "40")),
        )
    bb = BitBrowser()
    pid = None
    try:
        pid = create_browser_with_retry(bb, f"mail_{time.strftime('%H%M%S')}")
        if not pid:
            return None
        bb._post("/browser/update", {
            "id": pid, "proxyMethod": 2, "proxyType": "noproxy",
            "browserFingerPrint": {"coreVersion": "130"},
        })
        data = None
        for _ in range(8):
            try:
                data = bb.open_browser(pid)
                break
            except Exception:
                await asyncio.sleep(4)
        if not data:
            return None
        browser = await p.chromium.connect_over_cdp(data["ws"])
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await inject_stealth(ctx, page)
        return await get_code_outlook_pw(
            page, email, email_pw,
            sender_hint=GROK_SENDER, subject_hint=GROK_SUBJECT,
            code_regex=r"\b((?=[A-Z0-9-]*[A-Z])[A-Z0-9]{2,4}-[A-Z0-9]{2,4})\b", max_wait=160, poll=8,
        )
    except Exception as e:
        print(f"  [mail] direct browser error: {e}")
        return None
    finally:
        if pid:
            try:
                bb.close_browser(pid)
            except Exception:
                pass
            await asyncio.sleep(2)
            try:
                bb.delete_browser(pid)
            except Exception:
                pass


async def register_one(index, total, p, node):
    start = time.time()

    def check_timeout():
        if time.time() - start > REGISTER_TIMEOUT:
            raise TimeoutError(f"timeout {REGISTER_TIMEOUT}s")

    if FIXED_EMAIL:
        email, email_pw, refresh_token, client_id = FIXED_EMAIL, FIXED_PASSWORD, "", ""
    else:
        em = email_pool.next_email(PLATFORM)
        if not em:
            print("  no email available")
            return None
        email, email_pw, refresh_token, client_id = em
    password = rand_password()
    print(f"\n#{index}/{total} email={email}")

    name = f"grok_{time.strftime('%m%d_%H%M%S')}_{index}"
    bb = BitBrowser()
    pid = None
    success = False
    try:
        # BitBrowser 走 Clash 代理
        pid = create_browser_with_retry(
            bb, name,
        )
        if not pid:
            print("  create browser failed")
            return None
        # 重新用代理配置更新窗口
        bb._post("/browser/update", {
            "id": pid, "name": name, "proxyMethod": 2, "proxyType": "http",
            "host": CLASH_PROXY_HOST, "port": CLASH_PROXY_PORT,
            "browserFingerPrint": {"coreVersion": "130"},
        })
        data = None
        for _ in range(8):
            try:
                data = bb.open_browser(pid)
                break
            except Exception:
                await asyncio.sleep(4)
        if not data:
            print("  open browser failed")
            return None

        browser = await p.chromium.connect_over_cdp(data["ws"])
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await inject_stealth(ctx, page)

        # Step 1: 打开 grok.com，等渲染
        print("  [1] goto grok.com (via proxy node)")
        for attempt in range(3):
            try:
                await page.goto(GROK_URL, timeout=60000, wait_until="domcontentloaded")
                break
            except Exception as e:
                print(f"  goto retry {attempt+1}: {str(e)[:50]}")
                await asyncio.sleep(4)
        await wait_render(page)
        check_timeout()

        # 关 cookie 弹窗
        await asyncio.sleep(2)
        dismissed = await click_any(page, COOKIE_DISMISS, timeout=3000)
        if dismissed:
            print(f"  cookie banner dismissed: {dismissed}")
            await asyncio.sleep(2)

        # Step 2: 点 新規登録
        print("  [2] click signup")
        clicked = await click_any(page, SIGNUP_BTN, timeout=6000)
        if not clicked:
            print("  signup button not found")
            await dump_state(page, "no-signup")
            email_pool.mark_error(PLATFORM, email, email_pw, "no_signup_btn")
            return None
        await asyncio.sleep(6)  # 跳 accounts.x.ai
        await wait_render(page, max_s=40)
        await dump_state(page, "after-signup")
        check_timeout()

        # Step 3: 选 メールで登録 (email signup)
        print("  [3] choose email signup")
        clicked = await click_any(page, EMAIL_SIGNUP_BTN, timeout=6000)
        if clicked:
            print(f"  clicked: {clicked}")

        # 等邮箱输入框出现：grok 经代理 SPA 渲染慢(可达30-40s)，点完不能立即判定。
        # 坑1：OneTrust Cookie 横幅会遮挡并拦截 '用邮箱注册' 点击，导致页面不跳转——每轮先关横幅。
        # 坑2：横幅里有个隐藏搜索框 input#vendor-search-handler(placeholder=搜索...)，会污染
        #      input[type=text] 兜底选择器、点它直接 30s 超时——故排除它并只取可见的输入框。
        email_sel = ('input[type="email"], input[name="email"], input[autocomplete="email"], '
                     'input[type="text"]:not([name="vendor-search-handler"])'
                     ':not([placeholder*="搜索"]):not([placeholder*="検索"]):not([placeholder*="search" i])'
                     ':not([aria-label*="Cookie"]):not([aria-label*="搜索"])')

        async def _visible_email():
            loc = page.locator(email_sel)
            for j in range(await loc.count()):
                el = loc.nth(j)
                try:
                    if await el.is_visible():
                        return el
                except Exception:
                    pass
            return None

        email_input = None
        for i in range(16):  # ~50s
            await click_any(page, COOKIE_DISMISS, timeout=2000)  # 关 Cookie 横幅（拦截点击）
            email_input = await _visible_email()
            if email_input:
                break
            await asyncio.sleep(3)
            if i in (4, 9):  # 横幅关掉后补点邮箱注册（首次点击可能被横幅吃掉）
                again = await click_any(page, EMAIL_SIGNUP_BTN, timeout=4000)
                if again:
                    print(f"  re-clicked: {again}")
        await dump_state(page, "email-method")

        # Step 4: 填邮箱
        print("  [4] fill email")
        if email_input:
            await email_input.click()
            await email_input.fill(email)
            await asyncio.sleep(1)
            await click_any(page, CONTINUE_BTN, timeout=5000)
            await asyncio.sleep(5)
        else:
            print("  email input not found")
            await dump_state(page, "no-email-input")
            email_pool.mark_error(PLATFORM, email, email_pw, "no_email_input")
            return None
        await dump_state(page, "after-email")
        check_timeout()

        # Step 5: 邮件验证码
        # 关键架构：注册浏览器走代理(过Grok CF)，但 Outlook 界面走代理刷不出来，
        # 所以取信单开一个 noproxy 的 BitBrowser 窗口(本机直连)读邮件。
        print("  [5] get verification code via separate noproxy Outlook window")
        code = await get_code_via_direct_browser(email, email_pw, p)

        if code:
            print(f"  got code: {code}")
            # 精确定位验证码框(name=code)，避免误填到搜索框(text/検索)
            ci = page.locator('input[name="code"]').first
            if await ci.count() == 0:
                ci = page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]').first
            if await ci.count() == 0:
                # 兜底：排除搜索框的 text input
                ci = page.locator('input[type="text"]:not([placeholder*="検索"]):not([name="vendor-search-handler"])').first
            if await ci.count() > 0:
                await ci.click()
                await ci.fill("")
                # 逐字符输入触发 React onChange（fill 直接 setValue 不触发，x.ai 表单识别不到）
                await ci.type(code, delay=120)
                await asyncio.sleep(1.5)
                # 先试回车提交（验证码框常回车即提交），再点确认按钮
                try:
                    await ci.press("Enter")
                    await asyncio.sleep(2)
                except Exception:
                    pass
                submitted = await click_any(page, VERIFY_BTN, timeout=5000)
                print(f"  提交验证码按钮: {submitted}")
                await asyncio.sleep(6)
            await dump_state(page, "after-code")
        else:
            print("  no code received")
            email_pool.mark_error(PLATFORM, email, email_pw, "no_code")

        # Step 6: 完成注册页（x.ai 新流程：givenName/familyName + password + Cloudflare Turnstile + 登録を完了）
        def _rand_word():
            return random.choice("BCDFGHJKLMNPQRST") + "".join(random.choices("aeiou", k=1)) \
                   + "".join(random.choices(string.ascii_lowercase, k=random.randint(3, 6)))

        gname = page.locator('input[name="givenName"]').first
        fname = page.locator('input[name="familyName"]').first
        if await gname.count() > 0:
            first, last = _rand_word().capitalize(), _rand_word().capitalize()
            try:
                await gname.click(); await gname.type(first, delay=60)
                if await fname.count() > 0:
                    await fname.click(); await fname.type(last, delay=60)
                print(f"  [6] name: {first} {last}")
            except Exception as e:
                print(f"  [6] name fill err: {str(e)[:50]}")

        pw_input = page.locator('input[type="password"]').first
        if await pw_input.count() > 0:
            print("  [6] set password")
            try:
                await pw_input.click(); await pw_input.type(password, delay=50)
            except Exception:
                await pw_input.fill(password)
            await asyncio.sleep(1)

        # 等 Turnstile token + 点完成注册：拿到 token 才点（空 token 提交必被拦在原页）。
        # 最多 3 轮：每轮先耐心等 token，再点完成；若仍停在 accounts.x.ai/sign-up 说明没过，
        # 回到循环继续等 token（managed 模式有时晚到）再重点。
        completed = False
        for attempt in range(3):
            has_token = await _wait_turnstile(page, max_s=60)
            done = await click_any(page, COMPLETE_BTN, timeout=8000)
            print(f"  [6] complete: btn={done} turnstile={has_token} (attempt {attempt+1}/3)")
            await asyncio.sleep(6)
            cur = page.url
            # 离开 sign-up 页 = 注册推进成功
            if "/sign-up" not in cur:
                completed = True
                break
            await dump_state(page, f"after-complete-{attempt+1}")
            check_timeout()
        if not completed:
            print("  [6] 仍停在 sign-up 页（Turnstile 未过 / 提交被拦）")
        check_timeout()

        # 回到 grok.com 确保 cookie 落到主域
        try:
            await page.goto("https://grok.com/", timeout=45000, wait_until="domcontentloaded")
            await wait_render(page, max_s=40)
        except Exception:
            pass
        await dump_state(page, "final")

        key_val, _ = await save_platform_cookies(
            ctx, PLATFORM, pid, email=email, password=password, key_cookie_names=KEY_COOKIES
        )
        if key_val:
            email_pool.mark_used(PLATFORM, email, email_pw)
            success = True
            print("  [OK] session cookie saved")
            return key_val
        else:
            print("  [FAIL] no session cookie")
            email_pool.mark_error(PLATFORM, email, email_pw, "no_session_cookie")
            return None

    except Exception as e:
        print(f"  ERROR: {e}")
        if email:
            email_pool.mark_error(PLATFORM, email, email_pw, str(e)[:50])
        return None
    finally:
        if pid:
            keep = KEEP_ON_FAIL and not success
            try:
                bb.close_browser(pid)
            except Exception:
                pass
            await asyncio.sleep(2)
            if not keep:
                try:
                    bb.delete_browser(pid)
                except Exception:
                    pass
            else:
                print(f"  [debug] window kept: {name} (id={pid})")


async def main():
    parser = argparse.ArgumentParser(description="Grok Auto Register")
    parser.add_argument("--count", "-n", type=int, default=1)
    parser.add_argument("--concurrency", "-c", type=int, default=1)
    parser.add_argument("--timeout", "-t", type=int, default=600)
    parser.add_argument("--node", default="auto", help="Clash 出口节点(过grok CF)")
    parser.add_argument("--keep-on-fail", action="store_true")
    parser.add_argument("--email", default=None, help="指定邮箱(绕过邮箱池)")
    parser.add_argument("--password", default=None, help="指定邮箱密码")
    args = parser.parse_args()

    global REGISTER_TIMEOUT, KEEP_ON_FAIL, FIXED_EMAIL, FIXED_PASSWORD
    REGISTER_TIMEOUT = args.timeout
    KEEP_ON_FAIL = args.keep_on_fail
    FIXED_EMAIL = args.email
    FIXED_PASSWORD = args.password

    print("=" * 50)
    print(f"  Grok Auto Register  count={args.count} node={args.node}")
    print("=" * 50)

    # 选节点过 grok CF：--node 指定则用它，否则自动探测能过的节点
    try:
        if args.node and args.node.lower() != "auto":
            proxy_switch.set_node(args.node)
            time.sleep(2)
            print(f"  使用指定节点 -> {proxy_switch.current_node()}")
        else:
            print("  自动探测能过 grok CF 的节点...")
            node = proxy_switch.find_working_node(test_url="https://grok.com/")
            if not node:
                print("  没找到能过 grok CF 的节点(可能 CF 高防护时段，稍后重试)")
                return
            print(f"  选用节点: {node}")
    except Exception as e:
        print(f"  切节点失败(确认 Clash 在跑): {e}")
        return

    sem = asyncio.Semaphore(args.concurrency)
    results = []

    async def run_one(i):
        async with sem:
            if i > 1:
                await asyncio.sleep(random.uniform(3, 8) * (i - 1))
            async with async_playwright() as p:
                try:
                    sk = await register_one(i, args.count, p, args.node)
                    results.append(sk)
                except Exception as e:
                    print(f"  #{i} fatal: {e}")
                    results.append(None)

    await asyncio.gather(*[run_one(i) for i in range(1, args.count + 1)])

    ok = sum(1 for r in results if r)
    print(f"\n{'='*50}\n  success: {ok}/{len(results)}\n{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
