"""Breeze login flow -> session token.

The supported Breeze flow is:
  1. open https://api.icicidirect.com/apiuser/login?api_key=<url-encoded key>
  2. sign in with the ICICI Direct user id + password, then the OTP sent to you
  3. ICICI redirects to the redirect URL registered for your app with
     ?apisession=<SESSION_TOKEN> appended
  4. that token + API secret open the session (BreezeConnect.generate_session)

`browser_login` automates steps 1-3 with Playwright: it fills the user id and
password from .env, asks you for the OTP in the terminal, submits it and
captures the token from the redirect. If the page layout differs from what the
selectors expect, it falls back to letting you finish in the same browser
window and still captures the redirect. `manual_login` needs no browser
automation at all: you log in yourself and paste the redirected URL.
"""
from __future__ import annotations

import getpass
import time
from urllib.parse import parse_qs, quote_plus, urlparse

from ..config import Credentials
from ..log import get_logger, register_secret

log = get_logger("auth")

LOGIN_URL = "https://api.icicidirect.com/apiuser/login?api_key={api_key}"

USER_SELECTORS = ["#txtuid", "input[name='txtuid']", "input[placeholder*='User' i]", "input[type='text']"]
PASS_SELECTORS = ["#txtPass", "input[name='txtPass']", "input[type='password']"]
TNC_SELECTORS = ["#chkssTnc", "input[type='checkbox']"]
LOGIN_SELECTORS = ["#btnSubmit", "input[type='submit']", "button[type='submit']", "button:has-text('Login')"]
OTP_SELECTORS = ["#pnlOTP input[type='text']:visible", "#pnlOTP input:visible", "input[tg-nm='otp']:visible",
                 "input[id*='otp' i]:visible", "input[name*='otp' i]:visible", "input[maxlength='1']:visible"]
OTP_SUBMIT_SELECTORS = ["#Button1", "#btnSubmitOTP", "#pnlOTP input[type='submit']", "#pnlOTP button",
                        "button:has-text('Submit')", "input[value*='Submit' i]"]


class LoginError(RuntimeError):
    pass


def login_url(api_key: str) -> str:
    return LOGIN_URL.format(api_key=quote_plus(api_key))


def extract_session_token(text: str) -> str | None:
    """Accept either a bare token or any URL containing apisession=..."""
    text = (text or "").strip()
    if not text:
        return None
    if "apisession" in text:
        qs = parse_qs(urlparse(text).query)
        vals = qs.get("apisession") or parse_qs(text.split("?", 1)[-1]).get("apisession")
        return vals[0].strip() if vals else None
    if text.isalnum():
        return text
    return None


def manual_login(creds: Credentials) -> str:
    creds.require_api()
    print("\nOpen this URL in your browser and log in (user id, password, OTP):\n")
    print("   ", login_url(creds.api_key), "\n")
    print("After login ICICI redirects to your app's redirect URL, e.g. http://localhost:3000/?apisession=12345678")
    print("The page itself may fail to load; that is fine. Copy the full address from the address bar.\n")
    for _ in range(3):
        token = extract_session_token(getpass.getpass("Paste the redirected URL (or just the apisession value): "))
        if token:
            register_secret(token)
            return token
        print("Could not find an apisession value in that input, try again.")
    raise LoginError("No session token entered")


def _first(page, selectors, timeout_ms=4000):
    """First selector that matches a visible element, or None."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    return loc
            except Exception:
                continue
        time.sleep(0.25)
    return None


def browser_login(creds: Credentials, headless: bool = False, timeout_s: int = 300) -> str:
    creds.require_login()
    for s in (creds.password, creds.api_secret):
        register_secret(s)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LoginError("Playwright is not installed. Run: pip install -r requirements.txt && "
                         "python3 -m playwright install chromium") from exc

    captured: dict[str, str] = {}

    def watch(url: str):
        if "apisession=" in url and "token" not in captured:
            tok = extract_session_token(url)
            if tok:
                captured["token"] = tok
                register_secret(tok)

    with sync_playwright() as p:
        browser = None
        for kwargs in ({"channel": "chrome"}, {}):   # prefer installed Google Chrome, else bundled Chromium
            try:
                browser = p.chromium.launch(headless=headless, **kwargs)
                break
            except Exception as exc:
                log.debug("Browser launch %s failed: %s", kwargs or "chromium", exc)
        if browser is None:
            raise LoginError("Could not start a browser. Run: python3 -m playwright install chromium")
        context = browser.new_context()
        context.on("request", lambda req: watch(req.url))
        page = context.new_page()
        page.on("framenavigated", lambda frame: watch(frame.url))

        log.info("Opening the ICICI Direct Breeze login page")
        page.goto(login_url(creds.api_key), wait_until="domcontentloaded")
        body = page.inner_text("body")[:300] if page.locator("body").count() else ""
        if "Public Key does not exist" in body:
            browser.close()
            raise LoginError("ICICI says 'Public Key does not exist': BREEZE_API_KEY in .env is wrong")

        automated = False
        user = _first(page, USER_SELECTORS)
        pwd = _first(page, PASS_SELECTORS, 1000)
        if user and pwd:
            user.first.fill(creds.user_id)
            pwd.first.fill(creds.password)
            tnc = _first(page, TNC_SELECTORS, 500)
            if tnc and not tnc.first.is_checked():
                tnc.first.check()
            btn = _first(page, LOGIN_SELECTORS, 1000)
            if btn:
                btn.first.click()
                automated = True
                log.info("Submitted user id and password; waiting for the OTP screen")

        otp_boxes = _first(page, OTP_SELECTORS, 20000) if automated else None
        if otp_boxes is not None and "token" not in captured:
            otp = ""
            while not (otp.isdigit() and 4 <= len(otp) <= 8):
                otp = input("Enter the OTP sent to your registered mobile/email: ").strip()
            register_secret(otp)
            n = otp_boxes.count()
            if n >= len(otp):
                for i, ch in enumerate(otp):
                    otp_boxes.nth(i).fill(ch)
            else:
                otp_boxes.first.fill(otp)
            submit = _first(page, OTP_SUBMIT_SELECTORS, 2000)
            if submit:
                submit.first.click()
            else:
                page.keyboard.press("Enter")
            log.info("OTP submitted; waiting for the redirect with the session token")
        elif "token" not in captured:
            print("\nCould not drive the login form automatically. Please finish logging in "
                  "(user id, password, OTP) in the browser window that just opened.\n"
                  f"Waiting up to {timeout_s}s for the redirect...")

        deadline = time.monotonic() + timeout_s
        while "token" not in captured and time.monotonic() < deadline:
            try:
                watch(page.url)
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
            if not browser.is_connected():
                break
        try:
            browser.close()
        except Exception:
            pass

    if "token" not in captured:
        raise LoginError("Did not receive a session token. Check the OTP, or run with --manual.")
    return captured["token"]
