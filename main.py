import os
import re
import sys
import time
import qrcode
import shutil
from http.cookies import SimpleCookie
from email.utils import parsedate_to_datetime
from playwright.sync_api import sync_playwright

from secure_storage import load_token, save_token, state_dir

WIN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"

def launch_persistent_ctx(pw, reset=False, headless=True):
    user_data_dir = state_dir() / "browser-profile"
    if user_data_dir.is_symlink():
        raise RuntimeError("Refusing a symlinked browser profile")
    if reset and user_data_dir.exists():
        shutil.rmtree(user_data_dir)
    user_data_dir.mkdir(mode=0o700, exist_ok=True)
    user_data_dir.chmod(0o700)

    args = [
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,960",
    ]

    if headless:
        args += ["--headless=new", "--disable-gpu"]

    common_kwargs = dict(
        user_data_dir=user_data_dir,
        headless=headless,
        args=args,
        viewport={"width": 1280, "height": 960},
        locale="zh-CN",
        user_agent=WIN_UA,
        extra_http_headers={
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-mobile": "?0",
            "accept-language": "zh-CN,zh;q=0.9"
        },
    )

    return pw.chromium.launch_persistent_context(**common_kwargs)

def apply_windows_ua(ctx, page):
    page.add_init_script(f"""
        Object.defineProperty(navigator, 'userAgent', {{get: () => '{WIN_UA}' }});
        Object.defineProperty(navigator, 'platform', {{get: () => 'Win32' }});
        Object.defineProperty(navigator, 'vendor', {{get: () => 'Google Inc.' }});
        Object.defineProperty(navigator, 'maxTouchPoints', {{get: () => 0 }});
    """)

    try:
        s = ctx.new_cdp_session(page)
        s.send("Emulation.setUserAgentOverride", {
            "userAgent": WIN_UA,
            "platform": "Windows",
            "acceptLanguage": "zh-CN,zh;q=0.9"
        })
    except:
        pass

    ctx.set_extra_http_headers({
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
        "accept-language": "zh-CN,zh;q=0.9"
    })

def print_qr(data):
    qr = qrcode.QRCode(border=0)
    qr.add_data(data)
    qr.make(fit=True)
    m = qr.get_matrix()

    black = "  "
    white = "██"
    scale = 1
    margin = 1
    w = len(m[0])

    for _ in range(margin * scale):
        print(white * (w + margin * 2))

    for row in m:
        line = white * margin
        for v in row:
            line += (black if v else white) * scale
        line += white * margin
        for _ in range(scale):
            print(line)

    for _ in range(margin * scale):
        print(white * (w + margin * 2))

def get_token(reset=False, headless=True, timeout_seconds=300):
    if os.geteuid() == 0:
        raise PermissionError("Do not run the Binance login browser as root")
    if not sys.stdout.isatty():
        raise RuntimeError("Login requires an interactive terminal to display the one-time QR code")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    deadline = time.monotonic() + timeout_seconds
    csrftoken = ""
    p20t = ""
    expirationTimestamp = -1
    with sync_playwright() as pw:
        ctx = launch_persistent_ctx(pw, reset=reset, headless=headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        apply_windows_ua(ctx, page)

        qr_results = set()

        def update_p20t_from_context():
            try:
                cookies = ctx.cookies("https://www.binance.com")
                c = next((c for c in cookies if c.get("name") == "p20t"), None)
                token = c.get("value", "")
                if not token: return
                nonlocal p20t
                p20t = token
                nonlocal expirationTimestamp
                expires = c.get("expires")
                # Playwright reports -1 for session cookies. Never invent a lifetime.
                if isinstance(expires, (int, float)) and expires > time.time():
                    expirationTimestamp = int(expires)
            except:
                pass

        def on_request(req):
            try:
                url = req.url
                if "https://www.binance.com/bapi/compliance/v2/private/compliance/account/login" in url:
                    token = req.headers.get("csrftoken", "")
                    if not token: return
                    nonlocal csrftoken
                    csrftoken = token
            except:
                pass

        def on_request_finished(req):
            try:
                url = req.url
                if "https://accounts.binance.com/bapi/accounts/v3/public/qrcode/login/get" in url:
                    resp = req.response()
                    if not resp: return
                    data = resp.json()
                    if not data.get("success"): return
                    code = data["data"]["qrCode"]
                    if code not in qr_results:
                        qr_results.add(code)
                        print("请使用 Binance App 扫描以下二维码登录")
                        print_qr(code)
                elif "https://accounts.binance.com/bapi/accounts/v2/private/authcenter/setTrustDevice" in url:
                    resp = req.response()
                    if not resp: return
                    hdrs_arr = resp.headers_array()
                    date_hdr = resp.headers.get("date") or resp.header_value("date")
                    if not hdrs_arr or not date_hdr: return
                    sc_values = [h.get("value", "") for h in hdrs_arr if h.get("name", "").lower() == "set-cookie"]
                    m = next((m for sc in sc_values for m in SimpleCookie(sc).values() if m.key == "p20t"), None)
                    if not m: return
                    nonlocal p20t, expirationTimestamp
                    p20t = m.value
                    expirationTimestamp = int(m["max-age"]) + int(parsedate_to_datetime(date_hdr).timestamp())
            except:
                pass

        page.on("request", on_request)
        page.on("requestfinished", on_request_finished)
        ctx.on("request", on_request)
        ctx.on("requestfinished", on_request_finished)

        page.goto("https://accounts.binance.com/zh-CN/login?loginChannel=&return_to=", wait_until="domcontentloaded", timeout=30000)

        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Binance login timed out. No new session confirmed; try interactive QR login again."
                )
            page.wait_for_timeout(1500)

            if csrftoken and p20t:
                token_dict = {"csrftoken": csrftoken, "p20t": p20t, "expirationTimestamp": expirationTimestamp}
                if expirationTimestamp == -1:
                    try:
                        old = load_token()
                        if old.get("p20t") == p20t and old.get("csrftoken") == csrftoken:
                            token_dict["expirationTimestamp"] = old.get("expirationTimestamp", -1)
                    except FileNotFoundError:
                        pass
                save_token(token_dict)
                print("登录凭证已安全保存到私有状态目录；不会打印 Token。")
                ctx.close()
                return

            try:
                if "accounts.binance.com" in page.url:
                    if page.get_by_role("button", name=re.compile("Understand")).count() > 0:
                        page.get_by_role("button", name=re.compile("Understand")).first.click(timeout=1200, force=True)

                    if page.get_by_role("button", name=re.compile("知道了")).count() > 0:
                        page.get_by_role("button", name=re.compile("知道了")).first.click(timeout=1200, force=True)

                    if page.get_by_role("button", name=re.compile(r"^好的$")).count() > 0:
                        page.get_by_role("button", name=re.compile(r"^好的$")).first.click(timeout=1200, force=True)

                    if page.get_by_role("button", name=re.compile("已知晓")).count() > 0:
                        page.get_by_role("button", name=re.compile("已知晓")).first.click(timeout=1200, force=True)

                    if page.get_by_role("button", name=re.compile(r"^二维码登录$")).count() > 0 and page.get_by_text(re.compile("扫码登录")).count() == 0:
                        page.get_by_role("button", name=re.compile(r"^二维码登录$")).first.click(timeout=1200, force=True)

                    if page.get_by_role("button", name=re.compile(r"^刷新二维码$")).count() > 0:
                        page.get_by_role("button", name=re.compile(r"^刷新二维码$")).first.click(timeout=1200, force=True)

                    if page.get_by_role("button", name=re.compile(r"^登录失败$")).count() > 0:
                        page.get_by_role("button", name=re.compile(r"^登录失败$")).first.click(timeout=1200, force=True)

                    if page.get_by_role("button", name=re.compile(r"^登录$")).count() > 0 and page.get_by_text(re.compile(r"^邮箱/手机号码$")).count() > 0 and page.get_by_text(re.compile(r"^用手机相机扫描$")).count() == 0:
                        page.get_by_role("button", name=re.compile(r"^登录$")).first.click(timeout=1200, force=True)

                    if page.get_by_role("button", name=re.compile(r"^是$")).count() > 0:
                        page.get_by_role("button", name=re.compile(r"^是$")).first.click(timeout=1200, force=True)
                else:
                    update_p20t_from_context()
            except:
                pass

if __name__ == "__main__":
    get_token(reset=False, headless=True)

