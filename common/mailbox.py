# -*- coding: utf-8 -*-
"""
common/mailbox.py — 通用 Outlook 取信（Graph API + 浏览器兜底，均扫 inbox + junk）

支持两种提取目标：
  - magic link（Claude 用）
  - 验证码 code（ChatGPT / Grok 用 6 位数字）

emails.txt 行格式: email----password----refresh_token----client_id
"""

import os
import re
import sys
import time
import asyncio

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import requests

DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
GRAPH_FOLDERS = ["inbox", "junkemail"]


def _get_access_token(refresh_token, client_id=DEFAULT_CLIENT_ID, scope="https://graph.microsoft.com/Mail.Read"):
    try:
        resp = requests.post(
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            data={
                "client_id": client_id or DEFAULT_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": scope,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  [mail] token refresh failed: {resp.status_code} {resp.text[:120]}")
            return None
        return resp.json().get("access_token")
    except Exception as e:
        print(f"  [mail] token error: {e}")
        return None


def fetch_messages(access_token, folder, top=10):
    """拉取某文件夹最新邮件，返回 [{subject, from, body, received}]"""
    headers = {"Authorization": f"Bearer {access_token}"}
    url = (
        f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages"
        f"?$top={top}&$orderby=receivedDateTime desc"
        f"&$select=subject,from,body,bodyPreview,receivedDateTime"
    )
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return []
        out = []
        for m in r.json().get("value", []):
            out.append({
                "subject": m.get("subject", ""),
                "from": (m.get("from", {}).get("emailAddress", {}) or {}).get("address", ""),
                "body": (m.get("body", {}) or {}).get("content", "") or m.get("bodyPreview", ""),
                "received": m.get("receivedDateTime", ""),
            })
        return out
    except Exception as e:
        print(f"  [mail] fetch {folder} error: {e}")
        return []


def get_code_by_token(
    email,
    refresh_token,
    client_id=DEFAULT_CLIENT_ID,
    sender_contains=("openai.com", "anthropic", "x.ai", "grok"),
    subject_contains=("code", "verify", "verification", "confirm", "登录", "验证"),
    code_regex=r"\b(\d{6})\b",
    max_wait=120,
    poll=5,
):
    """轮询 inbox+junk，匹配发件人/主题后用正则提取验证码。返回 code 字符串或 None。
    sender_contains / subject_contains 任一命中即视为目标邮件（宽松匹配，二者满足其一）。"""
    token = _get_access_token(refresh_token, client_id)
    if not token:
        return None

    pat = re.compile(code_regex)
    start = time.time()
    while time.time() - start < max_wait:
        for folder in GRAPH_FOLDERS:
            for m in fetch_messages(token, folder, top=10):
                subj = (m["subject"] or "").lower()
                frm = (m["from"] or "").lower()
                hit_sender = any(s.lower() in frm for s in sender_contains) if sender_contains else False
                hit_subject = any(s.lower() in subj for s in subject_contains) if subject_contains else False
                if not (hit_sender or hit_subject):
                    continue
                # 优先在主题里找验证码（很多服务把 code 放主题），再到正文
                for text in (m["subject"] or "", m["body"] or ""):
                    mm = pat.search(text)
                    if mm:
                        code = _first_group(mm)
                        print(f"  [mail] code found in {folder} (from={m['from']}): {code}")
                        return code
        elapsed = int(time.time() - start)
        print(f"  [mail] waiting for code (inbox+junk)... ({elapsed}s/{max_wait}s)")
        # token 可能在长轮询中过期，过半时刷新一次
        if elapsed > max_wait // 2 and elapsed % (poll * 4) < poll:
            nt = _get_access_token(refresh_token, client_id)
            if nt:
                token = nt
        time.sleep(poll)

    print("  [mail] timeout, no code found")
    return None


def get_link_by_token(
    email,
    refresh_token,
    client_id=DEFAULT_CLIENT_ID,
    link_regex=r"https://[^\s\"'<>]+",
    sender_contains=("openai.com", "anthropic", "x.ai", "grok"),
    subject_contains=("verify", "confirm", "sign in", "magic", "login", "登录", "验证"),
    must_contain=None,
    max_wait=120,
    poll=5,
):
    """轮询 inbox+junk，匹配邮件后提取链接（可用 must_contain 过滤目标链接，如 'verify_email'）。"""
    token = _get_access_token(refresh_token, client_id)
    if not token:
        return None
    pat = re.compile(link_regex)
    start = time.time()
    while time.time() - start < max_wait:
        for folder in GRAPH_FOLDERS:
            for m in fetch_messages(token, folder, top=10):
                subj = (m["subject"] or "").lower()
                frm = (m["from"] or "").lower()
                hit = (any(s.lower() in frm for s in sender_contains) if sender_contains else False) or \
                      (any(s.lower() in subj for s in subject_contains) if subject_contains else False)
                if not hit:
                    continue
                for link in pat.findall(m["body"] or ""):
                    if must_contain and must_contain not in link:
                        continue
                    print(f"  [mail] link found in {folder}: {link[:80]}...")
                    return link
        elapsed = int(time.time() - start)
        print(f"  [mail] waiting for link (inbox+junk)... ({elapsed}s/{max_wait}s)")
        time.sleep(poll)
    print("  [mail] timeout, no link found")
    return None


# ========== 浏览器登录取信（refresh_token 失效时的兜底，已验证可用）==========

async def _outlook_login(page, email, password):
    """用邮箱密码登录 Outlook。返回是否成功进入邮箱。
    已验证：登录后可能跳 passkey 设置页，直接导航 inbox URL 可绕过。"""
    try:
        await page.goto("https://login.live.com/", timeout=60000)
        await asyncio.sleep(3)
        ei = page.locator('input[type="email"], input[name="loginfmt"]').first
        if await ei.count() > 0:
            await ei.fill(email)
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")
            await asyncio.sleep(3)
        pi = page.locator('input[type="password"], input[name="passwd"]').first
        if await pi.count() > 0:
            await pi.fill(password)
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")
            await asyncio.sleep(5)

        # 密码提交后会出现若干中间页，逐个处理（轮询几轮，每轮点掉一个）：
        #  - "保持登录状态吗?" (Stay signed in / 保持登录): 点 是/Yes/继续/Continue
        #  - passkey 设置页 (Set up passkey / パスキー): 点 跳过/Skip/稍后/Not now
        #  - 其它确认页: 点 继续/确认/OK
        affirm = ["Yes", "是", "はい", "Sí", "Continue", "继续", "繼續", "確認", "确认", "OK", "Accept", "确定"]
        skip = ["Skip", "跳过", "跳過", "Not now", "稍后", "稍後", "後で", "Maybe later", "今はしない", "暂时跳过"]
        for _ in range(4):
            await asyncio.sleep(2)
            # 已经进入邮箱就停
            if "outlook" in (page.url or "") and "live.com/mail" in (page.url or ""):
                break
            body = ""
            try:
                body = (await page.locator("body").inner_text())[:300].lower()
            except Exception:
                pass
            # passkey/设置页优先点跳过
            clicked = False
            if any(k in body for k in ["passkey", "パスキー", "通行密钥", "密钥", "set up", "设置"]):
                for label in skip:
                    b = page.locator(f'button:has-text("{label}"), a:has-text("{label}")').first
                    if await b.count() > 0:
                        try:
                            await b.click(timeout=3000); clicked = True; await asyncio.sleep(2); break
                        except Exception:
                            pass
            if not clicked:
                for label in affirm:
                    b = page.locator(f'button:has-text("{label}"), input[value="{label}"]').first
                    if await b.count() > 0:
                        try:
                            await b.click(timeout=3000); clicked = True; await asyncio.sleep(2); break
                        except Exception:
                            pass
            if not clicked:
                # 没有可点的中间页按钮，尝试直接去邮箱
                break
        return True
    except Exception as e:
        print(f"  [mail-pw] login error: {e}")
        return False


async def _click_folder(page, folder_names):
    """点击左侧导航的文件夹（收件箱/垃圾邮件）触发列表加载。
    关键：Outlook 直接 goto junkemail URL 会得到空列表，必须点文件夹。"""
    try:
        clicked = await page.evaluate(
            """(names) => {
                const els = [...document.querySelectorAll('div[draggable=true], span, div[role=treeitem], [title], [role=option]')];
                for (const e of els) {
                    const t = (e.textContent || '').trim();
                    if (names.some(n => t === n || t.startsWith(n))) { e.click(); return t.slice(0, 30); }
                }
                return null;
            }""",
            folder_names,
        )
        return clicked
    except Exception:
        return None


def _first_group(m):
    """取正则第一个非空捕获组（支持 (A|B) 多格式 alternation 正则）。"""
    if not m:
        return None
    for g in m.groups():
        if g:
            return g
    return m.group(0)


async def _scan_current_folder(page, pat, sender_hint, subject_hint):
    """在当前已打开的 Outlook 文件夹里找目标邮件并提验证码。
    策略（从稳到松，应对'邮件就在垃圾箱却读不到'）：
      1) 直接扫列表项预览文本，命中码正则就返回（Outlook 预览常含 'code is 123456'）；
      2) 点开发件人/主题命中的最新一封，读正文取码；
      3) 仍无命中则点开最顶部(最新)一封兜底 —— 刚触发发送，最新即目标。"""
    hints = [h.lower() for h in (sender_hint + subject_hint)]
    # 等邮件列表渲染（[role=option] 是收件箱+垃圾箱通用的邮件项）
    for _ in range(8):
        await asyncio.sleep(2)
        try:
            n = await page.evaluate("() => document.querySelectorAll('[role=\"option\"]').length")
        except Exception:
            n = 0
        if n > 0:
            break
    else:
        return None

    # 1) 预览文本直接取码（顶部最新几封，避免抓到旧码）
    try:
        previews = await page.evaluate(
            """() => [...document.querySelectorAll('[role="option"]')].slice(0,5)
                     .map(it => (it.textContent||''))"""
        )
    except Exception:
        previews = []
    for txt in previews:
        m = pat.search(txt)
        if m:
            return _first_group(m)

    # 2)/3) 选要点开的邮件：优先发件人/主题命中的最新一封，否则点最顶部(最新)一封
    try:
        clicked = await page.evaluate(
            """(hints) => {
                const items = [...document.querySelectorAll('[role="option"]')];
                for (const it of items) {  // 列表按时间倒序，第一封命中即最新
                    const t = (it.textContent || '').toLowerCase();
                    if (hints.some(h => h && t.includes(h))) { it.click(); return (it.textContent||'').slice(0,80); }
                }
                if (items.length) { items[0].click(); return '[fallback-newest] ' + (items[0].textContent||'').slice(0,60); }
                return null;
            }""",
            hints,
        )
    except Exception:
        clicked = None
    if not clicked:
        return None

    # 等阅读窗格渲染，从正文(优先 [role=main] 阅读窗格)取码
    for _ in range(6):
        await asyncio.sleep(2)
        try:
            body = await page.evaluate(
                "() => { const m=document.querySelector('[role=main]'); return (m?m.innerText:document.body.innerText)||''; }"
            )
        except Exception:
            body = ""
        m = pat.search(body)
        if m:
            return _first_group(m)
    return None


async def fetch_from_broker(email, password, sender_hint, subject_hint, regex, kind, timeout):
    """broker 模式：把取码委托给共享取码服务 mailbox_broker（设了环境变量 MAILBOX_BROKER 时启用）。
    返回 code/link 字符串或 None。三个注册脚本并行时靠它共用一个 Outlook 会话、避免并发登录被拦。"""
    base = os.environ.get("MAILBOX_BROKER")
    if not base:
        return None
    import aiohttp
    url = base.rstrip("/") + "/fetch"
    payload = {
        "email": email, "password": password,
        "sender_hint": list(sender_hint), "subject_hint": list(subject_hint),
        "regex": regex, "kind": kind, "timeout": timeout,
    }
    try:
        cfg = aiohttp.ClientTimeout(total=timeout + 60)
        async with aiohttp.ClientSession(timeout=cfg) as sess:
            async with sess.post(url, json=payload) as resp:
                data = await resp.json()
        val = data.get("value")
        if val:
            print(f"  [broker] got {kind}: {val[:50]}")
        else:
            print(f"  [broker] no {kind} ({data.get('error', 'timeout')})")
        return val
    except Exception as e:
        print(f"  [broker] fetch error: {e}")
        return None


async def get_code_outlook_pw(
    page,
    email,
    password,
    sender_hint=("openai", "anthropic", "grok", "x.ai", "noreply", "no-reply"),
    subject_hint=("code", "verify", "verification", "openai", "chatgpt", "confirm", "验证"),
    code_regex=r"\b(\d{6})\b",
    max_wait=150,
    poll=8,
):
    """浏览器登录 Outlook 取 6 位验证码（refresh_token 失效时用）。
    通过点击左侧文件夹切换 inbox/junk（直接 goto junk URL 列表为空）。
    page: BitBrowser 里新开的一个标签。返回 code 或 None。"""
    if os.environ.get("MAILBOX_BROKER"):
        return await fetch_from_broker(email, password, sender_hint, subject_hint, code_regex, "code", max_wait)
    if not await _outlook_login(page, email, password):
        return None
    pat = re.compile(code_regex)
    # 进收件箱让整个邮箱界面完整加载一次
    try:
        await page.goto("https://outlook.live.com/mail/0/", timeout=60000)
        await asyncio.sleep(6)
    except Exception:
        pass

    INBOX_NAMES = ["收件箱", "Inbox", "受信トレイ"]
    JUNK_NAMES = ["垃圾邮件", "Junk Email", "Junk", "迷惑メール"]

    start = time.time()
    while time.time() - start < max_wait:
        # 收件箱
        await _click_folder(page, INBOX_NAMES)
        await asyncio.sleep(2)
        code = await _scan_current_folder(page, pat, sender_hint, subject_hint)
        if code:
            print(f"  [mail-pw] code found in inbox: {code}")
            return code
        # 垃圾箱（点击文件夹触发加载）
        await _click_folder(page, JUNK_NAMES)
        await asyncio.sleep(2)
        code = await _scan_current_folder(page, pat, sender_hint, subject_hint)
        if code:
            print(f"  [mail-pw] code found in junk: {code}")
            return code
        elapsed = int(time.time() - start)
        print(f"  [mail-pw] waiting for code (inbox+junk)... ({elapsed}s/{max_wait}s)")
        await asyncio.sleep(poll)
    print("  [mail-pw] timeout, no code found")
    return None
