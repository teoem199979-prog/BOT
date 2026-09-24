"""
🌐 BROWSER ENGINE — trình duyệt (Playwright/Edge CDP) cho các domain KHÔNG có
API client riêng: MM88, RR88, XX88, O8, GG88.

Đây là bản khôi phục/thu gọn từ bot đời trước (trước khi migrate sang
browser-only — giữ lại phần cần cho các domain cấu hình:
  - Kết nối Edge đang chạy sẵn qua CDP (KHÔNG launch Chromium riêng)
  - TabPool: mỗi domain giữ (các) tab riêng, không tự mở tràn lan
  - Tìm ô nhập tài khoản/code, bấm nút submit, đọc kết quả (nhiều tầng
    fallback: selector riêng domain → selector chung SweetAlert/toast →
    quét từ khoá toàn trang → diff text trước/sau khi bấm)
  - Chụp màn hình + lưu HTML khi kết quả không rõ ràng (SCREENSHOT_ON_UNKNOWN)
    để dễ debug khi giao diện site đã đổi so với lúc code cũ chạy.

Module này CỐ TÌNH không import main_script.py ở cấp module (tránh import
vòng, vì main_script.py phải import module này để gọi). Vài hàm dùng
DEFERRED IMPORT (import main_script bên trong thân hàm) để gọi ngược lại
các tiện ích đã có sẵn ở đó (append_code_history, client Telegram) — an
toàn vì lúc các hàm này thực sự được GỌI, main_script đã import xong.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import gc
import random
import re as _re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from config import Config
from logger_setup import logger
from media_helpers import take_result_screenshot
from submission_outcomes import record_outcome, classify_result
from browser_site_profiles import browser_domains, get_site_profile

# ============================================================
# DOMAIN SCOPE
# Browser automation remains on one asyncio event loop. Do not call
# Playwright Page/Browser objects from raw threading.Thread workers; the
# TabPool locks and per-domain async workers provide safe parallelism instead.
# ============================================================
BROWSER_DOMAINS = browser_domains()


def _normalize_domain(url: str) -> str:
    p = urlparse(url or "")
    return (p.netloc or p.path).lower().replace("www.", "").strip("/")


def _append_code_history_safe(**kwargs):
    """Deferred import wrapper — gọi append_code_history() thật của
    main_script.py mà không cần import nó ở cấp module."""
    try:
        import main_script as _ms
        _ms.append_code_history(**kwargs)
    except Exception as e:
        logger.debug(f"⚠️ [Browser] append_history lỗi: {e}")


# ============================================================
# STATE
# ============================================================
class BrowserState:
    def __init__(self):
        self.account_pages: dict = {}       # key "domain|user" -> Page
        self.context_locks: dict = {}
        self.cf_verified: dict = {}
        self.submission_count: dict = {}
        self._input_cache: dict = {}
        self._input_cache_ttl: float = 20.0
        self._submits_since_full_reload: dict = {}
        self.is_running = True


bot_state = BrowserState()


def shutdown():
    bot_state.is_running = False


# ============================================================
# SELECTORS
# ============================================================
def _get_domain_username_selectors(domain: str) -> list:
    profile = get_site_profile(domain)
    return list(profile.username_selectors) if profile else []


def _get_domain_result_selectors(domain: str) -> list:
    profile = get_site_profile(domain)
    return list(profile.result_selectors) if profile else []


CF_SELECTORS = [
    "iframe[src*='turnstile']",
    "iframe[src*='challenges.cloudflare.com']",
    ".cf-turnstile",
    "[data-sitekey]",
]

REACT_FILL_JS = """
    ([el, val]) => {
        const proto = el.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        el.focus();
        setter.call(el, '');
        setter.call(el, val);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    }
"""

REACT_FILL_VERIFY_JS = """
    ([userEl, codeEl, userVal, codeVal, fillUser]) => {
        const setVal = (el, val) => {
            const proto = el.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            el.focus();
            setter.call(el, '');
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        };
        if (userEl && fillUser) setVal(userEl, userVal);
        setVal(codeEl, codeVal);
        return {
            actualUser: userEl ? userEl.value : null,
            actualCode: codeEl.value,
        };
    }
"""

_MANUAL_VERIFY_KEYWORDS = [
    "mã xác thực", "ma xac thuc",
    "nhập đúng mã trong ảnh", "nhap dung ma trong anh",
    "hoàn tất xác minh", "hoan tat xac minh",
    "nhập mã xác nhận", "nhap ma xac nhan",
    "kéo thanh trượt", "keo thanh truot",
    "hoàn thành ghép", "hoan thanh ghep",
]


def _needs_manual_verify(text: str) -> bool:
    if not text:
        return False
    low = text.strip().lower()
    return any(k in low for k in _MANUAL_VERIFY_KEYWORDS)


# ============================================================
# EDGE CDP CONNECT / LAUNCH
# ============================================================
_pw_instance = None
_edge_browser = None
_shared_context = None
_browser_lock = None
_last_launch_time: float = 0.0
_LAUNCH_COOLDOWN = 15.0


def _get_launch_lock():
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


def _kill_all_msedge():
    try:
        import psutil
        killed = 0
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info.get("name") or "").lower() != "msedge.exe":
                    continue
                proc.kill()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return killed
    except ImportError:
        return 0
    except Exception:
        return 0


def _get_edge_bot_profile_dir() -> str:
    custom_dir = getattr(Config, "EDGE_PROFILE_DIR", "") or ""
    if custom_dir.strip():
        return custom_dir.strip()
    return str(Path(__file__).resolve().parent / "edge_bot_profile")


def _launch_edge_debug(cdp_port: int) -> bool:
    exe = getattr(Config, "EDGE_EXECUTABLE_PATH", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

    if not Path(exe).exists():
        alt = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        if Path(alt).exists():
            logger.warning(f"⚠️ [Edge-CDP] EDGE_EXECUTABLE_PATH không tồn tại ({exe}) — dùng: {alt}")
            exe = alt
        else:
            logger.critical(
                f"❌ [Edge-CDP] Không tìm thấy msedge.exe ở cả 2 vị trí:\n"
                f"   - {exe}\n   - {alt}\n👉 Kiểm tra lại EDGE_EXECUTABLE_PATH trong .env"
            )
            return False

    profile_dir = _get_edge_bot_profile_dir()
    profile_name = getattr(Config, "EDGE_PROFILE_NAME", "") or "Default"

    try:
        import subprocess
        subprocess.Popen(
            [
                exe,
                f"--remote-debugging-port={cdp_port}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-allow-origins=http://localhost:{cdp_port},http://127.0.0.1:{cdp_port}",
                "--disable-blink-features=AutomationControlled",
                f"--user-data-dir={profile_dir}",
                f"--profile-directory={profile_name}",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--disable-component-update",
                "--disable-domain-reliability",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--no-first-run",
                "--no-default-browser-check",
                "--mute-audio",
                "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,AutofillServerCommunication",
            ],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return True
    except Exception as e:
        logger.error(f"❌ [Edge-CDP] Không tự mở lại được Edge ({exe}): {e}")
        return False


async def get_or_launch_browser_context(user: str = "shared", force_reconnect: bool = False):
    global _pw_instance, _edge_browser, _shared_context, _last_launch_time

    if not force_reconnect and _shared_context is not None:
        try:
            _ = _shared_context.pages
            return _shared_context
        except Exception:
            _shared_context = None

    async with _get_launch_lock():
        if not force_reconnect and _shared_context is not None:
            try:
                _ = _shared_context.pages
                return _shared_context
            except Exception:
                _shared_context = None

        if _pw_instance is None:
            _pw_instance = await async_playwright().start()

        cdp_port = getattr(Config, "EDGE_CDP_PORT", 9222)
        cdp_url = f"http://127.0.0.1:{cdp_port}"

        logger.info(f"[Edge-CDP] Đang kết nối vào Edge đang chạy tại {cdp_url}...")
        _edge_browser = None
        last_err = None

        now = time.time()
        just_launched = (now - _last_launch_time) < _LAUNCH_COOLDOWN
        max_attempts = 1 if just_launched else 2

        for attempt in range(1, max_attempts + 1):
            try:
                _edge_browser = await _pw_instance.chromium.connect_over_cdp(cdp_url, timeout=15000)
                break
            except Exception as e:
                last_err = e
                if just_launched:
                    wait_left = max(1.0, _LAUNCH_COOLDOWN - (time.time() - _last_launch_time))
                    logger.warning(
                        f"⚠️ [Edge-CDP] Kết nối thất bại nhưng Edge vừa được mở lại gần đây "
                        f"({wait_left:.0f}s trước) — ĐỢI THÊM thay vì kill lại: {e}"
                    )
                    await asyncio.sleep(min(wait_left + 3.0, 15.0))
                    just_launched = False
                    continue

                logger.warning(
                    f"⚠️ [Edge-CDP] Kết nối thất bại (lần {attempt}/{max_attempts}): {e} — "
                    f"không đóng các tiến trình Edge hiện có; thử mở Edge debug riêng..."
                )
                await asyncio.sleep(1)
                if _launch_edge_debug(cdp_port):
                    _last_launch_time = time.time()
                    logger.info("[Edge-CDP] Đã gửi lệnh mở lại Edge — chờ khởi động...")
                    await asyncio.sleep(10)

        if _edge_browser is None:
            logger.critical(
                f"❌ [Edge-CDP] Không kết nối được tới Edge ({cdp_url}) sau khi đã thử tự mở lại: {last_err}\n"
                f"👉 Có thể do: Edge bị chặn bởi firewall/antivirus trên cổng {getattr(Config, 'EDGE_CDP_PORT', 9222)}, "
                f"hoặc \"Tiếp tục chạy ứng dụng nền\" đang bật trong edge://settings/system."
            )
            raise last_err if last_err else RuntimeError("Edge CDP connect failed")

        if not _edge_browser.contexts:
            logger.error(
                "❌ [Edge-CDP] Kết nối được nhưng Edge không có context/tab nào đang mở — "
                "mở ít nhất 1 tab trong Edge trước khi chạy bot."
            )
            raise RuntimeError("Edge CDP: no existing browser context")

        _shared_context = _edge_browser.contexts[0]
        logger.info(f"[Edge-CDP] ✅ Đã kết nối — dùng context hiện có ({len(_shared_context.pages)} tab đang mở)")
        return _shared_context


_browser_hwnd: int = 0
_last_restore_at: float = 0.0


def _find_browser_hwnd() -> int:
    global _browser_hwnd
    if _browser_hwnd:
        if ctypes.windll.user32.IsWindow(_browser_hwnd):
            return _browser_hwnd
        _browser_hwnd = 0

    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def _cb(hwnd, _):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.lower()
        if any(k in title for k in ("edge", "microsoft edge", "chrome")):
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 100 and h > 50:
                found.append((w * h, hwnd))
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if not found:
        return 0
    found.sort(key=lambda x: x[0], reverse=True)
    _browser_hwnd = found[0][1]
    return _browser_hwnd


def edge_restore():
    global _last_restore_at
    try:
        now = time.monotonic()
        if now - _last_restore_at < 2.0:
            return
        _last_restore_at = now
        hwnd = _find_browser_hwnd()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            logger.debug("🔼 Edge restored")
    except Exception as e:
        logger.debug(f"browser_restore error: {e}")


# ============================================================
# CLOUDFLARE DETECTION
# ============================================================
async def _cf_already_passed(page, domain: str = "") -> bool:
    try:
        passed = await page.evaluate(
            """
            () => {
                const text = (document.body.innerText || '').toLowerCase();
                const successMarkers = ['thành công', 'thanh cong', 'xác thực thành công', 'verified', 'success'];
                return successMarkers.some(m => text.includes(m));
            }
            """
        )
        if passed:
            return True
    except Exception:
        pass

    try:
        btn_state = await page.evaluate(
            """
            () => {
                const buttons = [...document.querySelectorAll('button')];
                for (const btn of buttons) {
                    const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                    if (txt === 'xác thực' || txt === 'xac thuc') {
                        return !btn.disabled;
                    }
                }
                return null;
            }
            """
        )
        if btn_state is True:
            return True
        if btn_state is False:
            return False
    except Exception:
        pass

    return False


async def is_cloudflare_present(page, domain: str = "") -> bool:
    try:
        verify_button_visible = await page.evaluate(
            """
            () => {
                const buttons = [...document.querySelectorAll('button')];
                for (const btn of buttons) {
                    const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                    if (txt === 'xác thực' || txt === 'xac thuc') {
                        const rect = btn.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    }
                }
                return false;
            }
            """
        )
    except Exception:
        verify_button_visible = False

    if verify_button_visible:
        return True

    cf_widget_found = False
    for sel in CF_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await safe_is_visible(el):
                cf_widget_found = True
                break
        except Exception:
            pass

    if cf_widget_found:
        if await _cf_already_passed(page, domain=domain):
            pass
        else:
            return True

    try:
        load_failed = await page.evaluate(
            """
            () => {
                const markers = ['không tải được captcha', 'khong tai duoc captcha', 'error', 'thử lại', 'thu lai'];
                const text = (document.body.innerText || '').toLowerCase();
                return markers.some(m => text.includes(m)) &&
                       (text.includes('captcha') || text.includes('turnstile') || text.includes('cloudflare'));
            }
            """
        )
        if load_failed:
            return True
    except Exception:
        pass

    return False


async def safe_is_visible(element) -> bool:
    try:
        return await element.is_visible()
    except Exception:
        return False


def safe_is_closed(page) -> bool:
    try:
        if page is None:
            return True
        return page.is_closed()
    except Exception:
        return True


# ============================================================
# INPUT FIELDS
# ============================================================
def _invalidate_input_cache(key: str):
    bot_state._input_cache.pop(key, None)


async def find_input_fields(page, cache_key: str = None, domain: str = ""):
    now = time.time()

    if cache_key:
        cached = bot_state._input_cache.get(cache_key)
        if cached:
            username_input, code_input, cache_time = cached
            if now - cache_time < bot_state._input_cache_ttl:
                try:
                    if code_input:
                        visible = await code_input.is_visible()
                        if visible:
                            return username_input, code_input
                    _invalidate_input_cache(cache_key)
                except Exception:
                    _invalidate_input_cache(cache_key)

    username_input = None
    code_input = None
    domain_username_selectors = _get_domain_username_selectors(domain)

    username_selectors = domain_username_selectors + [
        "#account-code", "#username-input", "#ten_tai_khoan",
        "input#username", "input[name='username']",
        "input[placeholder*='người dùng' i]", "input[placeholder*='tên' i]",
        "input[placeholder*='tài' i]", "input[placeholder*='tài khoản' i]",
        "input[placeholder*='user' i]", "input[placeholder*='đăng nhập' i]",
        "input[name='ten_tai_khoan']", "input[id='username']", "input[type='text']",
    ]

    profile = get_site_profile(domain)
    domain_code_selectors = list(profile.code_selectors) if profile else []
    code_selectors = domain_code_selectors + [
        "#enter-code-code", "#promo-code", "#giftcode-input", "input[placeholder='Nhập mã']", "input[autocomplete='one-time-code']",
        "input#code", "input[name='code']", "input[placeholder*='mã code' i]",
        "input[placeholder*='code' i]", "input[placeholder*='mã' i]",
        "input[name='giftcode']", "input[id='code']", "input[id*='code' i]", "input[id*='promo' i]",
    ]

    try:
        selector_result = await page.evaluate(
            """
            ({usernameSelectors, codeSelectors}) => {
                const visible = (el) => {
                    if (!el || el.disabled) return false;
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' &&
                           r.width > 0 && r.height > 0;
                };
                const first = (selectors) => {
                    for (const sel of selectors) {
                        try {
                            const el = document.querySelector(sel);
                            if (visible(el)) return sel;
                        } catch (_) {}
                    }
                    return null;
                };
                return {username: first(usernameSelectors), code: first(codeSelectors)};
            }
            """,
            {"usernameSelectors": username_selectors, "codeSelectors": code_selectors},
        )
        if selector_result:
            if selector_result.get("username"):
                username_input = await page.query_selector(selector_result["username"])
            if selector_result.get("code"):
                code_input = await page.query_selector(selector_result["code"])

        if not username_input or not code_input:
            inputs = await page.query_selector_all(
                "input:not([type='hidden']):not([type='checkbox']):not([type='radio']):not([type='submit'])"
            )
            visible_inputs = []
            for inp in inputs:
                if await safe_is_visible(inp):
                    visible_inputs.append(inp)
            if len(visible_inputs) >= 2:
                if not username_input:
                    username_input = visible_inputs[0]
                if not code_input:
                    code_input = visible_inputs[1]
            elif len(visible_inputs) == 1 and not code_input:
                code_input = visible_inputs[0]

    except Exception as e:
        logger.debug(f"⚠️ Error finding input fields: {e}")

    if cache_key and code_input:
        bot_state._input_cache[cache_key] = (username_input, code_input, now)

    return username_input, code_input


async def scroll_to_input_fields(page):
    try:
        found = await page.evaluate(
            """
            () => {
                const inputs = document.querySelectorAll('input[type="text"], input:not([type="hidden"])');
                if (inputs.length > 0) {
                    const firstInput = inputs[0];
                    firstInput.scrollIntoView({behavior: 'auto', block: 'center'});
                    firstInput.focus();
                    return true;
                }
                return false;
            }
            """
        )
        return found
    except Exception as e:
        logger.debug(f"⚠️ Scroll error: {e}")
        return False


async def open_mm88_code_form(page) -> bool:
    """MM88 may land on its home shell before exposing the code form."""
    try:
        clicked = await page.evaluate(
            """
            () => {
                const nodes = [...document.querySelectorAll('a,button,[role="button"],span')];
                const target = nodes.find((el) => {
                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    return text === 'nhập code' || text === 'nhap code';
                });
                if (!target) return false;
                const clickable = target.closest('a,button,[role="button"]') || target;
                clickable.click();
                return true;
            }
            """
        )
        if not clicked:
            return False
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        await asyncio.sleep(0.05)
        return True
    except Exception as e:
        logger.debug(f"⚠️ MM88 Nhập Code navigation lỗi: {e}")
        return False


# ============================================================
# SUBMIT BUTTON CLICKING
# ============================================================
async def click_verification_button_if_present(page, domain: str = "") -> bool:
    if domain not in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:
        logger.debug(f"ℹ️ [{domain or 'unknown'}] Bỏ qua nút Xác thực: domain không yêu cầu")
        return False
    try:
        # Không chạy vòng chờ dài nếu trang không có Turnstile/challenge và
        # cũng chưa render nút xác thực. Trước đây mỗi lượt QQ88/HI88 có thể
        # mất tới VERIFICATION_BUTTON_WAIT_SECONDS dù không cần captcha.
        has_verification_hint = await page.evaluate(
            """
            () => {
                const challenge = document.querySelector(
                    '.cf-turnstile, [data-sitekey], iframe[src*="turnstile"], '
                    + 'iframe[src*="challenges.cloudflare.com"]'
                );
                if (challenge) return true;
                return [...document.querySelectorAll('button,[role="button"]')].some((el) => {
                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    return /^(xác thực|xac thuc|verify)$/.test(text);
                });
            }
            """
        )
        if not has_verification_hint:
            return False

        wait_seconds = min(
            8.0,
            max(1.0, float(getattr(Config, "VERIFICATION_BUTTON_WAIT_SECONDS", 8.0))),
        )
        clicked = bool(await page.evaluate(
            """async function (waitSeconds) {
                var deadline = Date.now() + (waitSeconds * 1000);
                var verifyTexts = ['x\\u00e1c th\\u1ef1c', 'xac thuc', 'verify'];

                function isVisible(el) {
                    if (!el) return false;
                    var rect = el.getBoundingClientRect();
                    var style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none'
                        && style.visibility !== 'hidden';
                }

                function cloudLoading() {
                    var scopes = document.querySelectorAll(
                        '[role="dialog"], [role="alertdialog"], .modal, ' +
                        '[class*="modal" i], [class*="dialog" i], ' +
                        '[class*="captcha" i], [class*="turnstile" i]'
                    );
                    var text = '';
                    for (var i = 0; i < scopes.length; i++) {
                        if (!isVisible(scopes[i])) continue;
                        text += ' ' + (scopes[i].innerText || scopes[i].textContent || '');
                    }
                    if (/thành công|success|verified|verification complete/i.test(text)) {
                        return false;
                    }
                    return /đang (xác minh|kiểm tra|tải)|verifying|checking|loading/i.test(text);
                }

                while (Date.now() < deadline) {
                    var buttons = document.querySelectorAll('button');
                    for (var j = 0; j < buttons.length; j++) {
                        var el = buttons[j];
                        var label = (el.innerText || el.textContent || '').trim().toLowerCase();
                        if (verifyTexts.indexOf(label) === -1) continue;
                        if (!isVisible(el) || el.disabled || cloudLoading()) continue;
                        el.click();
                        return true;
                    }
                    await new Promise(function (resolve) { setTimeout(resolve, 100); });
                }
                return false;
            }""",
            wait_seconds,
        ))
        if not clicked:
            logger.debug(f"ℹ️ [{domain}] Không thấy nút Xác thực trong {wait_seconds}s")
        return clicked
    except Exception as exc:
        logger.warning(f"⚠️ [{domain}] verification button check lỗi: {exc}")
        return False


async def click_submit_fast(page, domain: str = "") -> bool:
    delay_min = float(getattr(Config, "RANDOM_DELAY_MIN", 0.1))
    delay_max = float(getattr(Config, "RANDOM_DELAY_MAX", 0.3))
    if delay_max < delay_min:
        delay_max = delay_min
    if delay_max > 0:
        await asyncio.sleep(random.uniform(delay_min, delay_max))

    profile = get_site_profile(domain)
    domain_sel = profile.submit_selector if profile else None
    if domain_sel:
        try:
            locator = page.locator(domain_sel).first
            await locator.wait_for(state="visible", timeout=300)
            await locator.click(timeout=700)
            logger.debug(f"✅ Playwright-clicked domain-specific button: {domain}")
            return True
        except Exception:
            pass
        try:
            clicked = await page.evaluate(
                """
                async (sel) => {
                    const deadline = Date.now() + 300;
                    while (Date.now() < deadline) {
                        const btn = document.querySelector(sel);
                        if (btn && !btn.disabled) {
                            const rect = btn.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                btn.click();
                                return true;
                            }
                        }
                        await new Promise(r => setTimeout(r, 50));
                    }
                    const btn = document.querySelector(sel);
                    if (btn) { btn.click(); return true; }
                    return false;
                }
                """,
                domain_sel,
            )
            if clicked:
                logger.debug(f"✅ Clicked domain-specific button: {domain}")
                return True
        except Exception:
            pass

    try:
        clicked = await page.evaluate(
            """
            () => {
                const keywords = [
                    'kiểm tra ngay', 'kiem tra ngay', 'kiểm tra', 'kiem tra',
                    'nhận code', 'nhan code', 'nhận ngay', 'nhan ngay',
                    'áp dụng', 'ap dung', 'đổi code', 'doi code',
                    'nạp code', 'nap code', 'gửi', 'gui', 'submit', 'apply'
                ];
                const EXCLUDE = /menu|nav|home|close|cancel|toggle|hamburger|back|trở về|huỷ|hủy|đóng|xác thực|xac thuc|verify|check/i;
                const els = [...document.querySelectorAll(
                    'button, a[role="button"], div[role="button"], span[role="button"], input[type="button"], input[type="submit"]'
                )];
                for (const kw of keywords) {
                    for (const el of els) {
                        if (el.disabled) continue;
                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        const img = el.querySelector('img[alt]');
                        const imgAlt = img ? (img.getAttribute('alt') || '').toLowerCase() : '';
                        const txt = (el.innerText || el.textContent || el.value || '').toLowerCase().trim();
                        if (EXCLUDE.test(aria + txt)) continue;
                        if ([txt, aria, imgAlt].some(s => s && s.includes(kw))) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                el.click();
                                return true;
                            }
                        }
                    }
                }
                return false;
            }
            """
        )
        if clicked:
            return True
    except Exception:
        pass

    generic_selectors = [
        "button[type='submit']", "input[type='submit']", ".btn-submit",
        ".apply-btn", ".submit-btn", "[class*='submit' i]", "[class*='apply' i]",
    ]
    for sel in generic_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await safe_is_visible(el):
                await page.evaluate("el => el.click()", el)
                return True
        except Exception:
            pass

    try:
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


# ============================================================
# RESULT DETECTION
# ============================================================
def _filter_nextjs_noise(text: str) -> str:
    if not text:
        return ""
    noise_markers = [
        "__next_f", "__NEXT", "self.__next", 'push([1,"', '"stylesheet"',
        '"link"', "webpack", "hydrat", '"rel":', '"href":', ':[[[\"$\"',
    ]
    t = text.strip()
    for marker in noise_markers:
        if marker in t:
            return ""
    if t.startswith(('{"', '[["', '[[["', "self.")):
        return ""
    return t


_TRANSIENT_CF_PATTERNS = [
    "captcha", "turnstile", "xác thực người dùng",
    "đang xử lý", "dang xu ly", "đang tải", "dang tai",
    "đang kiểm tra", "dang kiem tra", "checking",
    "vui lòng đợi", "vui long doi", "please wait",
    "processing", "verifying", "đang xác thực", "dang xac thuc",
]


def _is_transient_captcha_text(text: str) -> bool:
    if not text:
        return False
    low = text.strip().lower()
    if any(p in low for p in _TRANSIENT_CF_PATTERNS):
        return True
    BUTTON_ONLY_WORDS = {"hủy", "huy", "xác thực", "xac thuc", "đóng", "dong", "cancel", "verify", "ok", "close"}
    tokens = [t.strip() for t in _re.split(r"[\n/|,]+", low) if t.strip()]
    if tokens and len(low) <= 40 and all(t in BUTTON_ONLY_WORDS for t in tokens):
        return True
    return False


async def _detect_result_by_text_diff(page, before_text: str) -> str:
    try:
        after_text = await page.evaluate("() => document.body.innerText || ''")
    except Exception:
        return ""
    if not after_text:
        return ""

    before_lines = {l.strip() for l in (before_text or "").splitlines() if l.strip()}
    new_lines = []
    for line in after_text.splitlines():
        line = line.strip()
        if not line or line in before_lines:
            continue
        if len(line) < 3:
            continue
        clean = _filter_nextjs_noise(line)
        if not clean:
            continue
        if _is_transient_captcha_text(clean):
            continue
        new_lines.append(clean)

    if not new_lines:
        return ""
    return " ".join(new_lines[:6])


_DETECT_RESULT_JS = r"""
(args) => {
    const { orderedSelectors, combinedSelectors, includeGlobal } = args;
    const readSelector = (sel) => {
        try {
            const els = document.querySelectorAll(sel);
            const texts = [];
            for (const el of els) {
                const t = (el.innerText || el.textContent || '').trim();
                if (t) texts.push(t);
            }
            return texts.join(' ');
        } catch (e) {
            return '';
        }
    };
    const ordered = orderedSelectors.map(readSelector);
    const combinedParts = [];
    if (includeGlobal) {
        for (const sel of combinedSelectors) {
            const t = readSelector(sel);
            if (t) combinedParts.push(t);
        }
    }
    if (!includeGlobal) {
        return {ordered, combinedText: '', keywordText: '', bodyText: ''};
    }
    const keywords = [
        'thành công', 'thanh cong', 'thất bại', 'that bai', 'sai', 'lỗi', 'loi',
        'đã sử dụng', 'da su dung', 'success', 'failed', 'error', 'invalid', 'used',
        'không hợp lệ', 'khong hop le', 'hết hạn', 'het han', 'không đúng', 'không tồn tại',
    ];
    const noisePatterns = ['__next_f', '__NEXT', 'self.__next', 'push([', 'webpack'];
    let keywordText = '';
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let node;
    while (node = walker.nextNode()) {
        const parent = node.parentElement;
        if (!parent || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(parent.tagName)) continue;
        const txt = (node.textContent || '').trim();
        if (txt.length < 3 || noisePatterns.some(p => txt.includes(p))) continue;
        if (keywords.some(k => txt.toLowerCase().includes(k))) {
            keywordText = txt;
            break;
        }
    }
    return {
        ordered,
        combinedText: combinedParts.join(' '),
        keywordText,
        bodyText: (document.body.innerText || '').slice(0, 30000),
    };
}
"""


_WS_RE = _re.compile(r"\s+")


def _normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().lower()


def _is_stale_static_text(candidate: str, before_text: str) -> bool:
    """True nếu 'candidate' đã xuất hiện y hệt trên trang TRƯỚC khi bấm
    submit (before_text chụp lúc đó). Một số site (vd tangquaqq88.com) có
    banner cảnh báo tĩnh (vd "QQ88 LINK CHÍNH THỨC...") luôn nằm sẵn trong
    DOM và tình cờ khớp 1 trong các selector chung (PRIORITY_SELECTORS) —
    nếu không lọc, banner này bị đọc nhầm thành kết quả submit ngay ở lần
    poll ĐẦU TIÊN (trước khi popup thật kịp hiện ra), khiến vòng lặp thoát
    sớm với nội dung sai (AMBIGUOUS/NO_RESULT giả) dù site chưa trả lời gì.
    So khớp theo substring sau khi chuẩn hoá khoảng trắng — nội dung kết
    quả thật (thành công/sai/hết hạn...) gần như không bao giờ trùng khớp
    y hệt với text tĩnh đã có sẵn trước đó."""
    if not candidate or not before_text:
        return False
    norm_candidate = _normalize_ws(candidate)
    if len(norm_candidate) < 3:
        return False
    return norm_candidate in _normalize_ws(before_text)


async def detect_result_text(
    page,
    domain: str = "",
    before_text: str = "",
    *,
    selector_only: bool = False,
) -> str:
    domain_selectors = _get_domain_result_selectors(domain)

    PRIORITY_SELECTORS = [
        ".swal2-container", ".swal2-popup", "[role='alertdialog']",
        "#toast-container", ".iziToast-wrapper", ".notyf", ".p-toast",
        ".p-toast-message-content", "[class*='snackbar' i]",
        ".swal2-html-container", ".swal2-title", ".swal2-popup",
        "div[class*='popup'] p", "div[class*='modal'] p", "div[class*='dialog'] p",
        "div[class*='alert'] p", "div[class*='notice'] p", "div[class*='message'] p",
        ".text-red-600", ".text-green-600", ".text-yellow-600",
        ".text-red-500", ".text-green-500", "p.mt-1.text-sm",
        "div[class*='rounded-2xl'] p", "div[class*='rounded-xl'] p", "div[class*='rounded-lg'] p",
        "[role='alert']", "[role='status']", "[role='dialog']",
        "div[style*='position: fixed'] p", "div[style*='position:fixed'] p",
        "[data-sonner-toast] [data-description]", "[data-sonner-toast]",
        ".Toastify__toast-body", ".ant-message-notice-content", ".ant-notification-notice-message",
        "[data-toast]", "[data-radix-toast-viewport] *", "[aria-live]", "output",
        ".van-toast", ".van-dialog", ".el-message", ".el-notification", ".ant-message",
        ".toast", ".modal", "[class*='message']", "[class*='result']",
        "[class*='success']", "[class*='error']",
    ]

    result_selectors = [
        ".swal2-container", "[role='alertdialog']", "#toast-container",
        ".iziToast-wrapper", ".notyf", ".p-toast", ".p-toast-message-content",
        "[class*='snackbar' i]",
        ".text-red-600", ".text-green-600", "p.mt-1.text-sm",
        "div[class*='rounded-2xl'] p", "div[class*='rounded-xl'] p", "div[class*='rounded-lg'] p",
        "[role='dialog']", "[role='alert']", "[role='status']",
        ".modal-body", ".modal-content", ".popup-content", ".alert",
        "[class*='success']", "[class*='error']", "[class*='toast']",
        "[class*='result']", "[class*='notify']", "[class*='modal']",
        "[class*='popup']", "[class*='notification']", "div[style*='position: fixed']",
    ]

    ordered_selectors = domain_selectors + PRIORITY_SELECTORS

    try:
        data = await page.evaluate(
            _DETECT_RESULT_JS,
            {
                "orderedSelectors": domain_selectors if selector_only else ordered_selectors,
                "combinedSelectors": result_selectors,
                "includeGlobal": not selector_only,
            },
        )
    except Exception:
        data = None

    if data:
        for txt in data.get("ordered", []):
            if txt and len(txt.strip()) >= 3:
                clean = _filter_nextjs_noise(txt.strip())
                if not clean or _is_transient_captcha_text(clean):
                    continue
                if _is_stale_static_text(clean, before_text):
                    continue
                return clean

        combined = (data.get("combinedText") or "").strip()
        if len(combined) >= 3 and not _is_transient_captcha_text(combined):
            filtered = _filter_nextjs_noise(combined)
            if filtered and not _is_stale_static_text(filtered, before_text):
                return filtered

    if data:
        page_text = (data.get("keywordText") or "").strip()
        if page_text:
            clean = _filter_nextjs_noise(page_text)
            if clean and not _is_transient_captcha_text(clean) and not _is_stale_static_text(clean, before_text):
                return clean

        after_text = data.get("bodyText") or ""
        if before_text and after_text:
            before_lines = {line.strip() for line in before_text.splitlines() if line.strip()}
            new_lines = []
            for line in after_text.splitlines():
                line = line.strip()
                if not line or line in before_lines or len(line) < 3:
                    continue
                clean = _filter_nextjs_noise(line)
                if clean and not _is_transient_captcha_text(clean):
                    new_lines.append(clean)
            if new_lines:
                return " ".join(new_lines[:6])

    return ""


# ============================================================
# PAGE PERFORMANCE / STEALTH
# ============================================================
async def _setup_page_performance(page, label: str = ""):
    STEALTH_JS = """
        () => {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
            if (!window.chrome) { window.chrome = {}; }
            window.chrome.runtime = {};
            Object.defineProperty(navigator, 'plugins', {
                get: () => ([
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                ]),
                configurable: true,
            });
            Object.defineProperty(navigator, 'languages', { get: () => ['vi-VN', 'vi', 'en-US', 'en'], configurable: true });
            if (window.$cdc_asdjflasutopfhvcZLmcfl_) { delete window.$cdc_asdjflasutopfhvcZLmcfl_; }
            if (window.$wdc_) { delete window.$wdc_; }
            if (navigator.permissions && navigator.permissions.query) {
                const origQuery = navigator.permissions.query;
                navigator.permissions.query = (parameters) =>
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : origQuery(parameters);
            }
            Object.defineProperty(navigator, 'headless', { get: () => false, configurable: true });
            Object.defineProperty(screen, 'width', { get: () => 1920, configurable: true });
            Object.defineProperty(screen, 'height', { get: () => 1080, configurable: true });
            Object.defineProperty(screen, 'availWidth', { get: () => 1920, configurable: true });
            Object.defineProperty(screen, 'availHeight', { get: () => 1040, configurable: true });
            try {
                const getParam = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                    return getParam.call(this, parameter);
                };
                const getParam2 = WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                    return getParam2.call(this, parameter);
                };
            } catch(e) {}
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
            Object.defineProperty(navigator, 'connection', {
                get: () => ({ rtt: 50, downlink: 10, effectiveType: '4g', saveData: false }),
                configurable: true,
            });
        }
    """
    try:
        await page.add_init_script(STEALTH_JS)
    except Exception as e:
        logger.debug(f"⚠️ [{label}] add_init_script error: {e}")

    _BLOCK_DOMAINS = (
        "google-analytics", "googletagmanager", "doubleclick", "facebook.net",
        "fbcdn.net", "hotjar", "googlesyndication", "adsystem", "criteo",
        "taboola", "outbrain", "clarity.ms", "sentry.io", "crisp.chat", "tawk.to",
    )
    _BLOCK_TYPES = ("media", "ping", "font")

    async def _handle_route(route):
        req = route.request
        url = req.url.lower()
        rtype = req.resource_type
        if "cloudflare" in url:
            await route.continue_()
            return
        if any(d in url for d in _BLOCK_DOMAINS):
            await route.abort()
            return
        if rtype in _BLOCK_TYPES:
            await route.abort()
            return
        await route.continue_()

    try:
        await page.route("**/*", _handle_route)
    except Exception as e:
        logger.debug(f"⚠️ [{label}] Cannot setup route: {e}")


async def _close_unwanted_popups(page):
    try:
        closed = await page.evaluate(
            """
            () => {
                const hasOverlay = document.querySelector(
                    '.modal, [class*="modal" i], [class*="popup" i], [role="dialog"]'
                );
                if (!hasOverlay) return 0;
                const CLOSE_KEYWORDS = ['đóng', 'close', 'x', 'cancel', 'hủy', 'dismiss', 'got it', 'ok', 'thoát'];
                const SKIP_TEXT = ['xác thực', 'xac thuc', 'submit', 'kiểm tra', 'áp dụng', 'nhận'];
                const OVERLAY_SEL = [
                    '.modal', '[class*="modal" i]', '[class*="popup" i]',
                    '[class*="overlay" i]', '[class*="dialog" i]',
                    '[class*="notification" i]', '[class*="toast" i]',
                    '[class*="alert" i]:not(.alert-success):not(.alert-info)',
                    '[class*="banner" i]', '[class*="announcement" i]',
                ];
                const ICON_CLOSE_PATHS = ['M6 18L18 6M6 6l12 12'];
                let count = 0;
                for (const sel of OVERLAY_SEL) {
                    const els = [...document.querySelectorAll(sel)];
                    for (const el of els) {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        const btns = [...el.querySelectorAll('button, [role="button"], a, span')];
                        for (const btn of btns) {
                            const txt = (btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '').trim().toLowerCase();
                            if (SKIP_TEXT.some(s => txt.includes(s))) continue;
                            if (CLOSE_KEYWORDS.some(k => txt === k || txt.startsWith(k))) {
                                btn.click();
                                count++;
                                break;
                            }
                        }
                    }
                }
                const paths = [...document.querySelectorAll('svg path')];
                for (const p of paths) {
                    const d = (p.getAttribute('d') || '').trim();
                    if (!ICON_CLOSE_PATHS.includes(d)) continue;
                    const clickable = p.closest('button, [role="button"], a');
                    if (!clickable) continue;
                    const rect = clickable.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    clickable.click();
                    count++;
                }
                return count;
            }
            """
        )
        if closed and closed > 0:
            logger.debug(f"🧹 Đóng {closed} popup không mong muốn")
    except Exception:
        pass


async def _wake_tab_for_submit(page):
    try:
        await page.bring_to_front()
        await page.evaluate(
            "Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });"
        )
        await _close_unwanted_popups(page)
    except Exception:
        pass


# ============================================================
# TAB POOL
# ============================================================
class TabPool:
    def __init__(self, max_per_domain: int = 3, per_domain_overrides: dict | None = None):
        self.max_per_domain = max(1, int(max_per_domain))
        self._per_domain_max: dict = dict(per_domain_overrides or {})
        self._domains: dict = {}
        self._rr_idx: dict = {}
        self._setup_lock = asyncio.Lock()

    @staticmethod
    def _new_entry(page):
        now = time.monotonic()
        return {"page": page, "lock": asyncio.Lock(), "created_at": now, "last_used": now}

    @staticmethod
    def _touch(entry):
        entry["last_used"] = time.monotonic()

    def _max_for(self, domain: str) -> int:
        return max(1, int(self._per_domain_max.get(domain, self.max_per_domain)))

    async def init(self, domain_url_map: dict | None = None):
        context = await get_or_launch_browser_context("shared")
        existing = list(context.pages)
        claimed_ids = set()
        domain_url_map = domain_url_map or {}

        pending_nav = []
        for domain, target_url in domain_url_map.items():
            if not domain or domain in self._domains:
                continue

            page = None
            for p in existing:
                if id(p) in claimed_ids:
                    continue
                try:
                    url = (p.url or "").lower()
                except Exception:
                    continue
                if domain in url:
                    page = p
                    claimed_ids.add(id(p))
                    break

            opened_new = False
            if page is not None:
                await _setup_page_performance(page, f"dedicated-{domain}")
                logger.info(f"  🗂️ [Startup] '{domain}' → dùng tab có sẵn")
            else:
                page = await context.new_page()
                await _setup_page_performance(page, f"dedicated-{domain}")
                opened_new = True
                logger.info(f"  🌐 [Startup] '{domain}' → tự mở tab mới (CHỈ lần này)")

            self._domains.setdefault(domain, []).append(self._new_entry(page))
            pending_nav.append((domain, page, opened_new, target_url))

        nav_semaphore = asyncio.Semaphore(5)

        async def _goto_one(domain, page, opened_new, target_url):
            async with nav_semaphore:
                try:
                    need_goto = opened_new
                    if not need_goto:
                        try:
                            need_goto = domain not in (page.url or "").lower()
                        except Exception:
                            need_goto = True
                    if need_goto:
                        await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                        await scroll_to_input_fields(page)
                    await _close_unwanted_popups(page)
                    await page.bring_to_front()
                except Exception as e:
                    logger.warning(f"⚠️ [Startup] '{domain}' lỗi điều hướng: {e}")

        await asyncio.gather(*[
            _goto_one(domain, page, opened_new, target_url)
            for domain, page, opened_new, target_url in pending_nav
        ])

        logger.info(
            f"✅ TabPool: {len(self._domains)} domain đã sẵn sàng. Nếu 1 tab bị Cloudflare "
            f"chặn ngay lúc này, lượt submit tới đó sẽ tự fallback sang browser — "
            f"không cần xác minh tay."
        )

    async def _claim_existing_or_new(self, domain: str) -> dict | None:
        context = await get_or_launch_browser_context("shared")
        claimed_ids = {id(e["page"]) for entries in self._domains.values() for e in entries}
        page = None
        for p in context.pages:
            if id(p) in claimed_ids:
                continue
            try:
                url = (p.url or "").lower()
            except Exception:
                continue
            if domain in url:
                page = p
                break

        if page is not None:
            await _setup_page_performance(page, f"dedicated-{domain}")
            entry = self._new_entry(page)
            self._domains.setdefault(domain, []).append(entry)
            return entry

        if not getattr(Config, "AUTO_OPEN_MISSING_TABS", True):
            logger.info(f"🗂️ [Dedicated] '{domain}' → AUTO_OPEN_MISSING_TABS=False: không mở tab mới")
            return None

        page = await context.new_page()
        await _setup_page_performance(page, f"dedicated-{domain}")
        logger.info(f"🌐 [Dedicated] '{domain}' → chưa có tab sẵn (lazy) → mở tab mới")
        entry = self._new_entry(page)
        self._domains.setdefault(domain, []).append(entry)
        return entry

    async def _new_tab_for_domain(self, domain: str) -> dict:
        context = await get_or_launch_browser_context("shared")
        claimed_ids = {id(e["page"]) for e in self._domains.get(domain, [])}
        for p in context.pages:
            if id(p) in claimed_ids:
                continue
            try:
                url = (p.url or "").lower()
            except Exception:
                continue
            if domain in url:
                await _setup_page_performance(p, f"dedicated-{domain}")
                entry = self._new_entry(p)
                self._domains.setdefault(domain, []).append(entry)
                return entry

        if not getattr(Config, "AUTO_OPEN_MISSING_TABS", True):
            raise RuntimeError("Auto-open missing tabs disabled by config")

        page = await context.new_page()
        await _setup_page_performance(page, f"dedicated-{domain}")
        entry = self._new_entry(page)
        self._domains.setdefault(domain, []).append(entry)
        logger.info(
            f"🗂️ [Dedicated] '{domain}' cần xử lý song song → mở thêm tab phụ "
            f"({len(self._domains[domain])}/{self._max_for(domain)})"
        )
        return entry

    async def _respawn_page(self, domain: str):
        context = await get_or_launch_browser_context("shared")
        page = await context.new_page()
        await _setup_page_performance(page, f"dedicated-{domain}")
        return page

    async def acquire(self, domain: str = ""):
        """Acquire a usable tab, waiting briefly through transient contention."""
        wait_limit = max(0.0, float(getattr(Config, "TAB_ACQUIRE_WAIT_SECONDS", 2.0)))
        deadline = time.monotonic() + wait_limit
        while True:
            try:
                return await self._try_acquire(domain)
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.05)

    async def _try_acquire(self, domain: str = ""):
        domain = domain or "unknown"

        entries = self._domains.get(domain)
        if not entries:
            async with self._setup_lock:
                entries = self._domains.get(domain)
                if not entries:
                    entry = await self._claim_existing_or_new(domain)
                    if entry is None:
                        context = await get_or_launch_browser_context("shared")
                        wait_timeout = 10.0
                        poll = 0.5
                        waited = 0.0
                        while waited < wait_timeout:
                            for p in context.pages:
                                try:
                                    if domain in (p.url or "").lower():
                                        entry = self._new_entry(p)
                                        self._domains.setdefault(domain, []).append(entry)
                                        entries = self._domains[domain]
                                        break
                                except Exception:
                                    pass
                            if entries:
                                break
                            await asyncio.sleep(poll)
                            waited += poll
                        if not entries:
                            raise RuntimeError(f"No tab available for domain '{domain}'")

        for entry in entries:
            page = entry["page"]
            if entry["lock"].locked() or safe_is_closed(page):
                continue
            try:
                cf_blocked = await is_cloudflare_present(page, domain=domain)
            except Exception:
                cf_blocked = False
            if cf_blocked:
                continue
            self._touch(entry)
            return entry, page, entry["lock"]

        domain_cap = self._max_for(domain)
        entries = self._domains.get(domain, [])
        if len(entries) < domain_cap:
            try:
                entry = await self._new_tab_for_domain(domain)
                self._touch(entry)
                return entry, entry["page"], entry["lock"]
            except Exception:
                pass

        for candidate in entries:
            if candidate["lock"].locked() or safe_is_closed(candidate["page"]):
                continue
            try:
                if await is_cloudflare_present(candidate["page"], domain=domain):
                    continue
            except Exception:
                pass
            self._touch(candidate)
            return candidate, candidate["page"], candidate["lock"]

        raise RuntimeError(
            f"No available non-blocked tab for domain '{domain}' "
            f"({len(entries)} entries are busy, closed, or under verification)"
        )

    async def collect_garbage(self, *, idle_ttl: float = 900.0, min_tabs_per_domain: int = 1) -> dict:
        """Remove closed/stale spare tabs without interrupting submissions."""
        idle_ttl = max(30.0, float(idle_ttl))
        keep_min = max(1, int(min_tabs_per_domain))
        now = time.monotonic()
        removed = 0
        closed = 0

        async with self._setup_lock:
            for domain, entries in list(self._domains.items()):
                if not entries:
                    self._domains.pop(domain, None)
                    self._rr_idx.pop(domain, None)
                    continue

                survivors = []
                ordered = sorted(entries, key=lambda e: e.get("created_at", now))
                for index, entry in enumerate(ordered):
                    page = entry.get("page")
                    lock = entry.get("lock")
                    is_closed = safe_is_closed(page)
                    is_idle_spare = (
                        index >= keep_min
                        and not lock.locked()
                        and now - float(entry.get("last_used", now)) >= idle_ttl
                    )
                    if lock.locked() or (not is_closed and not is_idle_spare):
                        survivors.append(entry)
                        continue

                    if not is_closed:
                        try:
                            await page.close()
                            closed += 1
                        except Exception:
                            survivors.append(entry)
                            continue
                    removed += 1

                    for key, cached_page in list(bot_state.account_pages.items()):
                        if cached_page is page:
                            bot_state.account_pages.pop(key, None)
                            bot_state._input_cache.pop(key, None)
                            bot_state.context_locks.pop(key, None)

                if survivors:
                    self._domains[domain] = survivors
                    self._rr_idx[domain] = self._rr_idx.get(domain, 0) % len(survivors)
                else:
                    self._domains.pop(domain, None)
                    self._rr_idx.pop(domain, None)

        if removed:
            gc.collect()
            logger.info(
                "🧹 [TabPool-GC] removed=%s closed=%s remaining=%s",
                removed,
                closed,
                sum(len(items) for items in self._domains.values()),
            )
        return {
            "removed": removed,
            "closed": closed,
            "remaining": sum(len(items) for items in self._domains.values()),
        }


_tab_pool: TabPool | None = None


def _check_edge_cdp_port_reachable(port: int, timeout: float = 1.5) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


async def preload_browsers_and_accounts(account_targets: list):
    """account_targets: danh sách item {"key","domain","target_url","accounts"}
    ĐÃ ĐƯỢC LỌC SẴN chỉ gồm domain thuộc BROWSER_DOMAINS — main_script.py
    tính toán và truyền vào (dùng chung build_unique_account_targets())."""
    global _tab_pool

    if not account_targets:
        logger.info("ℹ️ [Browser] Không có kênh nào thuộc 7 domain trình duyệt — bỏ qua preload")
        return

    pool_size = max(1, min(10, int(getattr(Config, "TAB_POOL_SIZE", 1) or 1)))

    domain_channel_count: dict = {}
    for item in account_targets:
        d = item["domain"]
        domain_channel_count[d] = domain_channel_count.get(d, 0) + 1

    domain_tab_cap = max(1, int(getattr(Config, "MAX_TAB_PER_DOMAIN_CAP", 5)))
    per_domain_overrides = {}
    for d, count in domain_channel_count.items():
        # Apply the cap to every configured domain. Without this override,
        # TAB_POOL_SIZE could accidentally allow many tabs for domains that
        # have fewer configured account targets than the global pool size.
        per_domain_overrides[d] = min(domain_tab_cap, max(1, count))

    _tab_pool = TabPool(max_per_domain=pool_size, per_domain_overrides=per_domain_overrides)

    domain_url_map: dict = {}
    for item in account_targets:
        d = item["domain"]
        if d not in domain_url_map:
            domain_url_map[d] = item["target_url"]

    for item in account_targets:
        key = item.get("key", item["domain"])
        bot_state.context_locks[key] = asyncio.Lock()
        bot_state.cf_verified[key] = True
        bot_state.submission_count[key] = 0

    site_count = len({item.get("domain") for item in account_targets if item.get("domain")})
    logger.info(
        "✅ [Browser] %s target domain+tài khoản (%s site trình duyệt) đăng ký xong",
        len(account_targets),
        site_count,
    )

    cdp_port = getattr(Config, "EDGE_CDP_PORT", 9222)
    cdp_ready = _check_edge_cdp_port_reachable(cdp_port)
    if not cdp_ready:
        max_retries = 7
        for attempt in range(1, max_retries + 1):
            logger.info(f"⏳ [Edge-CDP] Cổng {cdp_port} chưa phản hồi — thử lại ({attempt}/{max_retries}, mỗi 2s)...")
            await asyncio.sleep(2.0)
            if _check_edge_cdp_port_reachable(cdp_port):
                cdp_ready = True
                break

    if cdp_ready:
        logger.info(f"✅ [Edge-CDP] Cổng {cdp_port} đang mở — Edge sẵn sàng nhận kết nối")
        try:
            await _tab_pool.init(domain_url_map=domain_url_map)
            logger.info(f"✅ [TabPool] Đã gán tab riêng cho {len(domain_url_map)} domain")
        except Exception as e:
            logger.warning(f"⚠️ [TabPool] Không init được ngay lúc preload ({e}) — sẽ tự thử lại kiểu lazy")
    else:
        # Không để một pool chưa khởi tạo tiếp tục chạy lazy rồi thử kết nối
        # lại trong từng submit, gây chậm khoảng thời gian timeout CDP.
        _tab_pool = None
        logger.error(
            f"❌ [Edge-CDP] Cổng {cdp_port} KHÔNG phản hồi — Edge CHƯA chạy ở chế độ debug! "
            f"Các submit sẽ fail-fast cho tới khi Edge được khởi động đúng chế độ debug."
        )


# ============================================================
# PAGE CLEAN-UP AFTER SUBMIT
# ============================================================
async def _reload_page_and_refill(page, domain: str, target_url: str, key: str):
    try:
        edge_restore()
        await page.goto(target_url, wait_until="domcontentloaded", timeout=int(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)))
        if domain == "livemm88.net":
            await open_mm88_code_form(page)
        await scroll_to_input_fields(page)
        await _close_unwanted_popups(page)
        settle = float(getattr(Config, "MM88_FORM_SETTLE_SECONDS", 0.05)) if domain == "livemm88.net" else float(getattr(Config, "FORM_SETTLE_SECONDS", 0.03))
        await asyncio.sleep(max(0.02, settle))
        _invalidate_input_cache(key)
        return True
    except Exception as e:
        logger.warning(f"⚠️ [{domain}] Lỗi reload trang sau submit: {e}")
        return False


async def _quick_clean_page(page, key: str) -> bool:
    try:
        await _close_unwanted_popups(page)
        await page.evaluate(
            """
            () => {
                const inputs = document.querySelectorAll('input:not([type="hidden"])');
                for (const inp of inputs) {
                    try {
                        const placeholder = (inp.placeholder || '').toLowerCase();
                        const isUsername = placeholder.includes('tài khoản')
                            || placeholder.includes('tên người dùng')
                            || placeholder.includes('tai khoan')
                            || placeholder.includes('ten nguoi dung')
                            || inp.id === 'account-code'
                            || inp.name === 'username';
                        if (isUsername && (inp.value || '').trim()) continue;
                        const proto = inp.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        setter.call(inp, '');
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                    } catch (e) {}
                }
            }
            """
        )
        return True
    except Exception as e:
        logger.debug(f"⚠️ [{key}] Quick-clean lỗi: {e}")
        return False


async def _clean_page_after_submit(page, domain: str, target_url: str, key: str, force_full: bool = False):
    count = bot_state._submits_since_full_reload.get(key, 0) + 1
    threshold = int(getattr(Config, "FULL_RELOAD_EVERY_N", 100))

    if not force_full and count < threshold:
        ok = await _quick_clean_page(page, key)
        if ok:
            bot_state._submits_since_full_reload[key] = count
            return True

    bot_state._submits_since_full_reload[key] = 0
    return await _reload_page_and_refill(page, domain, target_url, key)


# ============================================================
# SUBMIT (bản trình duyệt) — điểm vào chính của module này
# ============================================================
async def submit_code_browser(user: str, code: str, target_url: str, systems: dict) -> dict:
    """Serialize form mutations for one ``(domain, account)`` pair.

    A page keeps the username and code in shared DOM state. Without this
    outer lock, two fanout tasks for the same account can overwrite each
    other's inputs between fill and click, producing validation errors.
    """
    domain = _normalize_domain(target_url)
    key = f"{domain}|{user}"
    lock = bot_state.context_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _submit_code_browser_locked(user, code, target_url, systems)


async def _submit_code_browser_locked(user: str, code: str, target_url: str, systems: dict) -> dict:
    """Trả về kết quả submit chuẩn hóa gồm {"success", "message",
    "has_points", "is_wrong_code", ...}. Khi gặp lỗi HẠ TẦNG (mất tab, mất
    CDP, không tìm thấy input, Cloudflare/captcha chặn...) trả thêm khoá
    "_infra_failure": True — main_script.py.submit_code_safe() dựa vào cờ
    này để ghi nhận lỗi sang browser ngay, không đứng chờ."""
    start_time = time.time()
    domain = _normalize_domain(target_url)
    key = f"{domain}|{user}"

    if _tab_pool is None:
        return {"success": False, "message": "Browser chưa sẵn sàng (no TabPool)", "_infra_failure": True}

    if key not in bot_state.context_locks:
        bot_state.context_locks[key] = asyncio.Lock()
        bot_state.cf_verified[key] = True
        bot_state.submission_count.setdefault(key, 0)

    try:
        tab_entry, page, tab_lock = await _tab_pool.acquire(domain=domain)
    except Exception as e:
        logger.warning(f"⚠️ [Browser|{domain}] Không lấy được tab: {e}")
        return {"success": False, "message": f"No tab: {e}", "_infra_failure": True}

    try:
        async with tab_lock:
            bot_state.account_pages[key] = page

            if page.is_closed():
                context = await get_or_launch_browser_context("shared")
                page = await context.new_page()
                await _setup_page_performance(page, domain)
                tab_entry["page"] = page
                bot_state.account_pages[key] = page
                bot_state._input_cache.pop(key, None)

            try:
                page_url = page.url
            except Exception:
                page_url = ""

            if (not page_url or "about:blank" in page_url or "google.com" in page_url or domain not in page_url.lower()):
                logger.info(f"🌐 [{domain}] Điều hướng tới {target_url}")
                edge_restore()
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=int(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)))
                    if domain == "livemm88.net":
                        await open_mm88_code_form(page)
                    await scroll_to_input_fields(page)
                    settle = float(getattr(Config, "MM88_FORM_SETTLE_SECONDS", 0.05)) if domain == "livemm88.net" else float(getattr(Config, "FORM_SETTLE_SECONDS", 0.03))
                    await asyncio.sleep(max(0.02, settle))
                    _invalidate_input_cache(key)
                except Exception as e:
                    return {"success": False, "message": f"Goto failed: {e}", "_infra_failure": True}

                await _wake_tab_for_submit(page)
                edge_restore()

                if await is_cloudflare_present(page, domain=domain):
                    logger.warning(f"⚠️ [{domain}] Cloudflare/captcha chặn tab — lỗi hạ tầng browser")
                    return {"success": False, "message": "Cloudflare challenge", "_infra_failure": True}

            cached_entry = bot_state._input_cache.get(key)
            cache_was_fresh = bool(cached_entry and (time.time() - cached_entry[2]) < bot_state._input_cache_ttl)
            username_input, code_input = await find_input_fields(page, cache_key=key, domain=domain)

            if not code_input:
                await asyncio.sleep(0.05)
                _invalidate_input_cache(key)
                cache_was_fresh = False
                username_input, code_input = await find_input_fields(page, cache_key=key, domain=domain)

            if not code_input:
                if domain == "livemm88.net" and await open_mm88_code_form(page):
                    _invalidate_input_cache(key)
                    username_input, code_input = await find_input_fields(page, cache_key=key, domain=domain)

            if not code_input:
                return {"success": False, "message": "Không tìm thấy ô nhập code (site có thể đã đổi UI)", "_infra_failure": True}

            if not cache_was_fresh:
                await scroll_to_input_fields(page)

            preserve_prefilled = False
            try:
                if username_input and getattr(Config, "PRESERVE_PREFILLED_USERNAME", True):
                    try:
                        current_username = (await username_input.input_value()).strip()
                        preserve_prefilled = bool(current_username) and current_username == str(user).strip()
                        if preserve_prefilled:
                            logger.debug(f"✅ [{domain}|{user}] giữ tài khoản đã điền sẵn, chỉ nhập code")
                    except Exception:
                        preserve_prefilled = False
                # Gộp fill + đọc xác minh vào một round-trip CDP thay vì
                # fill username, fill code, đọc username, đọc code riêng lẻ.
                verify = await page.evaluate(
                    REACT_FILL_VERIFY_JS,
                    [username_input, code_input, user, code, not preserve_prefilled],
                )
                actual_user = (verify.get("actualUser") or "").strip()
                actual_code = (verify.get("actualCode") or "").strip()
                if username_input and not preserve_prefilled and actual_user != str(user).strip():
                    raise RuntimeError(
                        f"Account input mismatch: expected={user!r} actual={actual_user!r}"
                    )
                if actual_code.upper() != str(code).strip().upper():
                    raise RuntimeError(
                        f"Code input mismatch: expected={code!r} actual={actual_code!r}"
                    )
            except Exception as e:
                _invalidate_input_cache(key)
                username_input, code_input = await find_input_fields(page, cache_key=key, domain=domain)
                if code_input:
                    try:
                        verify = await page.evaluate(
                            REACT_FILL_VERIFY_JS,
                            [username_input, code_input, user, code, not preserve_prefilled],
                        )
                        actual_user = (verify.get("actualUser") or "").strip()
                        actual_code = (verify.get("actualCode") or "").strip()
                        if username_input and not preserve_prefilled and actual_user != str(user).strip():
                            return {
                                "success": False,
                                "message": f"Account input mismatch after retry: {actual_user!r}",
                                "_infra_failure": True,
                            }
                        if actual_code.upper() != str(code).strip().upper():
                            return {
                                "success": False,
                                "message": f"Code input mismatch after retry: {actual_code!r}",
                                "_infra_failure": True,
                            }
                    except Exception as e2:
                        return {"success": False, "message": f"Fill error: {e2}", "_infra_failure": True}
                else:
                    return {"success": False, "message": f"Fill error: {e}", "_infra_failure": True}

            # QQ88/HI88 expose the Cloudflare verification button only after
            # the user clicks "Kiểm tra ngay". Their required order is:
            # fill code -> click check -> wait for Xác thực -> click Xác thực.
            if domain not in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:
                cf_wait_deadline = time.time() + float(getattr(Config, "CF_WAIT_SECONDS", 1.0))
                while time.time() < cf_wait_deadline:
                    try:
                        cf_state = await page.evaluate(
                            """
                            () => {
                                const hasWidget = !!document.querySelector(
                                    '.cf-turnstile, [data-sitekey], iframe[src*="turnstile"], '
                                    + 'iframe[src*="challenges.cloudflare.com"]'
                                );
                                if (!hasWidget) return {hasWidget: false, passed: false};
                                const text = (document.body.innerText || '').toLowerCase();
                                const passed = ['thành công', 'thanh cong', 'verified', 'success']
                                    .some((marker) => text.includes(marker));
                                return {hasWidget: true, passed};
                            }
                            """
                        )
                    except Exception:
                        cf_state = {"hasWidget": False, "passed": False}
                    if not cf_state.get("hasWidget") or cf_state.get("passed"):
                        break
                    await asyncio.sleep(max(0.05, float(getattr(Config, "CF_POLL_INTERVAL", 0.10))))

            try:
                pre_click_text = await page.evaluate(
                    """
                    () => {
                        const scopes = document.querySelectorAll(
                            'form, [role="dialog"], .modal, main, #app, #root'
                        );
                        let text = '';
                        for (const scope of scopes) {
                            text += (scope.innerText || '') + '\n';
                            if (text.length >= 30000) break;
                        }
                        return text.slice(0, 30000);
                    }
                    """
                )
            except Exception:
                pre_click_text = ""

            clicked = await click_submit_fast(page, domain=domain)
            verified_clicked = await click_verification_button_if_present(page, domain=domain)
            if verified_clicked:
                logger.info(f"✅ [Browser|{domain}] đã bấm nút Xác thực")
            elif domain in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:
                # Turnstile may still be verifying after the check button was
                # clicked. Stop this attempt before result recording: that
                # callback can clean/reload the page and restart the widget.
                # Keep the current page alive so a human can finish the
                # verification in the same Edge profile.
                try:
                    challenge_pending = await is_cloudflare_present(page, domain=domain)
                except Exception:
                    challenge_pending = False
                if challenge_pending:
                    logger.warning(
                        f"⚠️ [{domain}] Turnstile chưa hoàn tất — giữ nguyên trang, "
                        "không reload và chờ xác minh thủ công"
                    )
                    return {
                        "success": False,
                        "message": "Turnstile verification pending",
                        "_infra_failure": True,
                        "keep_page": True,
                    }
            click_elapsed = time.time() - start_time
            logger.info(f"🚀 [Browser|{user}] SUBMIT {code} ({click_elapsed:.2f}s)")

            result_text = ""
            timeout_by_domain = getattr(Config, "RESULT_DETECTION_TIMEOUT_BY_DOMAIN", {})
            default_timeout_ms = getattr(Config, "RESULT_DETECTION_TIMEOUT", 5000)
            result_timeout_ms = timeout_by_domain.get(domain, default_timeout_ms)
            result_timeout_s = float(result_timeout_ms) / 1000.0
            poll_deadline = time.time() + result_timeout_s
            selector_fast_deadline = min(
                poll_deadline,
                time.time() + max(
                    0.0,
                    float(getattr(Config, "RESULT_SELECTOR_FAST_WINDOW_SECONDS", 0.80)),
                ),
            )
            poll_interval = max(0.05, float(getattr(Config, "RESULT_POLL_INTERVAL", 0.10)))

            while time.time() < poll_deadline:
                try:
                    # Fast path: query only selectors owned by this site's
                    # profile. This avoids reading/scanning the whole DOM on
                    # every poll. Keep the old global detector as a fallback
                    # for sites whose UI changes or has no stable selector.
                    candidate = await detect_result_text(
                        page,
                        domain=domain,
                        before_text=pre_click_text,
                        selector_only=time.time() < selector_fast_deadline,
                    )
                    if candidate and len(candidate.strip()) >= 5:
                        result_text = candidate
                        break
                    if candidate and len(candidate.strip()) > len(result_text.strip()):
                        result_text = candidate
                except Exception:
                    pass
                await asyncio.sleep(poll_interval)
                # Kết quả thường xuất hiện trong vài trăm ms; sau đó tăng
                # nhẹ khoảng poll để không tạo hàng chục evaluate vô ích.
                poll_interval = min(0.25, poll_interval * 1.2)

            elapsed = time.time() - start_time

            if _needs_manual_verify(result_text):
                logger.warning(
                    f"⚠️ [{domain}] Site yêu cầu xác thực riêng (captcha ảnh) — "
                    f"result_text={result_text[:200]!r}"
                )
                try:
                    await _close_unwanted_popups(page)
                except Exception:
                    pass
                return {"success": False, "message": "Cần xác thực thủ công", "_infra_failure": True}

            status = classify_result(result_text)
            callbacks = {
                "take_screenshot": take_result_screenshot,
                "clean_page": _clean_page_after_submit,
                "append_history": _append_code_history_safe,
            }
            debug_info = None
            if status.value == "NO_RESULT":
                debug_info = {
                    "code": code, "user": user, "domain": domain,
                    "clicked_submit_button": clicked,
                    "pre_click_text_len": len(pre_click_text or ""),
                    "result_timeout_s": result_timeout_s,
                    "page_url": page.url if page else None,
                }

            outcome = await record_outcome(
                status=status, page=page, user=user, code=code,
                target_url=target_url, domain=domain, key=key, elapsed=elapsed,
                result_text=result_text, systems=systems, callbacks=callbacks,
                debug_info=debug_info,
            )
            return outcome.result

    except Exception as e:
        elapsed = time.time() - start_time
        err_str = str(e)
        if "Target page, context or browser has been closed" in err_str or "TargetClosedError" in type(e).__name__:
            try:
                context = await get_or_launch_browser_context("shared", force_reconnect=True)
                new_page = await context.new_page()
                bot_state.account_pages[key] = new_page
                await _setup_page_performance(new_page, domain)
                await new_page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
                _invalidate_input_cache(key)
            except Exception:
                pass
        try:
            systems["performance_monitor"].record_task("submit_code", elapsed, False)
        except Exception:
            pass
        logger.error(f"❌ [Browser|{domain}] {e}")
        return {"success": False, "message": str(e), "_infra_failure": True}


# ============================================================
# WATCHDOGS — chỉ giữ phần KHÔNG liên quan captcha (giữ Edge/tab sống)
# ============================================================
_edge_cdp_was_down = False


async def browser_watchdog():
    """Single ordered watchdog: CDP reachability/reconnect, then stale tabs."""
    interval = max(5.0, float(getattr(Config, "CDP_PING_INTERVAL", 60.0)))
    port = getattr(Config, "EDGE_CDP_PORT", 9222)
    global _edge_cdp_was_down
    while bot_state.is_running:
        try:
            await asyncio.sleep(interval)
            reachable = _check_edge_cdp_port_reachable(port, timeout=2.0)
            if not reachable:
                if not _edge_cdp_was_down:
                    logger.critical(f"❌ [Browser-Watchdog] Mất kết nối CDP {port}; đang reconnect")
                    _edge_cdp_was_down = True
                try:
                    await get_or_launch_browser_context("shared", force_reconnect=True)
                    _edge_cdp_was_down = False
                    logger.info(f"✅ [Browser-Watchdog] CDP {port} đã kết nối lại")
                except Exception as exc:
                    logger.warning(f"⚠️ [Browser-Watchdog] Reconnect thất bại: {exc}")
                continue
            if _edge_cdp_was_down:
                logger.info(f"✅ [Browser-Watchdog] CDP {port} phản hồi trở lại")
                _edge_cdp_was_down = False
            stale_keys = [key for key, page in list(bot_state.account_pages.items()) if safe_is_closed(page)]
            for key in stale_keys:
                domain = key.split("|", 1)[0]
                target_url = getattr(Config, "DOMAIN_TO_CHANNEL_URL", {}).get(domain)
                if not target_url:
                    target_url = next(
                        (cfg["url"] for cfg in Config.CHANNEL_CONFIG.values()
                         if _normalize_domain(cfg["url"]) == domain),
                        None,
                    )
                old_page = bot_state.account_pages.get(key)
                ctx = old_page.context if old_page else None
                if not target_url or ctx is None:
                    bot_state.account_pages.pop(key, None)
                    bot_state.context_locks.pop(key, None)
                    bot_state._input_cache.pop(key, None)
                    continue
                try:
                    new_page = await ctx.new_page()
                    await _setup_page_performance(new_page, domain)
                    await new_page.goto(target_url, wait_until="domcontentloaded", timeout=12000)
                    bot_state.account_pages[key] = new_page
                    bot_state._input_cache.pop(key, None)
                    logger.info(f"✅ [Browser-Watchdog] Đã mở lại tab {key}")
                except Exception as exc:
                    logger.warning(f"⚠️ [Browser-Watchdog] Mở lại tab {key} thất bại: {exc}")
            if _tab_pool is not None:
                try:
                    await _tab_pool.collect_garbage(
                        idle_ttl=getattr(Config, "TAB_POOL_IDLE_TTL", 900.0),
                        min_tabs_per_domain=getattr(Config, "TAB_POOL_MIN_TABS_PER_DOMAIN", 1),
                    )
                except Exception as exc:
                    logger.debug(f"⚠️ [TabPool-GC] cleanup lỗi: {exc}")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug(f"⚠️ browser_watchdog error: {exc}")


async def cleanup_browsers():
    global _shared_context, _edge_browser, _pw_instance

    _shared_context = None

    if _edge_browser is not None:
        try:
            await _edge_browser.close()
        except Exception:
            pass
        _edge_browser = None

    if _pw_instance is not None:
        try:
            await _pw_instance.stop()
        except Exception:
            pass
        _pw_instance = None
