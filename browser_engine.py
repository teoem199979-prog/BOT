from __future__ import annotations

import asyncio
from collections import deque
import os
import ctypes
import ctypes.wintypes
import re as _re
import time
import threading
import tempfile
import weakref
import hashlib
import json  # PATCH_MARKER_2026-10-25-v8-bugfix (#11: import tap trung, bo import trong vong nong)
# PATCH_MARKER_2026-10-26-v10 (Viec1 form 1500ms, Viec2 log chunk, Viec3 tab states, Viec6 foreground 2s)
from functools import lru_cache
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from config import Config, is_domain_enabled, get_effective_domain_accounts
from logger_setup import logger, warning_once
from media_helpers import take_result_screenshot
from submission_outcomes import record_outcome, classify_result, ResultStatus
from dashboard import update_latency
from browser_site_profiles import browser_domains, get_site_profile
from submit_pacer import pacer as submit_pacer
from code_identity import normalize_code
from llwin_slider import OS_MOUSE_WARNING, is_llwin_slider_visible, solve_llwin_slider  # PATCH_MARKER_2026-10-26-llwin-slider-logs2
import chrome_launcher

_abort_wait_tasks: set = set()


BROWSER_DOMAINS = frozenset(d for d in browser_domains() if is_domain_enabled(d))
ASYNC_SUBMIT_COUNTS = {"TURNSTILE_SKIP_VIA_COOKIE": 0}


@lru_cache(maxsize=64)
def _normalize_domain(url: str) -> str:
    p = urlparse(url or "")
    return (p.netloc or p.path).lower().replace("www.", "").strip("/")


def _page_matches_target(page_url: str, target_url: str, domain: str = "") -> bool:

    try:
        current = urlparse(page_url or "")
        target = urlparse(target_url or "")
        current_host = (current.hostname or "").lower().removeprefix("www.")
        target_host = (target.hostname or domain or "").lower().removeprefix("www.")
        if not current_host or current_host != target_host:
            return False
        target_path = (target.path or "/").rstrip("/") or "/"
        current_path = (current.path or "/").rstrip("/") or "/"
        return target_path == "/" or current_path == target_path
    except Exception:
        return False


def _append_code_history_safe(**kwargs):

    try:
        import sys
        _ms = sys.modules.get("__main__")
        if _ms is None or not hasattr(_ms, "append_code_history"):
            import main_script as _ms
        _ms.append_code_history(**kwargs)
    except Exception as e:
        logger.debug(f"⚠️ [Browser] append_history lỗi: {e}")


class BrowserState:
    def __init__(self):
        self.account_pages: dict = {}
        self.context_locks: dict = {}
        self._input_cache: dict = {}

        self._input_cache_ttl: float = 20.0
        self._submits_since_full_reload: dict = {}
        self.is_running = True


bot_state = BrowserState()


_browser_memory_pressure_requested = False


def request_browser_memory_cleanup() -> None:

    global _browser_memory_pressure_requested
    _browser_memory_pressure_requested = True


def shutdown():
    bot_state.is_running = False


def _get_domain_result_selectors(domain: str) -> list:
    profile = get_site_profile(domain)
    return list(profile.result_selectors) if profile else []


def _site_profile_value(domain: str, name: str, default):
    profile = get_site_profile(domain)
    return getattr(profile, name, default) if profile else default


_CF_STATE_JS = """
() => {
    const token = [...document.querySelectorAll(
        'textarea[name="g-recaptcha-response"], textarea[name*="recaptcha"], '
        + 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
        + '[data-cf-turnstile-response]'
    )].some((el) => String(
        el.value || el.textContent || el.getAttribute('data-cf-turnstile-response') || ''
    ).trim().length > 20);
    // A solved token is decisive: skip the layout-forcing innerText read.
    if (token) return false;
    const text = (document.body.innerText || '').toLowerCase();
    if (['xác thực thành công', 'xac thuc thanh cong', 'verification successful']
        .some((marker) => text.includes(marker))) {
        return false;
    }
    const visible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden';
    };
    if ([...document.querySelectorAll('button')].some((button) => {
        const label = (button.innerText || button.textContent || '').trim();
        return /^(xác thực|xac thuc|verify)$/i.test(label) && visible(button);
    })) return true;
    const loadFailed = ['không tải được captcha', 'khong tai duoc captcha', 'thử lại', 'thu lai']
        .some((marker) => text.includes(marker))
        && ['captcha', 'turnstile', 'cloudflare'].some((marker) => text.includes(marker));
    if (loadFailed) return true;
    return [...document.querySelectorAll(
        "iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com'], .cf-turnstile, [data-sitekey]"
    )].some(visible);
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




REACT_FILL_PRESERVE_JS = """
    ([userEl, codeEl, userVal, codeVal, preserve]) => {
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
        let preserved = false;
        if (userEl) {
            const cur = (userEl.value || '').trim();
            preserved = !!preserve && !!cur && cur.toLowerCase() === String(userVal).trim().toLowerCase();
            if (!preserved) setVal(userEl, userVal);
        }
        setVal(codeEl, codeVal);
        return {
            actualUser: userEl ? userEl.value : null,
            actualCode: codeEl.value,
            preserved,
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


PROFILE_FILL_JS = """async ([userSelectors, codeSelectors, user, code, preserve]) => {
    const started = performance.now();
    const usable = (el, editable) => {
        if (!el || !el.isConnected || el.disabled || (editable && el.readOnly)) return false;
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== 'none'
            && s.visibility !== 'hidden' && s.opacity !== '0';
    };
    const first = (selectors, editable) => {
        for (const selector of selectors) {
            try { for (const el of document.querySelectorAll(selector)) {
                if (usable(el, editable)) return el;
            }} catch (_) {}
        }
        return null;
    };
    let userEl = first(userSelectors, false), codeEl = first(codeSelectors, true);
    // Do not guess which generic input belongs to the account/code.
    if (!userEl || !codeEl || userEl === codeEl) return null;
    const userVal = String(user).trim(), codeVal = String(code).trim();
    const matchesAccount = (userEl.value || '').trim() === userVal;
    const preserved = matchesAccount && (!!preserve || userEl.readOnly);
    // A readonly account belongs to the site's current session. Preserve it
    // only when it matches; never silently replace a different account.
    if (userEl.readOnly && !matchesAccount) return null;
    const setValue = async (el, value) => {
        const proto = el.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        const previous = el.value;
        el.focus({preventScroll:true});
        setter.call(el, value);
        if (el._valueTracker && typeof el._valueTracker.setValue === 'function') {
            el._valueTracker.setValue(previous);
        }
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        await Promise.resolve();
    };
    if (!preserved) await setValue(userEl, userVal);
    // Account events can replace the code field before it is filled. Resolve
    // live elements after those events rather than filling a detached handle.
    userEl = first(userSelectors, false);
    codeEl = first(codeSelectors, true);
    if (!userEl || !codeEl || userEl === codeEl
            || (userEl.value || '').trim() !== userVal) return null;
    await setValue(codeEl, codeVal);
    const currentUser = first(userSelectors, false), currentCode = first(codeSelectors, true);
    if (!currentUser || !currentCode || currentUser === currentCode) {
        throw new Error('Form replaced during fill');
    }
    return {preserved, actualUser:currentUser.value, actualCode:currentCode.value,
        dom_ms:performance.now()-started};
}"""


_NOFILL = object()


async def _fill_profile_form(page, domain: str, user: str, code: str):
    profile = get_site_profile(domain)
    watcher = _PENDING_VERIFY_TASKS.get(id(page))
    if profile is None or (watcher is not None and not watcher.done()):
        return None
    try:
        expected_user, expected_code = str(user).strip(), str(code).strip()
        result = await page.evaluate(PROFILE_FILL_JS, [list(profile.username_selectors),
            list(profile.code_selectors), expected_user, expected_code,
            bool(getattr(Config, "PRESERVE_PREFILLED_USERNAME", True))])
        if result and ((result.get("actualUser") or "").strip() != expected_user
                       or not _code_matches_expected((result.get("actualCode") or "").strip(), expected_code, domain)):
            _invalidate_page_input_cache(page)
            return None
        return result
    except Exception:
        # The existing handle-based path retries and verifies values before click.
        _invalidate_page_input_cache(page)
        return None


_TAB_ONLY_FILL_JS = """async ([userSelectors, codeSelectors, expectedUser, code]) => {
    const first = (selectors, editable) => {
        for (const selector of selectors) {
            try { for (const el of document.querySelectorAll(selector)) {
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                if (el.isConnected && !el.disabled && (!editable || !el.readOnly)
                    && r.width > 0 && r.height > 0 && s.display !== 'none'
                    && s.visibility !== 'hidden' && s.opacity !== '0') return el;
            }} catch (_) {}
        }
        return null;
    };
    const user = first(userSelectors, false), input = first(codeSelectors, true);
    if (!user || !input || user === input || (user.value || '').trim() !== expectedUser) return null;
    const proto = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const old = input.value;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(input, String(code).trim());
    if (input._valueTracker) input._valueTracker.setValue(old);
    input.dispatchEvent(new Event('input', {bubbles:true}));
    input.dispatchEvent(new Event('change', {bubbles:true}));
    await Promise.resolve();
    const currentUser = first(userSelectors, false), currentCode = first(codeSelectors, true);
    if (!currentUser || !currentCode || (currentUser.value || '').trim() !== expectedUser) return null;
    return {actualUser: currentUser.value, actualCode: currentCode.value, preserved:true, dom_ms:0};
}"""


async def _read_tab_username(page, domain: str) -> str:
    profile = get_site_profile(domain)
    selectors = list(profile.username_selectors) if profile else []
    return str(await page.evaluate("""sels => {
        for (const sel of sels) {
            try { for (const el of document.querySelectorAll(sel)) {
                const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                if (r.width && r.height && s.display !== 'none' && s.visibility !== 'hidden')
                    return (el.value || '').trim();
            }} catch (_) {}
        }
        return '';
    }""", selectors) or "").strip()


async def _fill_tab_only_form(page, domain: str, user: str, code: str):
    profile = get_site_profile(domain)
    if profile is None:
        return None
    try:
        result = await page.evaluate(_TAB_ONLY_FILL_JS, [list(profile.username_selectors),
                                     list(profile.code_selectors), user, code])
        if result and _code_matches_expected(result.get("actualCode", ""), code, domain):
            return result
    except Exception:
        pass
    return None


def _needs_manual_verify(text: str) -> bool:
    if not text:
        return False
    low = text.strip().lower()
    return any(k in low for k in _MANUAL_VERIFY_KEYWORDS)


class _CdpEndpoint:

    def __init__(self, name: str, label: str, host_attr: str, port_attr: str, default_port: int):
        self.name = name
        self.label = label
        self._host_attr = host_attr
        self._port_attr = port_attr
        self._default_port = default_port
        self.pw = None
        self.browser = None
        self.context = None
        self.lock = None
        self.connect_task = None
        self.retry_at = 0.0
        self.last_error = None
        self.failure_count = 0
        self.was_down = False
        self.recovery_task = None
        self.recovery_exhausted = False
        self.hwnd = 0
        self.hwnd_retry_at = 0.0
        self.last_restore_at = 0.0
        self.last_foreground_at = 0.0  # PATCH_MARKER_2026-10-26-v10-foreground-throttle
        self.last_foreground_ok = False
        self.window_lock = threading.RLock()

    @property
    def host(self) -> str:
        return str(getattr(Config, self._host_attr, "127.0.0.1") or "127.0.0.1")

    @property
    def port(self) -> int:
        return int(getattr(Config, self._port_attr, self._default_port))

    def is_connected(self) -> bool:
        return (
            self.context is not None
            and self.browser is not None
            and self.browser.is_connected()
        )


_CHROME = _CdpEndpoint("edge", "Edge 1", "EDGE_CDP_HOST", "EDGE_CDP_PORT", 9222)
_CHROME2 = _CdpEndpoint("edge2", "Edge 2", "EDGE2_CDP_HOST", "EDGE2_CDP_PORT", 9223)


_needed_endpoint_names: set = set()


def _clean_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    return d[4:] if d.startswith("www.") else d


def _chrome2_domains() -> frozenset:
    raw = str(getattr(Config, "CHROME2_DOMAINS", "") or "")
    return frozenset(_clean_domain(x) for x in raw.split(",") if x.strip())


def endpoint_for_domain(domain: str = "") -> _CdpEndpoint:
    d = _clean_domain(domain)
    if d and bool(getattr(Config, "CHROME2_ENABLED", False)) and d in _chrome2_domains():
        return _CHROME2
    return _CHROME


def _needed_endpoints() -> list:
    names = set(_needed_endpoint_names)
    if not names:
        names = {endpoint_for_domain(d).name for d in BROWSER_DOMAINS if is_domain_enabled(d)}
    return [ep for ep in (_CHROME, _CHROME2) if ep.name in names]


def _endpoint_lock(ep: _CdpEndpoint) -> asyncio.Lock:
    if ep.lock is None:
        ep.lock = asyncio.Lock()
    return ep.lock


def _consume_cdp_connect_result(task) -> None:

    if not task.cancelled():
        task.exception()


async def _reset_playwright_driver(ep: _CdpEndpoint | None = None) -> None:

    ep = ep or _CHROME
    pw, ep.pw = ep.pw, None
    if pw is None:
        return
    try:
        await asyncio.wait_for(pw.stop(), timeout=5.0)
    except Exception as exc:
        logger.debug("⚠️ [%s-CDP] dừng driver Playwright lỗi: %s", ep.label, exc)


async def _connect_endpoint(ep: _CdpEndpoint):
    try:
        # A listening CDP port may still belong to the previous Chrome session.
        # Validate the browser before attaching or touching any existing tab.
        await asyncio.to_thread(chrome_launcher.assert_edge_endpoint, ep.port, ep.host)
        if ep.pw is None:
            ep.pw = await async_playwright().start()
        url = f"http://{ep.host}:{ep.port}"
        timeout = max(5.0, float(getattr(Config, "CHROME_CDP_CONNECT_TIMEOUT_SECONDS", 45.0)))
        browser = await ep.pw.chromium.connect_over_cdp(url, timeout=int(timeout * 1000))
        if not browser.contexts:
            raise RuntimeError(f"{ep.label} CDP: no existing browser context; open {ep.label} manually")
        ep.browser = browser
        ep.context = browser.contexts[0]
        ep.last_error = None
        ep.retry_at = 0.0
        ep.failure_count = 0
        logger.info("✅ [%s-CDP] Dùng context hiện có (%s tab); không tạo browser/tab mới", ep.label, len(ep.context.pages))
        return ep.context
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        ep.browser = None
        ep.context = None
        ep.last_error = exc
        ep.failure_count += 1
        base_cooldown = max(2.0, float(getattr(Config, "CHROME_CDP_RECONNECT_COOLDOWN_SECONDS", 5.0)))
        cooldown = min(60.0, base_cooldown * (2 ** min(ep.failure_count - 1, 4)))
        ep.retry_at = time.monotonic() + cooldown
        logger.error("❌ [%s-CDP] Attach thất bại (cổng %s): %s. Chờ %.1fs; watchdog sẽ kiểm tra phục hồi.",
                     ep.label, ep.port, exc, cooldown)

        await _reset_playwright_driver(ep)
        if ep.failure_count in (3, 10, 30):
            logger.critical(
                "🛑 [%s-CDP] Đã %s lần attach lỗi liên tiếp (cổng %s). "
                "Hãy THOÁT HẲN %s của bot (kiểm tra Task Manager) rồi chạy lại run.bat.",
                ep.label, ep.failure_count, ep.port, ep.label,
            )
        raise


async def get_context_for_endpoint(ep: _CdpEndpoint, force_reconnect: bool = False):

    if ep.is_connected():
        return ep.context
    async with _endpoint_lock(ep):
        if ep.is_connected():
            return ep.context
        if ep.connect_task is None or ep.connect_task.done():
            remaining = ep.retry_at - time.monotonic()
            if remaining > 0 and not force_reconnect:
                raise RuntimeError(f"CDP attach cooldown: retry in {remaining:.1f}s") from ep.last_error
            ep.connect_task = asyncio.create_task(_connect_endpoint(ep), name=f"{ep.name}-cdp-attach")
            ep.connect_task.add_done_callback(_consume_cdp_connect_result)
        pending = ep.connect_task

    return await asyncio.shield(pending)


async def get_or_launch_browser_context(user: str = "shared", force_reconnect: bool = False, domain: str = ""):
    """Reuse one context per CDP endpoint; all XX88 tabs already share cookies."""
    return await get_context_for_endpoint(endpoint_for_domain(domain), force_reconnect)


def _listening_pid(port: int) -> int:

    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return int(c.pid)
    except Exception:
        pass
    return 0


def _find_browser_hwnd(ep: _CdpEndpoint | None = None) -> int:

    ep = ep or _CHROME
    user32 = ctypes.windll.user32
    if ep.hwnd:
        if user32.IsWindow(ep.hwnd):
            return ep.hwnd
        ep.hwnd = 0

    # A failed lookup walks every TCP connection / process. Even offloaded,
    # avoid repeating that scan on every submit while the window is absent.
    if time.monotonic() < ep.hwnd_retry_at:
        return 0
    hwnd = _scan_browser_hwnd(ep, user32)
    if not hwnd:
        ep.hwnd_retry_at = time.monotonic() + 15.0
    return hwnd


def _scan_browser_hwnd(ep: _CdpEndpoint, user32) -> int:
    pid = _listening_pid(ep.port)
    if not pid:
        try:
            import psutil
            roots = []
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    if str(proc.info.get('name') or '').lower() != 'msedge.exe':
                        continue
                    if f'--remote-debugging-port={ep.port}' in proc.cmdline():
                        roots.append(int(proc.pid))
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
            if len(roots) == 1:
                pid = roots[0]
        except Exception:
            pass
    if not pid:
        return 0
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        # Owner check first: most top-level windows belong to other processes.
        owner = ctypes.wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w > 100 and h > 50:
            found.append((w * h, hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if not found:
        return 0
    found.sort(key=lambda x: x[0], reverse=True)
    ep.hwnd = found[0][1]
    return ep.hwnd


def chrome_restore(domain: str = ""):

    ep = endpoint_for_domain(domain)
    try:
        # These calls run in executor threads; serialize cache scans and the
        # restore throttle for domains sharing the same Edge endpoint.
        with ep.window_lock:
            now = time.monotonic()
            if now - ep.last_restore_at < 2.0:
                return
            ep.last_restore_at = now
            hwnd = _find_browser_hwnd(ep)
            if hwnd:
                user32 = ctypes.windll.user32
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)
                logger.debug("🔼 %s restored; active tab selected by Playwright", ep.label)
    except Exception as e:
        logger.debug(f"browser_restore error: {e}")


def _manual_verification_foreground_enabled(domain: str) -> bool:
    if not bool(getattr(Config, "MANUAL_VERIFICATION_FOREGROUND", True)):
        return False
    if bool(getattr(Config, "BROWSER_FOREGROUND_ON_SUBMIT", True)):
        return True
    configured = str(
        getattr(
            Config,
            "MANUAL_VERIFICATION_FOREGROUND_DOMAINS",
            "tangquaqq88.com,hi88-freecode.pages.dev",
        )
        or ""
    )
    domains = {item.strip().lower() for item in configured.split(",") if item.strip()}
    return str(domain or "").lower() in domains


def _force_chrome_window_foreground(domain: str = "") -> bool:

    try:
        ep = endpoint_for_domain(domain)
        with ep.window_lock:
            now = time.monotonic()
            if now - ep.last_foreground_at < 2.0:
                return ep.last_foreground_ok
            ep.last_foreground_at = now

            hwnd = _find_browser_hwnd(ep)
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)
            else:
                user32.ShowWindow(hwnd, 5)
            try:
                user32.BringWindowToTop(hwnd)
            except Exception:
                pass
            try:
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            if user32.GetForegroundWindow() != hwnd:
                # Windows can deny activation while the user types elsewhere.
                # Raise the selected browser visibly without injecting keys or
                # leaving it permanently above the user's other applications.
                flags = 0x0001 | 0x0002 | 0x0010 | 0x0040
                was_topmost = bool(user32.GetWindowLongW(hwnd, -20) & 0x0008)
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)
                if not was_topmost:
                    user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)
                user32.SetForegroundWindow(hwnd)
                if user32.GetForegroundWindow() != hwnd:
                    user32.FlashWindow(hwnd, True)
            ep.last_foreground_ok = bool(user32.GetForegroundWindow() == hwnd)  # PATCH_MARKER_2026-10-26-v10-foreground-throttle
            return ep.last_foreground_ok
    except Exception as exc:
        logger.debug(f"manual foreground error: {exc}")
        return False


async def _show_manual_verification_page(page, domain: str, tag: str = "") -> bool:

    if page is None or safe_is_closed(page) or not _manual_verification_foreground_enabled(domain):
        return False
    pending = _pending_foreground_target()
    if pending is not None and pending[0] is not page:
        return False  # Keep the challenge the user is currently completing visible.
    try:
        await asyncio.to_thread(chrome_restore, domain)
        await page.bring_to_front()
    except Exception as exc:
        logger.debug(f"⚠️ [{domain}] bring_to_front lỗi: {exc}")
    foreground = await asyncio.to_thread(_force_chrome_window_foreground, domain)
    logger.warning(
        "👁️ %s%s | foreground=%s — hãy hoàn tất Cloudflare/captcha nếu được yêu cầu",
        f"{tag} " if tag else "",
        domain,
        foreground,
    )
    return foreground


async def _cf_already_passed(page, domain: str = "") -> bool:

    try:
        state = await page.evaluate(
            """
            () => {
                const successMarkers = [
                    'xác thực thành công', 'xac thuc thanh cong',
                    'verification successful', 'verified',
                ];
                const token = [...document.querySelectorAll(
                    'textarea[name="g-recaptcha-response"], textarea[name*="recaptcha"], '
                    + 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
                    + '[data-cf-turnstile-response]'
                )].some((el) => String(
                    el.value || el.textContent || el.getAttribute('data-cf-turnstile-response') || ''
                ).trim().length > 20);
                // Only read innerText (forces layout) when no solved token exists.
                let marker = false;
                if (!token) {
                    const text = (document.body.innerText || '').toLowerCase();
                    marker = successMarkers.some((value) => text.includes(value));
                }
                return {token, marker};
            }
            """
        )
        return bool(state and (state.get("token") or state.get("marker")))
    except Exception:
        return False


async def _wait_for_cloudflare_passed_and_cleanup(page, domain: str = "", timeout_seconds: float | None = None) -> bool:

    wait_seconds = float(
        timeout_seconds
        if timeout_seconds is not None
        else getattr(Config, "VERIFICATION_BUTTON_WAIT_SECONDS", 1.5)
    )
    try:

        handle = await page.wait_for_function(
            """
            () => {
                const token = [...document.querySelectorAll(
                    'textarea[name="g-recaptcha-response"], textarea[name*="recaptcha"], '
                    + 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
                    + '[data-cf-turnstile-response]'
                )].some((el) => String(
                    el.value || el.textContent || el.getAttribute('data-cf-turnstile-response') || ''
                ).trim().length > 20);
                if (token) return true;
                // Polled every 100ms: only force layout when the token is absent.
                const text = (document.body.innerText || '').toLowerCase();
                return [
                    'xác thực thành công', 'xac thuc thanh cong',
                    'verification successful', 'verified',
                ].some((value) => text.includes(value));
            }
            """,
            timeout=min(8000.0, max(500.0, wait_seconds * 1000.0)),
            polling=100,
        )
        try:
            await handle.dispose()
        except Exception:
            pass
    except Exception:
        return False
    await _close_unwanted_popups(page)
    return True


async def is_cloudflare_present(page, domain: str = "") -> bool:
    try:
        return bool(await page.evaluate(_CF_STATE_JS))
    except Exception:
        return False


async def safe_is_visible(element) -> bool:
    try:
        return await element.is_visible()
    except Exception:
        return False


def _poll_interval(started: float, fast: float = 0.05, slow: float = 0.25, fast_window: float = 3.0) -> float:
    """Poll quickly right after an event, then back off for long manual waits."""
    return fast if time.monotonic() - started < fast_window else slow


def safe_is_closed(page) -> bool:
    try:
        if page is None:
            return True
        return page.is_closed()
    except Exception:
        return True


def _invalidate_input_cache(key: str):
    bot_state._input_cache.pop(key, None)


def _invalidate_page_input_cache(page) -> None:
    suffix = f"|{id(page)}"
    for cache_key in [key for key in list(bot_state._input_cache) if key.endswith(suffix)]:
        bot_state._input_cache.pop(cache_key, None)


async def find_input_fields(page, cache_key: str = None, domain: str = ""):
    now = time.time()

    if cache_key:
        cached = bot_state._input_cache.get(cache_key)
        if cached:
            username_input, code_input, cache_time = cached
            if now - cache_time < bot_state._input_cache_ttl:

                if code_input:
                    return username_input, code_input
                _invalidate_input_cache(cache_key)

    username_input = None
    code_input = None
    profile = get_site_profile(domain)
    domain_username_selectors = list(profile.username_selectors) if profile else []

    username_selectors = domain_username_selectors + [
        "#account-code", "#username-input", "#ten_tai_khoan",
        "input#username", "input[name='username']",
        "input[placeholder*='người dùng' i]", "input[placeholder*='tên' i]",
        "input[placeholder*='tài' i]", "input[placeholder*='tài khoản' i]",
        "input[placeholder*='user' i]", "input[placeholder*='đăng nhập' i]",
        "input[name='ten_tai_khoan']", "input[id='username']", "input[type='text']",
    ]

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


async def _response_to_result_text(response, *, message_only: bool = False) -> str:
    try:
        payload = await response.json()
    except Exception:
        if message_only:
            # An error page may contain scripts, form fields or other data
            # unrelated to the server's explanation of this submission.
            return ""
        try:
            return (await response.text()).strip()
        except Exception:
            return ""

    preferred_keys = (
        "message",
        "msg",
        "error",
        "detail",
        "result",
        "status",
        "content",
        "description",
    )
    parts: list[str] = []

    if message_only:
        # PATCH_MARKER_2026-10-25-v8-bugfix (#3: API tra {"message": [{"text": ...}]} thi "text"
        # khong nam trong message_keys nen bi mat; them text/content/
        # reason. Chi nhanh diagnostic nay; nhanh decisive giu nguyen
        # de khong doi hanh vi phan loai.)
        message_keys = {"message", "msg", "error", "detail", "description", "errors", "text", "content", "reason"}
        envelope_keys = {"data", "result", "response"}

        def collect_messages(value, depth: int = 0, allow_text: bool = False) -> None:
            if value is None or depth > 5:
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    name = str(key).lower()
                    if name in message_keys:
                        collect_messages(item, depth + 1, True)
                    elif name in envelope_keys and isinstance(item, (dict, list, tuple)):
                        collect_messages(item, depth + 1)
            elif isinstance(value, (list, tuple)):
                for item in value[:20]:
                    collect_messages(item, depth + 1, allow_text)
            elif allow_text and isinstance(value, str):
                text = " ".join(value.split())[:1000]
                if text and text not in parts:
                    parts.append(text)

        collect_messages(payload)
        return " ".join(parts)[:1000]

    # PATCH_MARKER_2026-10-25-v7-apitimer-std: chi lay chuoi thong bao
    # (message/status) va envelope data/result/response, KHONG gom moi
    # scalar trong JSON nhu cu. Ban cu gom ca so diem, id, timestamp vao
    # text (RR88: '... 38 diem 38'), gay nhieu va co the lam classifier
    # doc nham khi response lan ca tu khoa that bai o field khong lien
    # quan. Ban nay RR88 chi con 2 cau message, van SUCCESS_POINTS.
    _MESSAGE_KEYS = ("message", "msg", "error", "detail", "description", "errors", "content")
    _ENVELOPE_KEYS = ("data", "result", "response")

    def collect(value, depth: int = 0) -> None:
        if value is None or depth > 5:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                name = str(key).lower()
                if name in _MESSAGE_KEYS:
                    collect_message(item, depth + 1)
                elif name in _ENVELOPE_KEYS:
                    if isinstance(item, (dict, list, tuple)):
                        collect(item, depth + 1)
                    elif isinstance(item, str):
                        text = " ".join(item.split())
                        if text and text not in parts:
                            parts.append(text)
                elif name == "status" and isinstance(item, str):
                    text = " ".join(item.split())
                    if text and text not in parts:
                        parts.append(text)
        elif isinstance(value, (list, tuple)):
            for item in value[:20]:
                collect(item, depth + 1)
        elif isinstance(value, str):
            text = " ".join(value.split())
            if text and text not in parts:
                parts.append(text)

    def collect_message(value, depth: int = 0) -> None:
        if value is None or depth > 5:
            return
        if isinstance(value, str):
            text = " ".join(value.split())
            if text and text not in parts:
                parts.append(text)
        elif isinstance(value, (list, tuple)):
            for item in value[:20]:
                collect_message(item, depth + 1)
        elif isinstance(value, dict):
            collect(value, depth + 1)

    collect(payload)
    return " ".join(parts)[:4000]





_TURNSTILE_HOST_STATE_JS = """
() => {
    const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
    };
    const hasWidget = [...document.querySelectorAll(
        "iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com'], "
        + "iframe[src*='recaptcha'], iframe[src*='google.com/recaptcha'], "
        + ".cf-turnstile, .g-recaptcha, [data-sitekey]"
    )].some(visible);
    const token = [...document.querySelectorAll(
        'textarea[name="g-recaptcha-response"], textarea[name*="recaptcha"], '
        + 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
        + '[data-cf-turnstile-response]'
    )].some((el) => String(
        el.value || el.textContent || el.getAttribute('data-cf-turnstile-response') || ''
    ).trim().length > 20);
    return {hasWidget, token};
}
"""

async def _turnstile_expired_hint(page) -> bool:
    try:
        # PATCH_MARKER_2026-10-25-v8-bugfix (#4: mo rong selector nut
        # "Thu lai" sang button khong type va [role="button"]; cac dieu
        # kien text/parent/captcha/het-han van giu nen khong bam nham.)
        return bool(await page.evaluate(r"""() => {
            const t = ((document.body && document.body.innerText) || '').toLowerCase();
            if (!/captcha[^\n]{0,40}(hết hạn|het han|expired)|(hết hạn|het han)[^\n]{0,40}captcha/.test(t)) return false;
            try {
                const api = window.turnstile;
                const n = document.querySelectorAll(
                    'input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]'
                ).length;
                if (n === 1 && api && typeof api.isExpired === 'function') {
                    // XX88 retains its expired banner while a fresh widget is active.
                    // Do not restart that widget just because the old label remains.
                    return api.isExpired() === true;
                }
            } catch (_) {}
            return true;
        }"""))
    except Exception:
        return False


async def _reset_expired_turnstile(page) -> bool:
    """Request a fresh challenge only when a single widget is identifiable."""
    try:
        return bool(await page.evaluate(r"""() => {
            const norm = value => String(value || '').normalize('NFD')
                .replace(/[\u0300-\u036f]/g, '').replace(/đ/g, 'd').trim().toLowerCase();
            for (const button of document.querySelectorAll('button[type="button"], button:not([type]), [role="button"]')) {
                const parent = button.parentElement;
                const text = norm(parent && parent.innerText);
                const rect = button.getBoundingClientRect();
                if (norm(button.innerText) === 'thu lai' && text.length < 160
                    && /captcha/.test(text) && /het han|expired/.test(text)
                    && !button.disabled && rect.width > 0 && rect.height > 0) {
                    button.click(); // Keep the site's React state and widget lifecycle in sync.
                    return true;
                }
            }
            const api = window.turnstile;
            if (!api || typeof api.reset !== 'function') return false;
            const n = document.querySelectorAll(
                'input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]'
            ).length;
            if (n !== 1) return false;
            api.reset();
            return true;
        }"""))
    except Exception:
        return False


_xx88_challenge_retry_at = weakref.WeakKeyDictionary()
_xx88_ready_tasks = weakref.WeakKeyDictionary()
_DOMAIN_DISPATCH_PAUSE_UNTIL: dict[str, float] = {}


def pause_domain_dispatch(domain: str, seconds: float = 5.0) -> None:
    domain = _normalize_domain(domain)
    _DOMAIN_DISPATCH_PAUSE_UNTIL[domain] = max(
        _DOMAIN_DISPATCH_PAUSE_UNTIL.get(domain, 0.0),
        time.monotonic() + max(0.0, float(seconds)),
    )


def domain_dispatch_pause_remaining(domain: str) -> float:
    domain = _normalize_domain(domain)
    remaining = _DOMAIN_DISPATCH_PAUSE_UNTIL.get(domain, 0.0) - time.monotonic()
    if remaining <= 0:
        _DOMAIN_DISPATCH_PAUSE_UNTIL.pop(domain, None)
        return 0.0
    return remaining


def _xx88_readiness_pending(page) -> bool:
    task = _xx88_ready_tasks.get(page)
    return task is not None and not task.done()


def _arm_xx88_readiness(page, *, need_button=False) -> None:
    """Share one pre-submit readiness probe across all codes using this page."""
    if _xx88_readiness_pending(page):
        return

    async def watch():
        started = time.monotonic()
        # PATCH_MARKER_2026-10-25-v5-xx88-tabs-queue: giu cua so Edge XX88 o
        # truoc khi cho Turnstile. Tab bi minimize/che thi challenge co the
        # khong chay tiep, lam thoi gian XX88-Ready do bi lech. Chi khang
        # dinh foreground MOT lan luc arm (khong lap lai moi probe 0.75s de
        # khoi giat focus cua user lien tuc); duong submit van wake tab nhu cu.
        foreground_at_arm = None
        try:
            foreground_at_arm = await _wake_tab_for_submit(page, "xx88code.com")
        except Exception:
            foreground_at_arm = False
        logger.info("[XX88-Ready] giu cua so Edge o truoc khi cho xac minh: foreground=%s",
                    foreground_at_arm)
        try:
            # Bounded: a changed form must eventually be checked by the normal
            # submission path again. The readiness helper may renew an expired
            # challenge; this watcher never fills/submits codes or reloads tabs.
            while not safe_is_closed(page) and time.monotonic() - started < 30.0:
                await asyncio.sleep(.75)
                if await asyncio.wait_for(
                    _wait_for_turnstile_ready(page, "xx88code.com", 0), timeout=4.0
                ) and (not need_button or await asyncio.wait_for(
                    _submit_button_ready(page, "xx88code.com"), timeout=2.0
                )):
                    logger.info("[XX88-Ready] pre-submit verification ready after %.1fs (foreground_at_arm=%s)",
                                time.monotonic() - started, foreground_at_arm)
                    return
            if not safe_is_closed(page):
                logger.info("[XX88-Ready] het 30s cho van chua san sang (foreground_at_arm=%s)",
                            foreground_at_arm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[XX88-Ready] probe interrupted: %s", type(exc).__name__)
        finally:
            _xx88_ready_tasks.pop(page, None)
            if _tab_pool is not None:
                _tab_pool._notify_available("xx88code.com")

    _xx88_ready_tasks[page] = asyncio.create_task(watch(), name="xx88-pre-submit-ready")


async def _xx88_cookie_clearance(page):
    try:
        cookies = await page.context.cookies(["https://xx88code.com/"])
        now = time.time()
        return any(c.get("name") == "cf_clearance" and c.get("value")
                   and (float(c.get("expires", -1)) <= 0 or float(c["expires"]) > now)
                   for c in cookies)
    except Exception:
        return False


async def _xx88_submit_readiness(page):
    """Return (may_attempt_submit, cookie_path), never fabricate a business result."""
    tokens = await _challenge_token_snapshot(page)
    if tokens.get("turnstile") and not _challenge_token_consumed(page, "turnstile", tokens["turnstile"]):
        if await _wait_for_turnstile_ready(page, "xx88code.com", 0):
            return True, False
    ready = await _wait_for_challenge_token_retries(page, "xx88code.com")
    if ready:
        return True, False
    if await _xx88_cookie_clearance(page):
        ASYNC_SUBMIT_COUNTS["TURNSTILE_SKIP_VIA_COOKIE"] += 1
        logger.info("TURNSTILE_SKIP_VIA_COOKIE domain=xx88 total=%s",
                    ASYNC_SUBMIT_COUNTS["TURNSTILE_SKIP_VIA_COOKIE"])
        return True, True
    return False, False


async def _wait_for_challenge_token_retries(page, domain: str) -> bool:
    for attempt, wait_seconds in enumerate((3.0, 5.0, 8.0), start=1):
        deadline = time.monotonic() + wait_seconds
        while not safe_is_closed(page):
            try:
                state = await page.evaluate(_TURNSTILE_HOST_STATE_JS)
            except Exception:
                state = None
            if state and state.get("token"):
                tokens = await _challenge_token_snapshot(page)
                used = _used_challenge_tokens.get(page, {})
                if tokens and any(used.get(kind) != digest for kind, digest in tokens.items()):
                    return True
            if state and not state.get("hasWidget"):
                return domain != "xx88code.com"
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.15)
        logger.info("CHALLENGE_TOKEN_WAIT domain=%s attempt=%s wait_seconds=%s ready=false",
                    domain, attempt, wait_seconds)
    return False


class BrowserVerificationPending(RuntimeError):
    pass


async def _wait_for_turnstile_ready(page, domain: str, timeout_s: float) -> bool:

    deadline = time.monotonic() + max(0.0, float(timeout_s))
    ready_js = """
    () => {
        const nodes = document.querySelectorAll(
            "iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com'], "
            + ".cf-turnstile, [data-sitekey], input[name='cf-turnstile-response'], "
            + "textarea[name='cf-turnstile-response']"
        );
        const tokenNodes = document.querySelectorAll(
            "input[name='cf-turnstile-response'], textarea[name='cf-turnstile-response'], "
            + "[data-cf-turnstile-response]"
        );
        const token = [...tokenNodes].map((el) => String(
            el.value || el.textContent || el.getAttribute('data-cf-turnstile-response') || ''
        ).trim()).find(value => value.length > 20) || '';
        let expired = false;
        try {
            if (tokenNodes.length === 1 && typeof window.turnstile?.isExpired === 'function')
                expired = window.turnstile.isExpired() === true;
        } catch (_) {}
        return {hasWidget: nodes.length > 0, token, expired};
    }
    """
    retry_at = _xx88_challenge_retry_at.get(page, 0.0) if domain == "xx88code.com" else 0.0
    recovered = retry_at > time.monotonic()
    if recovered:
        deadline = time.monotonic()  # Recheck readiness, without waiting/resetting per code.
    # Stall early-exit (submit-opt): with a long granted wait (>=1.5s) but no
    # observable progress - widget present, no fresh token, no successful
    # expiry reset - waiting out the full timeout never produced a token in
    # the 23/10 log (XX88 cf_wait was either <500ms or the full 4.0s). Bail
    # at CF_STALL_SECONDS so the code is deferred and the tab/worker freed;
    # the deferred retry rechecks while Turnstile keeps solving in the tab.
    stall_deadline = None
    if float(timeout_s) >= 1.5 and not recovered:
        stall_deadline = time.monotonic() + max(
            0.3, float(getattr(Config, "CF_STALL_SECONDS", 1.2))
        )
    checked_expiry = False
    while True:
        try:
            state = await page.evaluate(ready_js)
        except Exception:
            return False
        if not state or not state.get("hasWidget"):
            if not recovered:
                return True
            # reset() may briefly remove the widget; absence is not success.
        elif (state.get("token") and not state.get("expired")
              and not _challenge_token_consumed(page, "turnstile", state["token"])):
            if domain == "xx88code.com":
                _xx88_challenge_retry_at.pop(page, None)
            return True
        if not checked_expiry or time.monotonic() >= deadline:
            checked_expiry = True
            if not recovered and domain == "xx88code.com" and await _turnstile_expired_hint(page):
                recovered = True
                _xx88_challenge_retry_at[page] = time.monotonic() + 30.0
                if await _reset_expired_turnstile(page):
                    logger.info("🔁 [%s] yêu cầu trang khôi phục CAPTCHA hết hạn", domain)
                    deadline = time.monotonic() + 3.0
                    stall_deadline = None  # A reset actually happened: give it the full window.
                    continue
        if stall_deadline is not None and time.monotonic() >= stall_deadline:
            return False
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(max(0.05, float(getattr(Config, "CF_POLL_INTERVAL", 0.10))))


_PENDING_VERIFY_TASKS: dict = {}
_PENDING_VERIFY_PAGES: dict = {}
_CAPTCHA_OCCURRENCES: dict[str, deque[float]] = {}
_CAPTCHA_DOMAIN_PAUSED_UNTIL: dict[str, float] = {}
_CAPTCHA_TIMEOUT_SECONDS = 60.0


def _pending_foreground_target():
    for task_key, (page, domain) in list(_PENDING_VERIFY_PAGES.items()):
        task = _PENDING_VERIFY_TASKS.get(task_key)
        if task is None or task.done() or safe_is_closed(page):
            _PENDING_VERIFY_PAGES.pop(task_key, None)
            continue
        if _manual_verification_foreground_enabled(domain):
            return page, domain
    if _manual_verification_foreground_enabled("xx88code.com"):
        for page, task in list(_xx88_ready_tasks.items()):
            if not task.done() and not safe_is_closed(page):
                return page, "xx88code.com"
    return None


_used_challenge_tokens = weakref.WeakKeyDictionary()


def _challenge_token_consumed(page, kind, token):
    used = _used_challenge_tokens.get(page)
    return bool(used and isinstance(token, str)
                and used.get(kind) == hashlib.sha256(token.encode()).hexdigest())


async def _challenge_token_snapshot(page):
    try:
        values = await page.evaluate("""() => {
            const read = name => [...document.querySelectorAll(`input[name="${name}"],textarea[name="${name}"]`)]
                .map(el => (el.value || '').trim()).find(Boolean) || '';
            return {turnstile:read('cf-turnstile-response'),recaptcha:read('g-recaptcha-response')};
        }""")
        return {kind: hashlib.sha256(token.encode()).hexdigest()
                for kind, token in values.items() if isinstance(token, str) and token}
    except Exception:
        return None


async def _refresh_used_challenges(page):
    if page not in _used_challenge_tokens:
        return True
    used = _used_challenge_tokens[page]
    if used is None:
        return False
    if not used:
        # A successful pre-click snapshot found no consumed challenge token.
        # There is nothing to compare/reset, even if the site added a widget later.
        return True
    current = await _challenge_token_snapshot(page)
    if current is None:
        return False
    stale = [kind for kind, digest in used.items() if current.get(kind) == digest]
    if not stale:
        return True  # The website has already cleared/refreshed its challenge.
    try:
        reset = await page.evaluate("""kinds => {
            for (const kind of kinds) {
                const name=kind==='turnstile'?'cf-turnstile-response':'g-recaptcha-response';
                if (document.querySelectorAll(`input[name="${name}"],textarea[name="${name}"]`).length !== 1) return false;
                const api=kind==='turnstile'?window.turnstile:(window.grecaptcha?.enterprise || window.grecaptcha);
                if (!api || typeof api.reset!=='function') return false;
                api.reset();
            }
            return true;
        }""", stale)
        return bool(reset)
    except Exception:
        return False


_PENDING_VERIFY_RESULTS: dict[tuple[str, str, str], dict] = {}


def _verification_result_key(domain: str, account: str, code: str) -> tuple[str, str, str]:
    return (str(domain or "").lower(), str(account or "").lower(), normalize_code(code, domain))


def consume_pending_verification_result(domain: str, account: str, code: str) -> dict | None:

    return _PENDING_VERIFY_RESULTS.pop(_verification_result_key(domain, account, code), None)


def _record_captcha_occurrence(domain: str) -> None:
    domain = _normalize_domain(domain)
    if not domain:
        return
    now = time.monotonic()
    for tracked_domain, timestamps in list(_CAPTCHA_OCCURRENCES.items()):
        while timestamps and now - timestamps[0] > 300.0:
            timestamps.popleft()
        if not timestamps:
            _CAPTCHA_OCCURRENCES.pop(tracked_domain, None)
            _CAPTCHA_DOMAIN_PAUSED_UNTIL.pop(tracked_domain, None)
    while len(_CAPTCHA_OCCURRENCES) >= 256 and domain not in _CAPTCHA_OCCURRENCES:
        oldest_domain = next(iter(_CAPTCHA_OCCURRENCES))
        _CAPTCHA_OCCURRENCES.pop(oldest_domain, None)
        _CAPTCHA_DOMAIN_PAUSED_UNTIL.pop(oldest_domain, None)
    occurrences = _CAPTCHA_OCCURRENCES.setdefault(domain, deque())
    occurrences.append(now)
    if len(occurrences) <= 3:
        return
    if now < _CAPTCHA_DOMAIN_PAUSED_UNTIL.get(domain, 0.0):
        return
    _CAPTCHA_DOMAIN_PAUSED_UNTIL[domain] = now + 600.0
    pause_domain_dispatch(domain, 600.0)
    logger.warning("DOMAIN_PAUSED reason=too_many_captchas domain=%s", domain)


async def _send_captcha_admin_alert(
    domain: str, account: str, *, message: str | None = None,
) -> None:
    import sys

    admin_id = int(getattr(Config, "TELEGRAM_ADMIN_ID", 0) or 0)
    if not admin_id:
        logger.warning("Captcha admin alert skipped: TELEGRAM_ADMIN_ID is not configured")
        return
    telegram_client = None
    for module_name in ("__main__", "main_script"):
        module = sys.modules.get(module_name)
        telegram_client = getattr(module, "client", None) if module else None
        if telegram_client is not None:
            break
    if telegram_client is None or not telegram_client.is_connected():
        logger.warning("Captcha admin alert skipped: Telegram client is unavailable")
        return
    message = message or (
        f"⚠️ CẢNH BÁO: Domain [{domain}] đang kẹt captcha tại account [{account}]. "
        "Hãy vào giải cứu hoặc bot sẽ tự động reset tab sau 60s."
    )
    try:
        await asyncio.wait_for(
            telegram_client.send_message(admin_id, message),
            timeout=5.0,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Captcha admin alert failed domain=%s account=%s: %s", domain, account, exc)


def _verification_pending(page) -> bool:
    task = _PENDING_VERIFY_TASKS.get(id(page))
    return task is not None and not task.done()


async def _turnstile_solved(page) -> bool:

    try:
        state = await page.evaluate(_TURNSTILE_HOST_STATE_JS)
    except Exception:
        return False
    if not state:
        return False
    if state.get("token") or not state.get("hasWidget"):
        return True

    try:
        for frame in page.frames:
            if "challenges.cloudflare.com" not in (frame.url or ""):
                continue
            text = await frame.evaluate(
                "() => ((document.body && document.body.innerText) || '')"
            )
            low = str(text or "").lower()
            if any(m in low for m in ("thành công", "thanh cong", "success", "verified")):
                return True
    except Exception:
        pass
    return False


async def _verification_token_present(page) -> bool:

    try:
        state = await page.evaluate(_TURNSTILE_HOST_STATE_JS)
        return bool(state and state.get("token"))
    except Exception:
        return False


async def _wait_turnstile_solved(page, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        if safe_is_closed(page):
            return False
        if await _turnstile_solved(page):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.15)


async def _click_verify_button_now(page) -> bool:
    try:
        btn = page.get_by_role(
            "button", name=_re.compile(r"^\s*(xác thực|xac thuc|verify)\s*$", _re.I)
        ).last
        if await btn.count() and await btn.is_visible() and await btn.is_enabled():
            tokens = await _challenge_token_snapshot(page)
            await btn.click(timeout=2000, no_wait_after=True)
            _used_challenge_tokens[page] = tokens
            return True
    except Exception:
        pass
    try:
        tokens = await _challenge_token_snapshot(page)
        clicked = bool(await page.evaluate(
            """() => {
                const el = [...document.querySelectorAll('button,[role="button"]')].find((b) => {
                    const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                    const r = b.getBoundingClientRect();
                    return /^(xác thực|xac thuc|verify)$/.test(t) && r.width > 0 && !b.disabled;
                });
                if (!el) return false;
                el.click();
                return true;
            }"""
        ))
        if clicked:
            _used_challenge_tokens[page] = tokens
        return clicked
    except Exception:
        return False


async def _wait_and_click_verify_button(page, timeout_seconds: float | None = None) -> bool:

    wait_s = float(
        timeout_seconds
        if timeout_seconds is not None
        else getattr(Config, "VERIFY_BUTTON_POST_SOLVE_WAIT_SECONDS", 4.0)
    )
    deadline = time.monotonic() + max(0.25, wait_s)
    while not safe_is_closed(page) and time.monotonic() < deadline:
        if await _click_verify_button_now(page):
            return True

        await asyncio.sleep(0.10)
    # Absence of a challenge is not proof that the application button was clicked.
    return False


async def _wait_for_site_result_popup(page, domain: str, timeout_seconds: float | None = None) -> bool:

    profile = get_site_profile(domain) if domain else None
    selectors = list(getattr(profile, "popup_dismiss_selectors", ()) or ()) if profile else []
    texts = [str(t).strip().lower() for t in (getattr(profile, "popup_dismiss_texts", ()) or ()) if str(t).strip()] if profile else []
    if not selectors and not texts:
        return False
    wait_s = float(
        timeout_seconds
        if timeout_seconds is not None
        else getattr(Config, "VERIFICATION_RESULT_POPUP_WAIT_SECONDS", 3.0)
    )
    wait_s = max(0.5, min(15.0, wait_s))
    try:
        handle = await page.wait_for_function(
            r"""
            ([selectors, texts]) => {
                const isVisible = (el) => {
                    if (!el || !el.isConnected) return false;
                    const st = getComputedStyle(el);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const norm = (s) => String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
                const inOverlay = (el) => {
                    for (let n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
                        const role = n.getAttribute && n.getAttribute('role');
                        if (role === 'dialog' || role === 'alertdialog') return true;
                        if (n.getAttribute && n.getAttribute('aria-modal') === 'true') return true;
                        const st = getComputedStyle(n);
                        const cls = String(n.className || '');
                        if (st.position === 'fixed' || /modal|popup|overlay|dialog|backdrop|alert/i.test(cls)) return true;
                    }
                    return false;
                };
                for (const sel of selectors) {
                    try {
                        for (const el of document.querySelectorAll(sel)) {
                            if (isVisible(el)) return true;
                        }
                    } catch (_) {}
                }
                if (texts.length) {
                    for (const el of document.querySelectorAll('button,[role="button"],a')) {
                        if (!isVisible(el) || !inOverlay(el)) continue;
                        if (texts.includes(norm(el.innerText || el.textContent || el.getAttribute('aria-label')))) return true;
                    }
                }
                return false;
            }
            """,
            arg=[selectors, texts],
            timeout=wait_s * 1000.0,
            polling=100,
        )
        try:
            return bool(await handle.json_value())
        finally:
            try:
                await handle.dispose()
            except Exception:
                pass
    except Exception:
        return False


async def _cleanup_after_verification_watcher(
    page, domain: str, target_url: str, key: str, tag: str
) -> bool:

    popup_ready = await _wait_for_site_result_popup(page, domain)
    if popup_ready:
        logger.info(f"🧹 {tag} popup kết quả đã xuất hiện → chạy cleanup riêng của {domain}")
    else:
        logger.warning(
            f"⚠️ {tag} chưa thấy nút popup kết quả trong thời gian chờ — "
            "vẫn chạy cleanup chuẩn để không để overlay cũ che lượt sau"
        )

    cleaned = await _clean_page_after_submit(
        page, domain, target_url, key, force_full=False
    )
    if not cleaned:
        logger.warning(f"⚠️ {tag} cleanup chuẩn thất bại")
        return False

    post = await _close_unwanted_popups(page)
    if post.get("stuck"):
        logger.warning(f"⚠️ {tag} overlay vẫn còn sau cleanup → force reload")
        cleaned = await _clean_page_after_submit(
            page, domain, target_url, key, force_full=True
        )
    if cleaned:
        logger.info(f"✅ {tag} cleanup hoàn tất — tab sẵn sàng cho mã kế tiếp")
    return bool(cleaned)


async def _manual_verify_hint_now(page) -> str:

    try:
        text = await page.evaluate(
            """() => {
                const selectors = [
                    '[role="dialog"]', '[role="alertdialog"]', '.modal',
                    '[class*="captcha" i]', '[class*="verify" i]',
                    '[class*="dialog" i]'
                ];
                let out = '';
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        if (r.width <= 0 || r.height <= 0 || st.display === 'none' || st.visibility === 'hidden') continue;
                        out += ' ' + (el.innerText || el.textContent || '');
                        if (out.length > 1800) return out.slice(0, 1800);
                    }
                }
                return out.slice(0, 1800);
            }"""
        )
    except Exception:
        return ''
    text = str(text or '').strip()
    return text if _needs_manual_verify(text) else ''


async def _probe_v79_captcha_image(page) -> dict:

    try:
        return await page.evaluate(
            r"""
            () => {
                const visible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        st.display !== 'none' && st.visibility !== 'hidden';
                };
                const code = [...document.querySelectorAll('input')].find((el) => {
                    const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
                    return ph.includes('mã code') || ph.includes('ma code') || ac === 'one-time-code';
                });
                const submit = document.querySelector(
                    "img[alt='Kiểm tra ngay'], img[alt='KIỂM TRA NGAY'], " +
                    "button[aria-label='Kiểm tra ngay'], button[type='submit']"
                );
                const cr = code && visible(code) ? code.getBoundingClientRect() : null;
                const sr = submit && visible(submit) ? submit.getBoundingClientRect() : null;
                const images = [...document.images].filter(visible).filter((img) => {
                    const r = img.getBoundingClientRect();
                    const alt = (img.getAttribute('alt') || '').toLowerCase();
                    const src = (img.getAttribute('src') || '').toLowerCase();
                    const cls = String(img.className || '').toLowerCase();
                    const sig = `${alt} ${src} ${cls}`;
                    if (/kiểm tra ngay|kiem tra ngay|xác nhận|xac nhan|logo|icon|avatar|coin|gift|btn/.test(sig)) return false;
                    const sizeOk = r.width >= 40 && r.width <= 520 && r.height >= 20 && r.height <= 220;
                    if (!sizeOk) return false;
                    const inBand = !!(cr && sr && r.top >= (cr.bottom - 20) && r.bottom <= (sr.top + 40));
                    const named = /captcha|verify|verification|xac.?thuc|ma.?xac/.test(sig);
                    return inBand || named;
                });
                if (!images.length) return {state: 'none'};
                const ready = images.find((img) => img.complete && img.naturalWidth > 4 && img.naturalHeight > 4);
                if (ready) return {state: 'ready', src: ready.currentSrc || ready.src || '', width: ready.naturalWidth, height: ready.naturalHeight};
                const broken = images.find((img) => img.complete && (!img.naturalWidth || !img.naturalHeight));
                if (broken) return {state: 'broken', src: broken.currentSrc || broken.src || '', alt: broken.alt || ''};
                return {state: 'loading'};
            }
            """
        )
    except Exception as exc:
        return {"state": "unknown", "error": str(exc)}


async def _ensure_v79_captcha_image_ready(page, *, target_url: str = "") -> tuple[bool, bool]:

    wait_s = max(0.4, float(getattr(Config, "V79_CAPTCHA_LOAD_WAIT_SECONDS", 1.5)))
    retries = max(0, int(getattr(Config, "V79_CAPTCHA_RELOAD_RETRIES", 2)))
    did_reload = False
    for attempt in range(retries + 1):
        deadline = time.monotonic() + wait_s
        last = {"state": "unknown"}
        while time.monotonic() < deadline:
            last = await _probe_v79_captcha_image(page)
            state = str(last.get("state", "unknown"))
            if state in {"ready", "none"}:
                if state == "ready":
                    logger.debug("✅ [V79] CAPTCHA image ready: %sx%s", last.get("width"), last.get("height"))
                return True, did_reload
            if state == "broken":
                break
            await asyncio.sleep(0.12)

        if attempt >= retries:
            logger.warning("🖼️ [V79] CAPTCHA image vẫn lỗi sau %s lần reload — giữ tab, không submit", retries)
            return False, did_reload

        logger.warning("🔄 [V79] CAPTCHA image tải lỗi — reload form lấy CAPTCHA mới (%s/%s)", attempt + 1, retries)
        try:
            await page.reload(
                wait_until="domcontentloaded",
                timeout=int(float(_site_profile_value("v79code.com", "navigation_timeout_seconds", 12.0)) * 1000),
            )
            did_reload = True
            await asyncio.sleep(0.20)
        except Exception as exc:
            logger.warning("⚠️ [V79] reload CAPTCHA lỗi: %s", exc)
            return False, did_reload
    return False, did_reload


def _manual_image_captcha_text(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in (
        "nhập đúng mã trong ảnh", "nhap dung ma trong anh",
        "nhập mã trong ảnh", "nhap ma trong anh",
        "captcha ảnh", "captcha anh",
    ))


async def _site_popup_visible_now(page, domain: str) -> bool:

    profile = get_site_profile(domain) if domain else None
    selectors = list(getattr(profile, "popup_dismiss_selectors", ()) or ()) if profile else []
    texts = [
        str(t).strip().lower()
        for t in (getattr(profile, "popup_dismiss_texts", ()) or ())
        if str(t).strip()
    ] if profile else []
    try:
        return bool(await page.evaluate(
            r"""
            ([selectors, texts]) => {
                const visible = (el) => {
                    if (!el || !el.isConnected) return false;
                    const st = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return st.display !== 'none' && st.visibility !== 'hidden'
                        && r.width > 0 && r.height > 0;
                };
                for (const sel of selectors || []) {
                    try {
                        if ([...document.querySelectorAll(sel)].some(visible)) return true;
                    } catch (_) {}
                }
                const norm = (s) => String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
                if (texts && texts.length) {
                    const nodes = [...document.querySelectorAll('button,[role="button"],a,input[type="button"],input[type="submit"]')];
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const t = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label'));
                        if (texts.some((x) => t === x || t.startsWith(x + ' '))) return true;
                    }
                }
                return false;
            }
            """,
            [selectors, texts],
        ))
    except Exception:
        return False


async def wait_for_pending_verification(
    domain: str, account: str | None = None, timeout_seconds: float | None = None
) -> bool:

    pool = _tab_pool
    if pool is None:
        return True
    try:
        entry = None
        if account:
            entry = pool._account_tabs.get(domain, {}).get(str(account))
        if entry is None:
            entries = pool._domains.get(domain, [])
            entry = entries[0] if len(entries) == 1 else None
        if entry is None:
            return True
        task = _PENDING_VERIFY_TASKS.get(id(entry.get("page")))
        if task is None or task.done():
            return True
        wait_s = float(
            timeout_seconds
            if timeout_seconds is not None
            else getattr(Config, "PENDING_VERIFY_WORKER_WAIT_SECONDS", 210.0)
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(1.0, wait_s))
            return True
        except asyncio.TimeoutError:
            return False
    except Exception as exc:
        logger.debug(f"⚠️ [{domain}|{account}] wait pending verification lỗi: {exc}")
        return False


def arm_verification_watcher(
    page, domain: str = "", code: str = "", user: str = "",
    target_url: str = "", key: str = "", systems: dict | None = None,
    *, llwin_slider_already_attempted: bool = False,
) -> None:

    if page is None or safe_is_closed(page):
        return
    pause_domain_dispatch(domain, 5.0)
    task_key = id(page)
    old = _PENDING_VERIFY_TASKS.get(task_key)
    if old is not None and not old.done():
        return
    _record_captcha_occurrence(domain)

    async def _run():
        tag = f"[Verify-Watch|{domain}|{user}|{code}]"
        profile = get_site_profile(domain) if domain else None
        cleanup_url = target_url or (getattr(profile, "origin", "") if profile else "")
        cleanup_key = key or f"{domain}|{user}"
        result_key = _verification_result_key(domain, user, code)

        watch_api_timer = _ApiTimer(
            page, getattr(profile, "public_api_endpoint", None),
            domain=domain, submitted_code=code, submitted_account=user,
        )
        watch_api_timer.attach()
        # Requests observed after arming belong to this verification attempt.
        # Some image/slider widgets send directly from the user's confirmation.
        watch_api_timer.mark_click()
        watch_started = time.time()
        watch_s = min(
            _CAPTCHA_TIMEOUT_SECONDS,
            max(1.0, float(getattr(Config, "PENDING_VERIFY_WATCH_SECONDS", 180.0))),
        )
        deadline = time.monotonic() + watch_s

        async def _force_reset(reason: str) -> None:
            if (
                domain in {"tangquaqq88.com", "hi88-freecode.pages.dev", "llwincode.com"}
                and cleanup_url
                and not safe_is_closed(page)
            ):
                logger.warning(f"♻️ {tag} {reason} → reload tab để xoá trạng thái xác thực/popup cũ")
                await _clean_page_after_submit(
                    page, domain, cleanup_url, cleanup_key, force_full=True
                )

        try:
            await _send_captcha_admin_alert(domain, user)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    await asyncio.wait_for(
                        _show_manual_verification_page(page, domain, tag),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    pass
            poll_started = time.monotonic()
            image_notice_logged = False
            # PATCH_MARKER_2026-10-26-llwin-slider-logs2: luoi an toan cho
            # truong hop slider hien MUON, sau khi submit flow da chuyen
            # sang watcher (hoac manual-hint bat truoc). Chi thu 1 lan;
            # giai xong thi vong lap se tu bat POST/popup, giai hong thi
            # roi ve giu tab cho user keo tay nhu cu, khong lap lai.
            llwin_slider_attempted = llwin_slider_already_attempted
            progressed = False

            while not safe_is_closed(page) and time.monotonic() < deadline:

                if _completed_api_result(watch_api_timer.text_future):
                    progressed = True
                    break
                if await _site_popup_visible_now(page, domain):
                    progressed = True
                    break

                try:
                    body = await page.evaluate(
                        "() => ((document.querySelector('[role=dialog],.modal,[role=alertdialog],main,#app,body')"
                        " || document.body).innerText || '').slice(0, 1600)"
                    )
                except Exception:
                    body = ""

                if domain == "llwincode.com":
                    # PATCH_MARKER_2026-10-26-llwin-slider-logs2: thu giai
                    # tu dong 1 lan truoc khi giu tab cho user. Preserve
                    # the visible slider until it is solved (by bot or by
                    # user): its callback sends the API request, so do not
                    # click the form/Turnstile controls or repeatedly
                    # reopen the puzzle.
                    if not llwin_slider_attempted:
                        try:
                            if await is_llwin_slider_visible(page):
                                llwin_slider_attempted = True
                                _w_dbg = tempfile.mkdtemp(prefix=f"llwin_slider_watch_{user}_{code}_")
                                logger.info(f"\U0001f9e9 [Verify-Watch|{domain}|{user}|{code}] slider hien muon \u2014 dang giai (debug: {_w_dbg})")
                                _w_ok = await solve_llwin_slider(
                                    page,
                                    debug_dir=_w_dbg,
                                    on_os_mouse_required=lambda: _send_captcha_admin_alert(
                                        domain, user, message=OS_MOUSE_WARNING,
                                    ),
                                    on_manual_required=lambda: _send_captcha_admin_alert(
                                        domain, user,
                                        message=(
                                            "⚠️ [LLWIN] YÊU CẦU KÉO CAPTCHA! "
                                            "Hãy dùng chuột thật kéo thanh trượt trên màn hình ngay bây giờ."
                                        ),
                                    ),
                                )
                                logger.info(f"\U0001f9e9 [Verify-Watch|{domain}|{user}|{code}] slider {'QUA' if _w_ok else 'CHUA QUA'} (debug: {_w_dbg})")
                        except Exception as _w_exc:
                            logger.warning(f"\u26a0\ufe0f [Verify-Watch|{domain}|{user}|{code}] giai slider loi: {_w_exc}")
                    if not image_notice_logged:
                        logger.warning("🖐️ %s chờ kéo thanh trượt LLwin — giữ nguyên tab", tag)
                        image_notice_logged = True
                    await asyncio.sleep(_poll_interval(poll_started))
                    continue
                if await _verification_token_present(page):
                    watch_api_timer.mark_click()
                    if await _wait_and_click_verify_button(
                        page,
                        timeout_seconds=float(
                            getattr(Config, "VERIFY_BUTTON_POST_SOLVE_WAIT_SECONDS", 4.0)
                        ),
                    ):
                        logger.info(f"✅ {tag} reCAPTCHA/Turnstile đã xong → đã bấm Xác thực")
                        progressed = True
                        break

                if _manual_image_captcha_text(body):
                    if not image_notice_logged:
                        logger.warning(
                            f"🖐️ {tag} đang chờ captcha ảnh thủ công — giữ nguyên tab, không reload"
                        )
                        image_notice_logged = True
                    await asyncio.sleep(_poll_interval(poll_started))
                    continue

                if await _turnstile_solved(page):
                    watch_api_timer.mark_click()
                    if await _wait_and_click_verify_button(
                        page,
                        timeout_seconds=float(
                            getattr(Config, "VERIFY_BUTTON_POST_SOLVE_WAIT_SECONDS", 4.0)
                        ),
                    ):
                        logger.info(f"✅ {tag} xác minh đã xong → tiếp tục bước kết quả")
                        progressed = True
                        break

                await asyncio.sleep(_poll_interval(poll_started))

            if not progressed:
                logger.warning(
                    "CAPTCHA_TIMEOUT domain=%s code=%s",
                    domain, code,
                )
                _PENDING_VERIFY_RESULTS[result_key] = {
                    "status": "REVIEW_REQUIRED",
                    "success": False,
                    "message": "Captcha was not solved before timeout; tab was reset",
                }
                while len(_PENDING_VERIFY_RESULTS) > 256:
                    _PENDING_VERIFY_RESULTS.pop(next(iter(_PENDING_VERIFY_RESULTS)), None)
                try:
                    await _release_page_performance(page)
                except Exception as exc:
                    logger.warning("⚠️ %s release page resources failed: %s", tag, exc)
                try:
                    if not safe_is_closed(page):
                        await page.close()
                except Exception as exc:
                    logger.warning("⚠️ %s close timed-out page failed: %s", tag, exc)
                finally:
                    _invalidate_page_input_cache(page)
                    for account_page_key, cached_page in list(bot_state.account_pages.items()):
                        if cached_page is page:
                            bot_state.account_pages.pop(account_page_key, None)
                try:
                    if _tab_pool is not None:
                        await _tab_pool.replace_timed_out_page(page, domain, user)
                except Exception as exc:
                    logger.error(
                        "Captcha tab replacement failed domain=%s account=%s: %s",
                        domain, user, exc,
                    )
                return

            result_text = ""
            result_wait_s = max(0.5, float(getattr(Config, "VERIFICATION_RESULT_POPUP_WAIT_SECONDS", 3.0)))
            result_deadline = time.monotonic() + result_wait_s
            while not safe_is_closed(page) and time.monotonic() < result_deadline:
                fut = watch_api_timer.text_future
                if fut is not None and fut.done():
                    try:
                        candidate = str(fut.result() or "").strip()
                    except Exception:
                        candidate = ""
                    if _is_decisive_result(candidate):
                        result_text = candidate
                        break
                try:
                    candidate = await detect_result_text(page, domain=domain)
                except Exception:
                    candidate = ""
                if _is_decisive_result(candidate) and not _is_site_shell_result_candidate(candidate, domain):
                    result_text = candidate
                    break
                await asyncio.sleep(0.05)

            if not result_text:
                try:
                    body = await page.evaluate(
                        "() => ((document.querySelector('[role=dialog],.modal,main,#app,body')"
                        " || document.body).innerText || '').slice(0, 900)"
                    )
                except Exception:
                    body = ""

                if _is_decisive_result(body) and not _is_site_shell_result_candidate(body, domain):
                    result_text = str(body).strip()
                elif _is_site_shell_result_candidate(body, domain):
                    logger.warning(
                        "🧭 %s chỉ thấy nội dung trang tĩnh; KHÔNG đánh dấu FAILED khi chưa có API/popup kết quả",
                        tag,
                    )

            status = classify_result(result_text)
            elapsed = max(0.0, time.time() - watch_started)
            logger.info(
                f"📋 {tag} outcome sau xác minh: status={status.value} text={str(result_text).strip()[:200]!r}"
            )

            result_payload = {
                "status": status.value,
                "success": status in (ResultStatus.SUCCESS_POINTS, ResultStatus.SUCCESS_NO_POINTS),
                "has_points": status == ResultStatus.SUCCESS_POINTS,
                "message": str(result_text or "No popup")[:200],
            }
            if systems is not None:
                try:
                    outcome = await record_outcome(
                        status=status, page=page, user=user, code=code,
                        target_url=cleanup_url or target_url, domain=domain, key=cleanup_key,
                        elapsed=elapsed, result_text=result_text, systems=systems,
                        callbacks={"append_history": _append_code_history_safe},
                    )
                    result_payload.update(dict(outcome.result))
                    result_payload["status"] = status.value
                except Exception as outcome_exc:
                    logger.warning(f"⚠️ {tag} ghi outcome sau verify lỗi: {outcome_exc}")
            watch_timings = watch_api_timer.metrics()
            if watch_timings:
                result_payload["latency_ms"] = watch_timings
                update_latency(domain, user, code, watch_timings)
                logger.info("[METRIC] verification_latency domain=%s timings=%s", domain, watch_timings)
            _PENDING_VERIFY_RESULTS[result_key] = result_payload
            # Unconsumed results (worker timed out) must not accumulate forever.
            while len(_PENDING_VERIFY_RESULTS) > 256:
                _PENDING_VERIFY_RESULTS.pop(next(iter(_PENDING_VERIFY_RESULTS)), None)

            if domain in {"tangquaqq88.com", "hi88-freecode.pages.dev", "llwincode.com"} and cleanup_url:
                await _cleanup_after_verification_watcher(
                    page, domain, cleanup_url, cleanup_key, tag
                )
            else:
                await _close_unwanted_popups(page)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"⚠️ {tag} watcher lỗi: {exc}")
            try:
                await _force_reset("watcher exception")
            except Exception as reset_exc:
                logger.debug(f"⚠️ {tag} force-reset sau watcher lỗi cũng thất bại: {reset_exc}")
        finally:
            try:
                watch_api_timer.detach()
            except Exception:
                pass
            if _PENDING_VERIFY_TASKS.get(task_key) is asyncio.current_task():
                _PENDING_VERIFY_TASKS.pop(task_key, None)
                _PENDING_VERIFY_PAGES.pop(task_key, None)
            try:
                if _tab_pool is not None:
                    _tab_pool._notify_available(domain)
            except Exception:
                pass
            next_challenge = _pending_foreground_target()
            if (next_challenge is not None and bot_state.is_running
                    and not asyncio.current_task().cancelling()):
                await _show_manual_verification_page(*next_challenge, tag="[Verify-Next]")

    _PENDING_VERIFY_TASKS[task_key] = asyncio.create_task(_run())
    _PENDING_VERIFY_PAGES[task_key] = (page, domain)


async def click_verification_button_if_present(page, domain: str = "") -> bool:
    if domain not in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:
        logger.debug(f"ℹ️ [{domain or 'unknown'}] Bỏ qua nút Xác thực: domain không yêu cầu")
        return False
    try:
        wait_seconds = min(
            8.0,
            max(1.0, float(getattr(Config, "VERIFICATION_BUTTON_WAIT_SECONDS", 1.5))),
        )

        try:
            hint = await page.wait_for_function(
                """
                () => {
                    const challenge = document.querySelector(
                        '.cf-turnstile, [data-sitekey], iframe[src*="turnstile"], '
                        + 'iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"]'
                    );
                    if (challenge) return true;
                    return [...document.querySelectorAll('button,[role="button"]')].some((el) => {
                        const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                        return /^(xác thực|xac thuc|verify)$/.test(text);
                    });
                }
                """,
                timeout=wait_seconds * 1000.0,
                polling=100,
            )
            try:
                await hint.dispose()
            except Exception:
                pass
        except Exception:
            return False

        solve_wait = float(getattr(Config, "TURNSTILE_SOLVE_WAIT_SECONDS", 1.5))
        if not await _wait_turnstile_solved(page, solve_wait):
            logger.debug(f"ℹ️ [{domain}] Turnstile chưa xong sau {solve_wait:.1f}s — chưa bấm Xác thực")
            return False

        try:
            verify_button = page.locator("button").filter(has_text="Xác thực").last
            ready = await page.evaluate(
                """
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0
                            && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const scopes = [...document.querySelectorAll(
                        '[role="dialog"], [role="alertdialog"], .modal, '
                        '[class*="modal" i], [class*="dialog" i], '
                        '[class*="captcha" i], [class*="turnstile" i]'
                    )].filter(visible);
                    const challengeText = scopes
                        .map((el) => el.innerText || el.textContent || '').join(' ')
                        .toLowerCase();
                    const loading = /đang (xác minh|kiểm tra|tải)|dang (xac minh|kiem tra|tai)|verifying|checking|loading/.test(challengeText);
                    const button = [...document.querySelectorAll(
                        'button, [role="button"], input[type="button"], input[type="submit"], '
                        '[tabindex="0"], img[alt*="xác thực" i], img[alt*="xac thuc" i], '
                        'img[alt*="verify" i]'
                    )].find((el) => {
                        const label = (el.innerText || el.textContent || el.value || el.alt || '')
                            .trim().toLowerCase();
                        return /^(xác thực|xac thuc|verify)$/.test(label)
                            && visible(el) && !el.disabled;
                    });
                    return Boolean(button && !loading);
                }
                """
            )
            if ready and await verify_button.count() and await verify_button.is_visible() and await verify_button.is_enabled():
                await verify_button.click(timeout=1500, no_wait_after=True)
                passed = await _wait_for_cloudflare_passed_and_cleanup(page, domain=domain)
                logger.info(
                    f"✅ [Browser|{domain}] Playwright đã click nút Xác thực; "
                    f"Cloudflare={'passed' if passed else 'đã click/chờ kết quả'}"
                )

                return True
        except Exception as exc:
            logger.debug(f"⚠️ [{domain}] Playwright click Xác thực chưa thành công: {exc}")

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
                    var buttons = document.querySelectorAll(
                        'button, [role="button"], input[type="button"], input[type="submit"], '
                        + '[tabindex="0"], img[alt*="xác thực" i], img[alt*="xac thuc" i], '
                        + 'img[alt*="verify" i]'
                    );
                    for (var j = 0; j < buttons.length; j++) {
                        var el = buttons[j];
                        var label = (el.innerText || el.textContent || el.value || el.alt || '').trim().toLowerCase();
                        if (verifyTexts.indexOf(label) === -1) continue;
                        if (!isVisible(el) || el.disabled || cloudLoading()) continue;
                        var target = el.closest('button,[role="button"],a,[tabindex="0"]') || el;
                        target.click();
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
            return False
        passed = await _wait_for_cloudflare_passed_and_cleanup(page, domain=domain)
        logger.info(
            f"✅ [Browser|{domain}] JavaScript đã click nút Xác thực; "
            f"Cloudflare={'passed' if passed else 'đã click/chờ kết quả'}"
        )
        return True
    except Exception as exc:
        logger.warning(f"⚠️ [{domain}] verification button check lỗi: {exc}")
        return False


def _env_domain_set(name: str, default: str) -> frozenset:
    return frozenset(d.strip() for d in str(os.getenv(name, default)).split(",") if d.strip())


_REDUCE_MOTION_DOMAINS = _env_domain_set("REDUCE_MOTION_DOMAINS", "xx88code.com")

_SKIP_FRONT_DOMAINS = frozenset(
    getattr(Config, "SKIP_BRING_TO_FRONT_DOMAINS", ("tangquaqq88.com",))
)

_REDUCE_MOTION_JS = """
(() => {
    if (window.top !== window) return;
    const apply = () => {
        if (document.getElementById('__bot_reduce_motion')) return;
        const st = document.createElement('style');
        st.id = '__bot_reduce_motion';
        st.textContent = '*,*::before,*::after{animation-duration:1ms !important;'
            + 'animation-delay:0s !important;animation-iteration-count:1 !important;'
            + 'transition-duration:1ms !important;transition-delay:0s !important;'
            + 'scroll-behavior:auto !important;}';
        const parent = document.head || document.documentElement;
        if (!parent) return;
        parent.appendChild(st);
    };
    apply();
    document.addEventListener('DOMContentLoaded', apply);
})();
"""


async def _apply_reduce_motion(page, domain: str) -> None:
    if domain not in _REDUCE_MOTION_DOMAINS:
        return
    try:
        await page.add_init_script(_REDUCE_MOTION_JS)
        await page.evaluate(_REDUCE_MOTION_JS)
    except Exception as exc:
        logger.debug(f"⚠️ [{domain}] reduce-motion lỗi: {exc}")


_LLWIN_UI_TWEAKS_JS = r"""
(() => {
    try {
        if (window.top !== window || !['llwincode.com', 'www.llwincode.com'].includes(location.hostname)) return;
        if (window.__autobotLLWINUI) { window.__autobotLLWINUI.apply(); return; }

        // Edit these selectors if LLWIN changes its form or decorative assets.
        const formSelector = 'form:has(input[autocomplete="username"]):has(input[autocomplete="one-time-code"]), '
            + 'form:has(input[placeholder="Nhập tên tài khoản"]):has(input[placeholder="Nhập mã code"])';
        const heroArtSelector = 'img[src$="/images/imgcode/bgpc.webp"], img[src$="/images/imgcode/bgmb.webp"]';
        const cardArtSelector = 'img[src$="/images/imgcode/popuppc.png"], img[src$="/images/imgcode/popupmb.png"]';
        const decorativeCanvasSelector = 'canvas[aria-hidden="true"].fixed.inset-0.pointer-events-none.z-30.w-full.h-full.select-none';
        const backgroundSelector = decorativeCanvasSelector + ', video, .background-wrapper, #particles-js, #tsparticles';
        const protectedSelector = 'form, input, textarea, button, a, iframe, [role="button"], [role="dialog"], [role="alertdialog"], '
            + '[class*="captcha" i], [class*="turnstile" i]';
        const styleId = '__autobot_llwin_ui_tweaks';
        const css = `
            body, body > .min-h-screen { background: #0f172a !important; }
            [data-autobot-llwin-background] { display: none !important; }
            [data-autobot-llwin-background], [data-autobot-llwin-background] * {
                animation: none !important; transition: none !important;
            }
            [data-autobot-llwin-layout="page"] {
                width: 100% !important; min-height: 100dvh !important; transform: none !important;
            }
            [data-autobot-llwin-layout="hero"] {
                box-sizing: border-box !important; min-height: calc(100dvh - 88px) !important;
                display: flex !important; flex-direction: column !important;
                justify-content: center !important; padding: 24px 16px !important;
            }
            [data-autobot-llwin-layout="positioner"] {
                position: relative !important; inset: auto !important; transform: none !important;
                translate: none !important; width: 100% !important; max-width: 720px !important;
                margin: 0 auto !important;
            }
            [data-autobot-llwin-layout="scaler"] { transform: none !important; scale: none !important; }
            [data-autobot-llwin-layout="card"] {
                transform: none !important; translate: none !important;
                width: 100% !important; max-width: 680px !important; min-height: 0 !important;
                display: block !important; margin: 0 auto !important;
            }
            [data-autobot-llwin-layout="overlay"] {
                position: relative !important; inset: auto !important;
                width: 100% !important; padding: 0 !important; display: block !important;
            }
            [data-autobot-llwin-form] {
                box-sizing: border-box !important; position: relative !important;
                width: 100% !important; max-width: 680px !important; margin: 24px auto !important;
                padding: clamp(20px, 4vw, 36px) !important; background: #fff !important;
                color: #0f172a !important; border: 2px solid #ef4444 !important;
                border-radius: 16px !important; box-shadow: 0 0 24px rgba(0,0,0,.5) !important;
                font-size: 20px !important;
            }
            [data-autobot-llwin-form] input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]),
            [data-autobot-llwin-form] textarea {
                box-sizing: border-box !important; width: 100% !important;
                min-height: 56px !important; font-size: 22px !important; line-height: 1.4 !important;
                background: #fff !important; color: #0f172a !important;
            }
            [data-autobot-llwin-form] label { font-size: 18px !important; color: #0f172a !important; }
            [data-autobot-llwin-form] button[type="submit"],
            [data-autobot-llwin-form] button[aria-label="Nhận code ngay"] {
                min-height: 56px !important; font-size: 20px !important;
            }
        `;
        const mark = (el, role) => el.setAttribute('data-autobot-llwin-layout', role);
        const hide = el => {
            if (el.closest(protectedSelector) || el.querySelector(protectedSelector)) {
                el.removeAttribute('data-autobot-llwin-background'); return;
            }
            el.setAttribute('data-autobot-llwin-background', '');
            if (el.tagName === 'VIDEO') { el.autoplay = false; el.pause(); }
        };

        // The coin effect exits when getContext returns null. Install before
        // hydration so its RAF loop never starts; other canvases are untouched.
        const getContext = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function (...args) {
            if (args[0] === '2d' && this instanceof HTMLCanvasElement
                    && this.matches(decorativeCanvasSelector) && !this.closest(protectedSelector)) return null;
            return Reflect.apply(getContext, this, args);
        };

        const apply = () => {
            try {
                const parent = document.head || document.documentElement;
                if (!parent) return;
                if (!document.getElementById(styleId)) {
                    const style = document.createElement('style');
                    style.id = styleId; style.textContent = css; parent.appendChild(style);
                }
                document.querySelectorAll(formSelector).forEach(form => {
                    form.setAttribute('data-autobot-llwin-form', '');
                    const overlay = form.parentElement, card = overlay && overlay.parentElement;
                    if (card && [...card.children].some(el => el.matches(cardArtSelector))) {
                        mark(card, 'card'); mark(overlay, 'overlay');
                        [...card.children].filter(el => el.matches(cardArtSelector)).forEach(hide);
                    }
                    for (let el = form.parentElement; el && el !== document.body; el = el.parentElement) {
                        if (el.classList.contains('origin-top') && el.style.width === '1920px') mark(el, 'page');
                        if (el.classList.contains('transform-gpu')) mark(el, 'scaler');
                        if (el.classList.contains('max-w-[760px]')) mark(el, 'positioner');
                        const art = [...el.children].filter(child => child.matches(heroArtSelector));
                        if (art.length) { mark(el, 'hero'); art.forEach(hide); }
                    }
                });
                document.querySelectorAll(backgroundSelector).forEach(hide);
            } catch (_) { /* A changed DOM must not interrupt the bot. */ }
        };
        window.__autobotLLWINUI = {apply};
        const start = () => {
            apply();
            // Child-list changes cover React hydration/remounts; no polling or
            // attribute observation while inputs or the captcha are moving.
            new MutationObserver(apply).observe(document.documentElement, {childList: true, subtree: true});
            document.addEventListener('play', event => {
                if (event.target instanceof HTMLVideoElement) hide(event.target);
            }, true);
        };
        if (document.documentElement) start();
        else document.addEventListener('DOMContentLoaded', start, {once: true});
    } catch (_) { /* Cosmetic enhancements are best-effort. */ }
})();
"""


async def inject_llwin_ui_tweaks(page) -> None:
    """Enlarge LLWIN's manual UI without replacing controls or changing selectors."""
    try:
        await page.evaluate(_LLWIN_UI_TWEAKS_JS)
    except Exception:
        pass


_DEFER_CLEAN_DOMAINS = frozenset(
    d.strip() for d in str(os.getenv("DEFER_CLEAN_DOMAINS", "")).split(",") if d.strip()
)


_QQ88_SYNC_GROUPS: dict[tuple[str, str], dict] = {}
_QQ88_SYNC_REACHED: set = set()
_QQ88_SYNC_LOCK: asyncio.Lock | None = None


def _qq88_sync_lock() -> asyncio.Lock:
    global _QQ88_SYNC_LOCK
    if _QQ88_SYNC_LOCK is None:
        _QQ88_SYNC_LOCK = asyncio.Lock()
    return _QQ88_SYNC_LOCK


async def _submit_button_ready(page, domain: str) -> bool:
    profile = get_site_profile(domain)
    domain_sel = profile.submit_selector if profile else None
    try:
        return bool(await page.evaluate(
            """
            (sel) => {
                const visible = (el) => {
                    if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
                };
                if (sel) {
                    for (const el of document.querySelectorAll(sel)) if (visible(el)) return true;
                }
                const keys = ['kiểm tra ngay','kiem tra ngay','kiểm tra','kiem tra','nhận code','nhan code','nhận ngay','nhan ngay','áp dụng','ap dung','đổi code','doi code','nạp code','nap code','gửi','gui','submit','apply'];
                const exclude = /xác thực|xac thuc|verify|close|cancel|đóng|hủy|huỷ/i;
                for (const el of document.querySelectorAll('button,a[role=button],div[role=button],input[type=button],input[type=submit]')) {
                    if (!visible(el)) continue;
                    const txt = (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').trim().toLowerCase();
                    if (!exclude.test(txt) && keys.some(k => txt.includes(k))) return true;
                }
                return false;
            }
            """,
            domain_sel,
        ))
    except Exception:
        return False


async def _wait_qq88_sync_barrier(code: str, user: str, expected: int) -> tuple[bool, float]:
    expected = max(1, int(expected or 1))
    if expected <= 1 or not bool(getattr(Config, 'QQ88_SYNC_BARRIER', True)):
        return True, 0.0
    key = ('tangquaqq88.com', normalize_code(code, 'tangquaqq88.com'))
    now = time.monotonic()
    lock = _qq88_sync_lock()
    async with lock:

        for old_key, group in list(_QQ88_SYNC_GROUPS.items()):
            if group.get('done') or now - float(group.get('created', now)) > 30.0:
                _QQ88_SYNC_GROUPS.pop(old_key, None)
        group = _QQ88_SYNC_GROUPS.get(key)
        if group is not None and group.get('cancelled') and group.get('cancelled_by') == str(user):

            _QQ88_SYNC_GROUPS.pop(key, None)
            group = None
        _QQ88_SYNC_REACHED.add((key[1], str(user)))
        if group is None:
            group = {
                'expected': expected, 'ready': {}, 'event': asyncio.Event(),
                'created': now, 'cancelled': False, 'clicks': {}, 'done': False,
            }
            _QQ88_SYNC_GROUPS[key] = group
        else:
            group['expected'] = max(int(group.get('expected', 1)), expected)
        group['ready'][str(user)] = time.perf_counter()
        if len(group['ready']) >= int(group['expected']) and not group.get('cancelled'):
            logger.info(
                '🟢 [QQ88-SYNC] %s | %s tab READY → mở barrier submit',
                code, group['expected'],
            )
            group['release_perf'] = time.perf_counter()
            group['event'].set()
        event = group['event']
    started = time.perf_counter()
    try:
        await asyncio.wait_for(
            event.wait(),
            timeout=float(getattr(Config, 'QQ88_SYNC_BARRIER_TIMEOUT_SECONDS', 5.0)),
        )
    except asyncio.TimeoutError:
        async with lock:
            group['cancelled'] = True
            group['event'].set()
        logger.warning(
            '⚠️ [QQ88-SYNC] %s chưa đủ %s tab READY — hủy batch trước submit',
            code, expected,
        )

    async with lock:
        cancelled = bool(group.get('cancelled'))
        if cancelled and _QQ88_SYNC_GROUPS.get(key) is group:
            _QQ88_SYNC_GROUPS.pop(key, None)
    return (not cancelled), (time.perf_counter() - started) * 1000.0


def _qq88_sync_leave(code: str, user: str, expected: int) -> None:

    expected = int(expected or 1)
    if expected <= 1 or not bool(getattr(Config, 'QQ88_SYNC_BARRIER', True)):
        return
    key = ('tangquaqq88.com', normalize_code(code, 'tangquaqq88.com'))
    reached = (key[1], str(user))
    if reached in _QQ88_SYNC_REACHED:
        _QQ88_SYNC_REACHED.discard(reached)
        return
    group = _QQ88_SYNC_GROUPS.get(key)
    if group is None:
        group = {
            'expected': expected, 'ready': {}, 'event': asyncio.Event(),
            'created': time.monotonic(), 'cancelled': False, 'clicks': {}, 'done': False,
        }
        _QQ88_SYNC_GROUPS[key] = group
    if group.get('done') or group.get('cancelled'):
        return
    group['cancelled'] = True
    group['cancelled_by'] = str(user)
    group['event'].set()
    logger.warning(
        '⚠️ [QQ88-SYNC] %s | %s không tới được barrier — hủy cả cặp trước submit',
        code, user,
    )


async def _note_qq88_sync_click(code: str, user: str) -> float | None:
    key = ('tangquaqq88.com', normalize_code(code, 'tangquaqq88.com'))
    lock = _qq88_sync_lock()
    async with lock:
        group = _QQ88_SYNC_GROUPS.get(key)
        if group is None:
            return None
        group['clicks'][str(user)] = time.perf_counter()
        expected = int(group.get('expected', 1))
        if len(group['clicks']) >= expected:
            values = list(group['clicks'].values())
            skew_ms = (max(values) - min(values)) * 1000.0 if len(values) > 1 else 0.0
            group['done'] = True
            logger.info(
                '⚡ [QQ88-SYNC] %s submit phát song song | click_skew=%.2fms',
                code, skew_ms,
            )
            return skew_ms
    return None


_SUBMIT_CLICK_JS = """
({selector, codeSelectors}) => {
    const query = (root, sel) => {
        try { return [...root.querySelectorAll(sel)]; } catch (_) { return []; }
    };
    const ready = (el) => {
        if (!el || !el.isConnected || el.closest("[inert], [aria-disabled='true']")) return false;
        const control = el.closest('button, input, [role="button"]') || el;
        if (control.disabled || control.matches(':disabled') || control.type === 'reset') return false;
        const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== 'none'
            && style.visibility !== 'hidden' && style.opacity !== '0'
            && style.pointerEvents !== 'none';
    };
    const label = el => [el.innerText, el.textContent, el.value, el.alt,
        el.getAttribute('aria-label'), el.querySelector('img[alt]')?.alt]
        .filter(Boolean).join(' ').trim().toLowerCase();
    const excluded = /menu|nav|home|close|cancel|toggle|hamburger|back|trở về|huỷ|hủy|đóng|xác thực|xac thuc|verify/i;
    const allowed = el => ready(el) && !excluded.test(label(el));
    let form = null;
    for (const sel of codeSelectors || []) {
        const input = query(document, sel).find(ready);
        if (input) { form = input.closest('form'); break; }
    }
    if (selector) {
        const matches = query(document, selector).filter(el =>
            !form || form.contains(el) || el.form === form);
        const candidates = matches.filter(allowed);
        // Image controls can own the handler themselves; click that same node.
        const button = candidates.find(el => el.tagName === 'IMG') || candidates[0];
        if (button) { button.click(); return true; }
        // Do not redirect a known disabled/hidden control to another form.
        if (matches.length) return false;
    }
    const root = form || document;
    const controls = query(root, 'button, input[type="submit"], input[type="button"], '
        + 'a[role="button"], div[role="button"], span[role="button"]').filter(allowed);
    const keywords = ['kiểm tra ngay', 'kiem tra ngay', 'kiểm tra', 'kiem tra',
        'nhận code', 'nhan code', 'nhận ngay', 'nhan ngay', 'áp dụng', 'ap dung',
        'đổi code', 'doi code', 'nạp code', 'nap code', 'gửi', 'gui', 'submit', 'apply',
        'check code'];
    const button = controls.find(el => keywords.some(word => label(el).includes(word)))
        || controls.find(el => el.matches(
            'button[type="submit"], input[type="submit"], .btn-submit, .apply-btn, .submit-btn'));
    if (!button) return false;
    button.click();
    return true;
}
"""


async def click_submit_fast(page, domain: str = "") -> bool:
    """Find and click one ready control in one browser round trip."""
    profile = get_site_profile(domain)
    try:
        return bool(await page.evaluate(_SUBMIT_CLICK_JS, {
            "selector": profile.submit_selector if profile else None,
            "codeSelectors": list(getattr(profile, "code_selectors", ()) or ()),
        }))
    except Exception as exc:
        # A lost CDP acknowledgement may follow a dispatched click.
        # Never fall through to a second click or keyboard submission.
        raise RuntimeError("Submit click result unconfirmed") from exc


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

    "theo dõi nhận code", "theo doi nhan code",
]


def _is_transient_captcha_text(text: str) -> bool:
    if not text:
        return False
    low = text.strip().lower()
    if classify_result(text) == ResultStatus.CAPTCHA_EXPIRED:
        return False
    if any(p in low for p in _TRANSIENT_CF_PATTERNS):
        return True
    BUTTON_ONLY_WORDS = {"hủy", "huy", "xác thực", "xac thuc", "đóng", "dong", "cancel", "verify", "ok", "close"}
    tokens = [t.strip() for t in _re.split(r"[\n/|,]+", low) if t.strip()]
    if tokens and len(low) <= 40 and all(t in BUTTON_ONLY_WORDS for t in tokens):
        return True
    return False




_DETECT_RESULT_JS = r"""
(args) => {
    const { orderedSelectors, combinedSelectors, includeGlobal } = args;
    const readSelector = (sel) => {
        try {
            const els = document.querySelectorAll(sel);
            const texts = [];
            for (const el of els) {
                if (!el.getClientRects().length) continue;
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


_PRIORITY_RESULT_SELECTORS = [
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

_RESULT_WAIT_JS = r"""
([domSel, genSel, beforeRaw, transient, fastMs, startMs, keywords, abortToken]) => {
    if (abortToken && window.__botWaitAbort && window.__botWaitAbort[abortToken]) return '__ABORTED__';
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const before = norm(beforeRaw);
    const buttonOnly = new Set(['hủy', 'huy', 'xác thực', 'xac thuc', 'đóng', 'dong', 'cancel', 'verify', 'ok', 'close']);
    const okText = (raw) => {
        const n = norm(raw);
        if (n.length < 5) return false;
        if (before && before.includes(n)) return false;
        if (transient.some((p) => n.includes(p))) return false;
        if (n.length <= 40) {
            const toks = raw.toLowerCase().split(/[\n\/|,]+/).map((x) => x.trim()).filter(Boolean);
            if (toks.length && toks.every((x) => buttonOnly.has(x))) return false;
        }
        if (/__next_f|self\.__next|webpack|push\(\[/.test(raw)) return false;
        return true;
    };
    const scan = (sels) => {
        for (const sel of sels) {
            let els;
            try { els = document.querySelectorAll(sel); } catch (_) { continue; }
            for (const el of els) {
                if (!el.getClientRects().length) continue;
                let t = (el.innerText || el.textContent || '').trim();
                // XX88's aquarium popup exposes the result text in a sibling
                // container while the stable marker is the btn-accept image.
                if (!t && el.matches("img[src$='/images/ui-aquarium/btn-accept.png']")) {
                    let parent = el.parentElement;
                    for (let i = 0; parent && i < 5 && !t; i++, parent = parent.parentElement) {
                        t = (parent.innerText || parent.textContent || '').trim();
                    }
                }
                if (okText(t)) return t;
            }
        }
        return null;
    };
    let r = scan(domSel);
    if (r) return r;
    if (Date.now() - startMs >= fastMs) {
        // Quét rộng (innerText toàn trang) tốn layout: tối đa ~8 lần/giây.
        const nowMs = Date.now();
        if (nowMs - (window.__botLastBroad || 0) < 120) return null;
        window.__botLastBroad = nowMs;
        r = scan(genSel);
        if (r) return r;
        const beforeLines = new Set((beforeRaw || '').split('\n').map((l) => l.trim()).filter(Boolean));
        const body = (document.body && document.body.innerText) || '';
        for (const line of body.split('\n')) {
            const l = line.trim();
            if (l.length < 3 || beforeLines.has(l)) continue;
            const low = l.toLowerCase();
            if (keywords.some((k) => low.includes(k)) && okText(l)) return l;
        }
    }
    return null;
}
"""

_RESULT_KEYWORDS = (
    "thành công", "thanh cong", "thất bại", "that bai", "sai", "lỗi", "loi",
    "đã sử dụng", "da su dung", "success", "failed", "error", "invalid", "used",
    "không hợp lệ", "khong hop le", "hết hạn", "het han", "không đúng", "không tồn tại",
)


def _normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().lower()


def _is_stale_static_text(candidate: str, before_text: str) -> bool:

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

    PRIORITY_SELECTORS = _PRIORITY_RESULT_SELECTORS

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


_perf_ready_pages = weakref.WeakSet()
_perf_network_sessions = weakref.WeakKeyDictionary()

# Full host names avoid treating a code, query string or first-party asset path
# containing a vendor name as a tracking request.
_TRACKING_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "connect.facebook.net", "hotjar.com", "hotjar.io", "googlesyndication.com",
    "amazon-adsystem.com", "criteo.com", "criteo.net", "taboola.com",
    "outbrain.com", "clarity.ms", "sentry.io", "crisp.chat", "tawk.to",
)
_MEDIA_EXTENSIONS = ("mp4", "webm", "m4v", "mov", "mp3", "m4a", "wav", "ogg", "oga", "aac")


def _performance_block_rules(block_media: bool) -> dict:
    # URLPattern separates hostname/pathname from search parameters. Older CDP
    # engines may ignore urlPatterns; exact-host legacy rules remain safe there.
    patterns = [
        {"urlPattern": f"*://{prefix}{host}:*/*", "block": True}
        for host in _TRACKING_HOSTS for prefix in ("", "*.")
    ]
    if block_media:
        patterns.extend(
            {"urlPattern": f"*://*:*/*.{extension}", "block": True}
            for extension in _MEDIA_EXTENSIONS
        )
    legacy = [
        f"{scheme}://{prefix}{host}/*"
        for host in _TRACKING_HOSTS for scheme in ("http", "https")
        for prefix in ("", "www.")
    ]
    return {"urlPatterns": patterns, "urls": legacy}


async def _release_page_performance(page) -> None:
    session = _perf_network_sessions.pop(page, None)
    if session is None:
        return
    try:
        await session.send("Network.setBlockedURLs", {"urls": [], "urlPatterns": []})
    except Exception:
        pass
    try:
        await session.detach()
    except Exception:
        pass


def _log_page_error(page, error):
    domain = _normalize_domain(page.url or "")
    message = " ".join(str(error).split())[:400]  # PATCH_MARKER_2026-10-26-v10-log-chunk
    warning_once(("page-js", domain, message),
                 "[Browser-UI|%s] JavaScript error: %s", domain, message)


async def _setup_page_performance(page, label: str = "", *, domain: str = ""):
    try:
        if page in _perf_ready_pages:
            return
    except TypeError:

        pass

    page.on("pageerror", lambda error: _log_page_error(page, error))

    page_domain = _clean_domain(domain) or _normalize_domain(page.url or "")
    async def release_on_close(*_):
        logger.warning("[TabPool-PageClosed] registered_domain=%s last_domain=%s",
                       page_domain, _normalize_domain(page.url or ""))
        await _release_page_performance(page)

    async def note_domain_change(frame):
        if frame == page.main_frame and _normalize_domain(page.url or "") != page_domain:
            logger.info("[TabPool-PageMoved] registered_domain=%s current_domain=%s",
                        page_domain, _normalize_domain(page.url or ""))
            await _release_page_performance(page)

    page.once("close", release_on_close)
    page.on("framenavigated", note_domain_change)
    captcha_safe = (
        bool(getattr(Config, "BROWSER_CAPTCHA_SAFE_MODE", True))
        and page_domain in (set(getattr(Config, "CAPTCHA_SAFE_DOMAINS", ()))
                            | set(getattr(Config, "ADDITIONAL_CAPTCHA_SAFE_DOMAINS", ())))
    )
    if captcha_safe:

        logger.info("🛡️ [Captcha-Safe|%s] resource blocking disabled", page_domain)
    elif get_site_profile(page_domain) is not None:
        session = None
        try:
            # Playwright routes disable the HTTP cache for the whole page. CDP
            # blocking keeps reloads cacheable and needs no Python per-request work.
            session = await page.context.new_cdp_session(page)
            await session.send("Network.enable", {})
            await session.send("Network.setBlockedURLs", _performance_block_rules(
                bool(getattr(Config, "EDGE_BLOCK_MEDIA_ASSETS", True))
            ))
            _perf_network_sessions[page] = session

        except Exception as e:
            if session is not None:
                try:
                    await session.detach()
                except Exception:
                    pass
            logger.debug(f"⚠️ [{label}] Cannot setup resource blocking: {e}")

    try:
        await _apply_reduce_motion(page, page_domain)
    except Exception:
        pass

    if page_domain == "llwincode.com":
        try:
            await page.add_init_script(_LLWIN_UI_TWEAKS_JS)
        except Exception:
            pass
        await inject_llwin_ui_tweaks(page)

    try:
        _perf_ready_pages.add(page)
    except TypeError:
        pass


async def _close_unwanted_popups(page) -> dict:

    try:
        result = await page.evaluate(
            """
            () => {
                const OVERLAY_SEL = [
                    '.modal', '[class*="modal" i]', '[class*="popup" i]',
                    '[class*="overlay" i]', '[class*="dialog" i]',
                    '[role="dialog"]', '[role="alertdialog"]',
                    '[class*="notification" i]', '[class*="toast" i]',
                    '[class*="alert" i]:not(.alert-success):not(.alert-info)',
                    '[class*="banner" i]', '[class*="announcement" i]',
                ];
                const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                // Container có ô nhập là FORM của trang (.modal-code-anchor),
                // không phải popup thông báo -> không đóng và không tính là "kẹt".
                const hasInput = (el) => !!el.querySelector(
                    'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]), textarea'
                );
                const hasOverlay = [...document.querySelectorAll(
                    '.modal, [class*="modal" i], [class*="popup" i], [role="dialog"]'
                )].some((el) => isVisible(el) && !hasInput(el));
                if (!hasOverlay) return {closed: 0, stuck: false};
                const CLOSE_KEYWORDS = ['đóng', 'close', 'x', 'cancel', 'hủy', 'dismiss', 'got it', 'ok', 'thoát'];
                const SKIP_TEXT = ['xác thực', 'xac thuc', 'submit', 'kiểm tra', 'áp dụng', 'nhận'];
                const ICON_CLOSE_PATHS = ['M6 18L18 6M6 6l12 12'];
                let count = 0;
                for (const sel of OVERLAY_SEL) {
                    const els = [...document.querySelectorAll(sel)];
                    for (const el of els) {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        if (hasInput(el)) continue;
                        const btns = [...el.querySelectorAll('button, [role="button"], a, span')];
                        for (const btn of btns) {
                            const txt = (btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '').trim().toLowerCase();
                            if (SKIP_TEXT.some(s => txt.includes(s))) continue;
                            if (CLOSE_KEYWORDS.some(k => txt === k || txt.startsWith(k + ' '))) {
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
                // Sau khi click, kiểm tra lại: overlay có thực sự biến mất
                // chưa. Một click "thành công" (đúng selector, đúng từ khoá)
                // vẫn có thể không đóng được overlay nếu nó cần animation,
                // một sự kiện khác ngoài click, hoặc nút đóng thật nằm ngoài
                // CLOSE_KEYWORDS/ICON_CLOSE_PATHS đã biết.
                const stuck = OVERLAY_SEL.some(
                    (sel) => [...document.querySelectorAll(sel)].some((el) => isVisible(el) && !hasInput(el))
                );
                return {closed: count, stuck};
            }
            """
        )
        closed = int((result or {}).get("closed", 0)) if isinstance(result, dict) else 0
        stuck = bool((result or {}).get("stuck", False)) if isinstance(result, dict) else False
        if closed > 0:
            logger.debug(f"🧹 Đóng {closed} popup không mong muốn")
        if stuck:
            logger.debug("⚠️ Popup/overlay vẫn còn sau khi thử đóng")
        return {"closed": closed, "stuck": stuck}
    except Exception:
        return {"closed": 0, "stuck": False}


async def _dismiss_pre_submit_popups(page, domain: str) -> None:
    state = await _close_unwanted_popups(page)
    if state.get("stuck"):
        try:
            await page.keyboard.press("Escape")
        except Exception as exc:
            logger.debug("[%s] Escape popup cleanup failed: %s", domain, type(exc).__name__)
        state = await _close_unwanted_popups(page)
    if state.get("stuck"):
        logger.warning("[%s] previous result popup remains before new submit", domain)


_DISMISS_RESULT_POPUP_JS = r"""
async ([selectors, texts]) => {
    const isVisible = (el) => {
        if (!el || !el.isConnected) return false;
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    // Nút phải nằm trong overlay (fixed/dialog): tránh bấm nhầm nút cùng chữ
    // "Tiếp tục"/"Đồng ý" của chính trang.
    const OVERLAY_CLASS = /modal|popup|overlay|dialog|backdrop|alert/i;
    const inOverlay = (el) => {
        for (let n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
            const role = n.getAttribute && n.getAttribute('role');
            if (role === 'dialog' || role === 'alertdialog') return true;
            if (n.getAttribute && n.getAttribute('aria-modal') === 'true') return true;
            const st = getComputedStyle(n);
            if (st.position === 'fixed') return true;
            // absolute + z-index > 0 (kiểu popup căn giữa không dùng fixed)
            if (st.position === 'absolute' && parseInt(st.zIndex, 10) > 0) return true;
            if (typeof n.className === 'string' && OVERLAY_CLASS.test(n.className)) return true;
        }
        return false;
    };
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const groups = () => {
        const out = [];
        for (const sel of selectors) {
            const els = [];
            try { for (const el of document.querySelectorAll(sel)) if (isVisible(el)) els.push(el); } catch (e) {}
            if (els.length) out.push(els);
        }
        if (texts.length) {
            const els = [];
            for (const el of document.querySelectorAll('button, [role="button"]')) {
                if (!isVisible(el) || !texts.includes(norm(el.innerText || el.textContent))) continue;
                if (inOverlay(el)) els.push(el);
            }
            if (els.length) out.push(els);
        }
        return out;
    };
    // <img> nút (btn-retry.png...) -> bấm phần tử cha dạng button để chắc chắn
    // kích hoạt handler React; fallback bấm chính ảnh.
    const clickEl = (el) => {
        const t = (el.tagName === 'IMG' && el.closest('button, [role="button"], a')) || el;
        try { t.click(); } catch (e) { try { el.click(); } catch (e2) {} }
    };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    let clicked = 0;
    // Mỗi lượt bấm 1 nhóm (nút ưu tiên cao nhất đang hiện: Thử lại -> Xác nhận
    // -> X). Nếu popup chưa đóng thì sang nhóm kế (vd X) thay vì reload trang.
    for (let pass = 0; pass < 3; pass++) {
        const g = groups();
        if (!g.length) break;
        const target = g[Math.min(pass, g.length - 1)];
        for (const el of target) { clickEl(el); clicked++; }
        const t0 = Date.now();
        while (groups().length && Date.now() - t0 < 200) await sleep(25);
        if (!groups().length) break;
    }
    return {clicked, remaining: groups().length};
}
"""


async def _dismiss_result_popup(page, domain: str) -> dict:

    profile = get_site_profile(domain)
    selectors = list(getattr(profile, "popup_dismiss_selectors", ()) or ()) if profile else []
    texts = [t.strip().lower() for t in (getattr(profile, "popup_dismiss_texts", ()) or ())] if profile else []
    if not selectors and not texts:
        return {"clicked": 0, "remaining": 0}
    try:
        res = await page.evaluate(_DISMISS_RESULT_POPUP_JS, [selectors, texts])
        if domain == "getcodexx8bonus.pages.dev" and isinstance(res, dict) and res.get("clicked") and res.get("remaining"):

            try:
                await page.locator(".swal2-popup.swal2-show").wait_for(state="hidden", timeout=550)
                res["remaining"] = 0
            except Exception:
                pass
        if isinstance(res, dict):
            if res.get("clicked"):
                logger.debug(f"🧹 [{domain}] đóng popup kết quả: clicked={res.get('clicked')} còn={res.get('remaining')}")
            return {"clicked": int(res.get("clicked", 0)), "remaining": int(res.get("remaining", 0))}
    except Exception as e:
        logger.debug(f"⚠️ [{domain}] dismiss popup kết quả lỗi: {e}")
    return {"clicked": 0, "remaining": 0}


async def _wake_tab_for_submit(page, domain: str = "") -> bool:

    try:
        follow = bool(getattr(Config, "BROWSER_FOREGROUND_ON_SUBMIT", True))
        pending = _pending_foreground_target() if follow else None
        front_page, front_domain = pending if pending is not None else (page, domain)
        front = (asyncio.sleep(0) if not follow and domain in _SKIP_FRONT_DOMAINS
                 else front_page.bring_to_front())
        front_results = await asyncio.gather(
            front,
            _close_unwanted_popups(page),
            return_exceptions=True,
        )
        if follow:
            raised = await asyncio.to_thread(_force_chrome_window_foreground, front_domain)
            if not raised:
                warning_once(("submit-foreground", domain),
                             "[%s] Windows chưa cho đưa Edge lên trước; tab nhập mã đã được chọn", domain)
            return bool(raised and front_page is page
                        and not isinstance(front_results[0], BaseException))
    except Exception:
        pass
    return False


def mark_account_exhausted(domain: str, account: str) -> None:
    if _tab_pool is not None:
        _tab_pool.mark_account_exhausted(domain, account)


def reset_exhausted_accounts() -> None:
    if _tab_pool is not None:
        _tab_pool.reset_exhausted()


def get_prefilled_accounts(domain: str) -> list[str]:

    if _tab_pool is None:
        return []
    return _tab_pool.prefilled_accounts(domain)


class TabPool:
    def __init__(self, max_per_domain: int = 3, per_domain_overrides: dict | None = None):
        self.max_per_domain = max(1, int(max_per_domain))
        self._per_domain_max: dict = dict(per_domain_overrides or {})
        self._domains: dict = {}
        self._account_tabs: dict[str, dict[str, dict]] = {}
        self._setup_lock = asyncio.Lock()

        self._block_reason: str = ""
        self._availability: dict[str, asyncio.Event] = {}

        self._exhausted: dict[str, set[str]] = {}

        self._last_memory_reload_global: float = 0.0
        self._domain_urls: dict[str, str] = {}
        self._missing_warning_at: dict[str, float] = {}
        self._last_tab_counts: dict[str, int] = {}
        self._restore_after: dict[str, float] = {}

    def mark_account_exhausted(self, domain: str, account: str) -> None:
        # PATCH_MARKER_2026-10-27-stateless-entry: không còn khóa tài khoản theo ngày.
        if getattr(Config, "STATELESS_ACCOUNT_ENTRY", True):
            return
        domain = _clean_domain(str(domain or ""))
        account = str(account or "").strip().casefold()
        if domain and account:
            self._exhausted.setdefault(domain, set()).add(account)
            # Release affinity, not the page: another account may safely reuse
            # this tab once its current submission/verification has finished.
            mapped = self._account_tabs.get(domain, {})
            for bound in list(mapped):
                if str(bound).strip().casefold() == account:
                    mapped.pop(bound, None)
            self._notify_available(domain)

    def _account_is_exhausted(self, domain: str, account: str) -> bool:
        if getattr(Config, "STATELESS_ACCOUNT_ENTRY", True):
            return False
        return str(account or "").strip().casefold() in self._exhausted.get(
            _clean_domain(str(domain or "")), set())

    def _reject_exhausted_account(self, domain: str, account: str | None, entry=None) -> None:
        if account and self._account_is_exhausted(domain, account):
            if entry is not None:
                entry["reserved"] = False
                self._notify_available(domain)
            raise RuntimeError(f"Account exhausted for domain '{domain}'")

    def _available_configured_accounts(self, domain: str) -> list:
        return [account for account in get_effective_domain_accounts(domain)
                if not self._account_is_exhausted(
                    domain, account.get("username", "") if isinstance(account, dict) else account)]

    def reset_exhausted(self) -> None:
        self._exhausted.clear()

    def prefilled_accounts(self, domain: str) -> list[str]:
        domain = _clean_domain(str(domain or ""))
        seen: list[str] = []
        seen_keys: set[str] = set()
        for entry in self._domains.get(domain, []):
            name = str(entry.get("prefilled") or "").strip()
            identity = name.casefold()
            if (name and identity not in seen_keys and not self._account_is_exhausted(domain, name)
                    and not safe_is_closed(entry["page"])):
                seen.append(name)
                seen_keys.add(identity)
        return seen

    async def refresh_prefilled(self, domain: str) -> None:

        profile = get_site_profile(domain)
        sels = list(profile.username_selectors) if profile else []

        async def _read_one(entry) -> None:
            try:
                value = await entry["page"].evaluate(
                    """(sels) => {
                        for (const sel of sels) {
                            try {
                                const el = document.querySelector(sel);
                                if (el && el.offsetParent !== null) return (el.value || '').trim();
                            } catch (e) {}
                        }
                        return '';
                    }""",
                    sels,
                )
                entry["prefilled"] = str(value or "").strip().lower()
            except Exception:
                entry["prefilled"] = ""

        todo = [
            e for e in self._domains.get(domain, [])
            if not safe_is_closed(e["page"]) and not e["lock"].locked()
        ]
        if todo:
            await asyncio.gather(*(_read_one(e) for e in todo))

    def _event_for(self, domain: str) -> asyncio.Event:
        return self._availability.setdefault(domain, asyncio.Event())

    def _notify_available(self, domain: str) -> None:
        self._event_for(domain).set()

    @staticmethod
    def _new_entry(page):
        now = time.monotonic()
        return {
            "page": page,
            "lock": asyncio.Lock(),
            "created_at": now,
            "last_used": now,
            "last_memory_maintenance": now,
            "last_memory_reload": now,
            "reserved": False,
        }

    @staticmethod
    def _touch(entry):
        entry["last_used"] = time.monotonic()

    def _max_for(self, domain: str) -> int:
        hard_cap = 2 if domain in {"xx88code.com", "v79code.com"} else 1
        if getattr(Config, "STATELESS_ACCOUNT_ENTRY", True):
            return hard_cap
        limit = max(1, int(self._per_domain_max.get(domain, self.max_per_domain)))
        site_cap = max(1, int(getattr(Config, "DOMAIN_TAB_LIMITS", {
            "xx88code.com": 3, "v79code.com": 2,  # PATCH_MARKER_2026-10-25-v5-xx88-tabs-queue
        }).get(domain, 1)))
        site_cap = max(1, int(getattr(Config, "PARALLEL_DOMAIN_TAB_LIMITS", {}).get(domain, site_cap)))
        if domain == "tangquaqq88.com":
            limit = min(limit, 2, max(1, int(getattr(Config, "QQ88_PARALLEL_SLOTS", 2))))
        return min(limit, site_cap, hard_cap)

    @staticmethod
    def _entry_belongs_to_domain(domain: str, entry: dict) -> bool:
        page = entry["page"]
        if safe_is_closed(page):
            return False
        try:
            return (_normalize_domain(page.url or "") == domain
                    or entry["lock"].locked() or entry.get("reserved") or _verification_pending(page)
                    or (domain == "xx88code.com" and _xx88_readiness_pending(page)))
        except Exception:
            return bool(entry["lock"].locked() or entry.get("reserved"))

    def open_tab_count(self, domain: str) -> int:
        return sum(1 for entry in self._domains.get(domain, [])
                   if self._entry_belongs_to_domain(domain, entry))

    def available_tab_count(self, domain: str) -> int:
        count = 0
        for entry in self._domains.get(domain, []):
            page = entry["page"]
            if safe_is_closed(page) or entry["lock"].locked() or entry.get("reserved"):
                continue
            if _verification_pending(page):
                continue
            if domain == "xx88code.com" and _xx88_readiness_pending(page):
                continue
            try:
                if _normalize_domain(page.url or "") == domain:
                    count += 1
            except Exception:
                continue
        return count

    def verification_blocked(self, domain: str) -> bool:
        if domain != "xx88code.com":
            return False
        entries = [entry for entry in self._domains.get(domain, [])
                   if self._entry_belongs_to_domain(domain, entry)]
        return bool(entries) and all(_xx88_readiness_pending(entry["page"]) for entry in entries)

    async def discover_user_tabs(self) -> None:
        if not getattr(Config, "DYNAMIC_TAB_SLOTS_ENABLED", False):
            return
        async with self._setup_lock:
            for domain in tuple(self._domain_urls):
                try:
                    context = await get_or_launch_browser_context("shared", domain=domain)
                except Exception:
                    continue
                entries = self._domains.setdefault(domain, [])
                before = self._last_tab_counts.get(domain, self.open_tab_count(domain))
                removed_closed = removed_offsite = added = 0
                alive = []
                for entry in entries:
                    if not self._entry_belongs_to_domain(domain, entry) and not entry["lock"].locked() and not entry.get("reserved"):
                        if safe_is_closed(entry["page"]):
                            removed_closed += 1
                        else:
                            removed_offsite += 1
                        for key, val in list(self._account_tabs.get(domain, {}).items()):
                            if val is entry:
                                self._account_tabs[domain].pop(key, None)
                    else:
                        alive.append(entry)
                entries[:] = alive
                claimed = {id(e["page"]) for domain_entries in self._domains.values() for e in domain_entries}
                for page in context.pages:
                    if len(entries) >= self._max_for(domain):
                        break
                    if id(page) in claimed or safe_is_closed(page):
                        continue
                    try:
                        matches = _normalize_domain(page.url or "") == domain
                    except Exception:
                        continue
                    if not matches:
                        continue
                    try:
                        await _setup_page_performance(page, f"user-tab-{domain}", domain=domain)
                    except Exception as exc:
                        logger.debug("[Auto-Tabs] %s setup: %s", domain, exc)
                    entries.append(self._new_entry(page))
                    added += 1
                    claimed.add(id(page))
                # Restore the two-slot sites within account/capacity limits,
                # with a cooldown if a tab was closed or navigation failed.
                minimum = (max(0, int(getattr(Config, "XX88_MIN_TABS", 2)))
                           if domain == "xx88code.com" else
                           max(0, int(getattr(Config, "V79_MIN_TABS", 2)))
                           if domain == "v79code.com" else 0)
                desired = min(minimum, self._max_for(domain))
                if not getattr(Config, "STATELESS_ACCOUNT_ENTRY", True):
                    desired = min(desired, len(self._available_configured_accounts(domain)))
                if len(entries) < desired and time.monotonic() >= self._restore_after.get(domain, 0.):
                    self._restore_after[domain] = time.monotonic() + 60.0
                    try:
                        await self._create_bot_tab(domain)
                        added += 1
                        logger.info("[TabPool-Restore] %s restored one missing tab (desired=%s)", domain, desired)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("[TabPool-Restore] %s failed: %s; retry >=60s", domain, type(exc).__name__)
                after = self.open_tab_count(domain)
                self._last_tab_counts[domain] = after
                if before != after or removed_closed or removed_offsite or added:
                    logger.info("[TabPool-Capacity] %s previous=%s open=%s cap=%s closed=%s offsite=%s added=%s verification=%s",
                                domain, before, after, self._max_for(domain), removed_closed, removed_offsite, added,
                                sum(_verification_pending(e["page"]) or (domain == "xx88code.com" and _xx88_readiness_pending(e["page"])) for e in entries))
                if before != after:
                    usable_accounts = len(self._available_configured_accounts(domain))
                    concurrency = min(after, usable_accounts,
                                      max(1, int(getattr(Config, "QQ88_PARALLEL_SLOTS", 2)))
                                      if domain == "tangquaqq88.com" else self._max_for(domain))
                    logger.info("📑 [Auto-Tabs] %s: %s → %s tab mở | accounts=%s | song_song=%s (max=%s)",
                                domain, before, after, usable_accounts, concurrency, self._max_for(domain))
                    self._notify_available(domain)

    async def recover_endpoint(self, ep) -> None:
        """Replace detached page handles, restore configured tabs and account bindings."""
        async with self._setup_lock:
            for domain in tuple(self._domain_urls):
                if endpoint_for_domain(domain) is not ep:
                    continue
                self._domains[domain] = []
                self._account_tabs[domain] = {}
                for key in list(bot_state.account_pages):
                    if key.startswith(domain + "|"):
                        bot_state.account_pages.pop(key, None)
                        bot_state._input_cache.pop(key, None)
                accounts = list(dict.fromkeys(
                    str(account.get("username", "") if isinstance(account, dict) else account).strip()
                    for account in self._available_configured_accounts(domain)
                    if (account.get("username") if isinstance(account, dict) else account)
                ))
                slots = (self._max_for(domain) if getattr(Config, "STATELESS_ACCOUNT_ENTRY", True)
                         else min(self._max_for(domain), len(accounts)))
                try:
                    for _ in range(slots):
                        entry = await self._claim_existing_or_new(domain)
                        if entry is None:
                            # Explicit crash/startup recovery also restores tabs in
                            # dynamic mode; normal scans still never create tabs.
                            await self._create_bot_tab(domain)
                    await self.refresh_prefilled(domain)
                    if not getattr(Config, "STATELESS_ACCOUNT_ENTRY", True):
                        self._bind_accounts(domain, accounts)
                    for account, entry in self._account_tabs[domain].items():
                        bot_state.account_pages[f"{domain}|{account}"] = entry["page"]
                    logger.info("[TabPool-Recovery] %s restored %s tabs", domain, len(self._domains[domain]))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # An unavailable website must not trigger another Edge kill.
                    logger.warning("[TabPool-Recovery] %s tab restore incomplete: %s", domain, exc)
                finally:
                    self._notify_available(domain)

    def _pin_account(self, domain: str, account: str, entry: dict) -> None:
        if self._account_is_exhausted(domain, account):
            return
        mapping = self._account_tabs.setdefault(domain, {})
        if domain == "tangquaqq88.com":
            for old_account, old_entry in list(mapping.items()):
                if old_account != str(account) and old_entry is entry:
                    del mapping[old_account]
        mapping[str(account)] = entry

    def _bind_accounts(self, domain: str, accounts: list[str] | None = None):
        domain = _clean_domain(str(domain or ""))
        entries = self._domains.get(domain, [])
        mapping = self._account_tabs.setdefault(domain, {})
        for account in list(mapping):
            if self._account_is_exhausted(domain, account):
                mapping.pop(account, None)
        accounts = [str(account).strip() for account in accounts or ()
                    if str(account or "").strip() and not self._account_is_exhausted(domain, account)]
        if not accounts:
            return
        by_lower = {str(a).casefold(): str(a) for a in accounts}
        used = {id(e) for e in mapping.values()}
        for entry in entries:
            name = by_lower.get(str(entry.get("prefilled") or "").strip().casefold())
            if name and name not in mapping and id(entry) not in used:
                mapping[name] = entry
                used.add(id(entry))
                logger.info("📌 [TabPool] %s: tab đang điền sẵn '%s' → ưu tiên tài khoản này", domain, name)
        free = [e for e in entries if id(e) not in used]
        for account in accounts:
            if not account or str(account) in mapping:
                continue
            if not free:
                break
            mapping[str(account)] = free.pop(0)

    async def init(self, domain_url_map: dict | None = None, domain_accounts: dict | None = None):

        self._domain_urls.update({str(k): str(v) for k, v in (domain_url_map or {}).items()})
        claimed = {id(e["page"]) for entries in self._domains.values() for e in entries}
        for domain, target_url in (domain_url_map or {}).items():
            if not domain or domain in self._domains:
                continue

            try:
                context = await get_or_launch_browser_context("shared", domain=domain)
            except Exception as exc:
                logger.warning("⚠️ [TabPool] %s: chưa kết nối được %s (%s) — sẽ thử lại khi có mã",
                               domain, endpoint_for_domain(domain).label, exc)
                continue
            existing = list(context.pages)
            count = min(self._max_for(domain), self.max_per_domain)
            for page in existing:
                if id(page) in claimed or safe_is_closed(page):
                    continue
                try:
                    matches = _normalize_domain(page.url or "") == domain
                except Exception:
                    continue
                if not matches:
                    continue
                claimed.add(id(page))
                await _setup_page_performance(page, f"existing-{domain}", domain=domain)
                self._domains.setdefault(domain, []).append(self._new_entry(page))
                try:
                    if not _page_matches_target(page.url, target_url, domain):
                        await page.goto(target_url, wait_until="domcontentloaded",
                                        timeout=int(float(_site_profile_value(domain, "navigation_timeout_seconds", 12.0)) * 1000))
                        await scroll_to_input_fields(page)
                    await _close_unwanted_popups(page)
                except Exception as exc:
                    logger.warning("⚠️ [Existing-Tab] %s setup: %s", domain, exc)
                if len(self._domains[domain]) >= count:
                    break
            available = len(self._domains.get(domain, []))

            if available < count and not getattr(Config, "DYNAMIC_TAB_SLOTS_ENABLED", False) and self._auto_open_allowed(domain):
                async with self._setup_lock:
                    missing = count - len(self._domains.get(domain, []))
                    if missing > 0:

                        for _ in range(missing):
                            try:
                                await self._create_bot_tab(domain)
                                await asyncio.sleep(0.18)
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                logger.warning("⚠️ [TabPool] %s: không mở được tab trong %s: %s", domain, endpoint_for_domain(domain).label, exc)
                                break
                available = len(self._domains.get(domain, []))
            if getattr(Config, "DYNAMIC_TAB_SLOTS_ENABLED", False):
                logger.info("📑 [Auto-Tabs] %s: nhận %s tab đang mở (tối đa %s), không tự mở tab", domain, available, count)
            elif available < count:
                logger.warning("⚠️ [Existing-Tab] %s: có %s/%s tab; %s", domain, available, count, "sẽ mở tab thiếu khi có mã" if self._auto_open_allowed(domain) else "hãy mở tab thủ công")
        async def _refresh_safe(dom: str) -> None:
            try:
                await self.refresh_prefilled(dom)
            except Exception as exc:
                logger.debug("⚠️ [TabPool] refresh_prefilled %s: %s", dom, exc)

        if domain_accounts:
            await asyncio.gather(*(_refresh_safe(d) for d in domain_accounts))
        if not getattr(Config, "STATELESS_ACCOUNT_ENTRY", True):
            for domain, accounts in (domain_accounts or {}).items():
                self._bind_accounts(domain, [str(a) for a in accounts if a])
        self._last_tab_counts.update({domain: self.open_tab_count(domain) for domain in self._domains})
        logger.info("✅ TabPool: đăng ký %s domain từ tab đang mở", len(self._domains))

    async def _find_existing_page(self, domain: str):
        context = await get_or_launch_browser_context("shared", domain=domain)
        claimed = {id(e["page"]) for entries in self._domains.values() for e in entries}
        for page in context.pages:
            if id(page) in claimed or safe_is_closed(page):
                continue
            try:
                if _normalize_domain(page.url or "") == domain:
                    return page
            except Exception:
                continue
        return None

    async def _claim_existing_or_new(self, domain: str) -> dict | None:

        page = await self._find_existing_page(domain)
        if page is None:
            return None
        await _setup_page_performance(page, f"existing-{domain}", domain=domain)
        entry = self._new_entry(page)
        self._domains.setdefault(domain, []).append(entry)
        return entry

    def _auto_open_allowed(self, domain: str) -> bool:
        if getattr(Config, "DYNAMIC_TAB_SLOTS_ENABLED", False):
            return False
        if bool(getattr(Config, "AUTO_OPEN_MISSING_TABS", False)):
            return True
        ep = endpoint_for_domain(domain)
        if ep is _CHROME2:
            return bool(getattr(Config, "CHROME2_AUTO_OPEN_TABS", True))
        return False

    async def _new_tab_for_domain(self, domain: str) -> dict:

        entry = await self._claim_existing_or_new(domain)
        if entry is not None:
            return entry
        if not self._auto_open_allowed(domain):
            raise RuntimeError(f"No existing spare tab for {domain}; open the tab manually")
        return await self._create_bot_tab(domain)

    async def _create_bot_tab(self, domain: str) -> dict:
        if len(self._domains.get(domain, [])) >= self._max_for(domain):
            raise RuntimeError(f"Tab limit reached for {domain}; reuse existing tabs")
        url = self._domain_urls.get(domain)
        if not url:
            raise RuntimeError(f"Missing configured target URL for {domain}")
        ep = endpoint_for_domain(domain)
        context = await get_or_launch_browser_context("shared", domain=domain)
        page = await context.new_page()
        try:
            await _setup_page_performance(page, f"bot-created-{domain}", domain=domain)
            await page.goto(
                url, wait_until="domcontentloaded",
                timeout=int(float(_site_profile_value(domain, "navigation_timeout_seconds", 12.0)) * 1000),
            )
            if domain in ("livemm88.net", "liverr88.net"):
                await open_mm88_code_form(page)
            await scroll_to_input_fields(page)
        except Exception:

            try:
                await page.close()
            except Exception:
                pass
            raise
        entry = self._new_entry(page)
        self._domains.setdefault(domain, []).append(entry)
        self._notify_available(domain)
        logger.info("🆕 [TabPool] %s: đã mở 1 tab mới trong %s", domain, ep.label)
        return entry

    async def replace_timed_out_page(self, page, domain: str, account: str) -> None:
        domain = _clean_domain(domain)
        async with self._setup_lock:
            entries = self._domains.get(domain, [])
            removed_entries = [entry for entry in entries if entry.get("page") is page]
            self._domains[domain] = [
                entry for entry in entries if entry.get("page") is not page
            ]
            if not self._domains[domain]:
                self._domains.pop(domain, None)
            for entry in removed_entries:
                for bound_account, bound_entry in list(
                    self._account_tabs.get(domain, {}).items()
                ):
                    if bound_entry is entry:
                        self._account_tabs[domain].pop(bound_account, None)
            for cache_key, cached_page in list(bot_state.account_pages.items()):
                if cached_page is page:
                    bot_state.account_pages.pop(cache_key, None)
            _invalidate_page_input_cache(page)

            replacement = await self._create_bot_tab(domain)
            if account:
                self._pin_account(domain, account, replacement)
                bot_state.account_pages[f"{domain}|{account}"] = replacement["page"]
            self._notify_available(domain)

    def _warn_missing(self, domain: str, message: str) -> None:
        now = time.monotonic()
        interval = max(5.0, float(getattr(Config, "TAB_MISSING_WARNING_INTERVAL_SECONDS", 30.0)))
        if now - self._missing_warning_at.get(domain, -interval) >= interval:
            self._missing_warning_at[domain] = now
            logger.warning("⚠️ [TabPool] %s", message)

    async def acquire(self, domain: str = "", account: str | None = None):
        domain = _clean_domain(str(domain or ""))
        self._reject_exhausted_account(domain, account)
        if self.verification_blocked(domain):
            raise BrowserVerificationPending("All XX88 tabs are waiting for pre-submit verification")
        wait_limit = max(0.0, float(getattr(Config, "TAB_ACQUIRE_WAIT_SECONDS", 2.0)))
        deadline = time.monotonic() + wait_limit

        _t_start = time.monotonic()
        _busy_retries = 0
        self._block_reason = ""
        while True:
            if self.verification_blocked(domain):
                raise BrowserVerificationPending("All XX88 tabs are waiting for pre-submit verification")
            wake = self._event_for(domain)
            wake.clear()
            try:
                _t_try = time.monotonic()
                _res = await self._try_acquire(domain, account=account)
                _total = time.monotonic() - _t_start
                if _total > 0.3:
                    logger.info(
                        "🔬 [TabDiag] %s|%s acquire=%.0fms | tab bận (retry)=%s | lần thử cuối=%.0fms "
                        "| lý do chờ=%s",
                        domain, account, _total * 1000.0, _busy_retries,
                        (time.monotonic() - _t_try) * 1000.0,
                        self._block_reason or "-",
                    )
                return _res
            except RuntimeError as exc:
                _busy_retries += 1

                permanent_missing = (
                    "No existing tab" in str(exc)
                    or "No existing spare tab" in str(exc)
                    or "Account tab closed" in str(exc)
                    or "Account exhausted" in str(exc)
                )
                if permanent_missing:
                    self._warn_missing(domain, str(exc))
                    raise
                if time.monotonic() >= deadline:
                    logger.debug("[TabPool] %s account=%s timed out waiting for reusable page: %s", domain, account, self._block_reason or str(exc))
                    raise
                try:
                    await asyncio.wait_for(wake.wait(), timeout=min(0.25, max(0.001, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    pass

    async def _try_acquire(self, domain: str = "", account: str | None = None):
        domain = _clean_domain(str(domain or "")) or "unknown"
        self._reject_exhausted_account(domain, account)

        entries = self._domains.get(domain)
        if entries and any(safe_is_closed(e["page"]) for e in entries):
            await self.collect_garbage()
            entries = self._domains.get(domain)
        if not entries:
            async with self._setup_lock:
                entries = self._domains.get(domain)
                if not entries:
                    try:
                        entry = await self._new_tab_for_domain(domain)
                    except RuntimeError as exc:
                        raise RuntimeError(f"No existing tab for '{domain}'; open the site manually") from exc
                    entries = self._domains.get(domain, [])

        if account:
            mapped = self._account_tabs.setdefault(domain, {})
            preferred = mapped.get(str(account))
            if preferred is not None and preferred not in entries:
                mapped.pop(str(account), None)
                preferred = None
            account_lc = str(account).strip().lower()
            pinned_other_ids = {
                id(other_entry) for other_account, other_entry in mapped.items()
                if other_account != str(account)
            }
            entries = sorted(entries, key=lambda e: (
                0 if e is preferred else
                1 if e.get("prefilled") == account_lc and id(e) not in pinned_other_ids else
                2 if id(e) not in pinned_other_ids else 3,
                e.get("last_used", 0.0),
            ))

        for entry in entries:
            if not account and not self._entry_belongs_to_domain(domain, entry):
                continue
            if domain == "tangquaqq88.com" and account and preferred is not None and entry is not preferred:
                continue
            page = entry["page"]
            if (entry["lock"].locked() or entry.get("reserved") or _verification_pending(page)
                    or safe_is_closed(page) or (domain == "xx88code.com" and _xx88_readiness_pending(page))):
                self._block_reason = "tab_lock"
                continue

            entry["reserved"] = True
            try:

                probe_cf = bool(getattr(Config, "TAB_ACQUIRE_PROBE_CLOUDFLARE", False))
                cf_blocked = (
                    probe_cf
                    and domain != "liverr88.net"
                    and await is_cloudflare_present(page, domain=domain)
                )
            except BaseException:
                entry["reserved"] = False
                self._notify_available(domain)
                raise
            if cf_blocked:
                entry["reserved"] = False
                self._block_reason = "cloudflare"
                continue
            self._reject_exhausted_account(domain, account, entry)
            self._touch(entry)
            if account:
                self._pin_account(domain, str(account), entry)
            return entry, page, entry["lock"]

        domain_cap = self._max_for(domain)
        entries = self._domains.get(domain, [])
        if len(entries) < domain_cap and (not entries or self._auto_open_allowed(domain)):

            async with self._setup_lock:
                entries = self._domains.get(domain, [])

                if len(entries) >= domain_cap:
                    raise RuntimeError(f"Tab setup changed for {domain}; retry acquisition")
                entry = await self._new_tab_for_domain(domain)
                self._reject_exhausted_account(domain, account, entry)
                if account:
                    self._pin_account(domain, str(account), entry)
                entry["reserved"] = True
                self._touch(entry)
                return entry, entry["page"], entry["lock"]

        fallback_entries = entries
        for candidate in fallback_entries:
            if candidate is None:
                continue
            if not account and not self._entry_belongs_to_domain(domain, candidate):
                continue
            if domain == "tangquaqq88.com" and account and preferred is not None and candidate is not preferred:
                continue
            if (candidate["lock"].locked() or candidate.get("reserved") or _verification_pending(candidate["page"])
                    or safe_is_closed(candidate["page"]) or (domain == "xx88code.com" and _xx88_readiness_pending(candidate["page"]))):
                continue
            candidate["reserved"] = True
            try:
                if (
                    bool(getattr(Config, "TAB_ACQUIRE_PROBE_CLOUDFLARE", False))
                    and domain != "liverr88.net"
                    and await is_cloudflare_present(candidate["page"], domain=domain)
                ):
                    candidate["reserved"] = False
                    continue
            except BaseException:
                candidate["reserved"] = False
                self._notify_available(domain)
                raise
            self._reject_exhausted_account(domain, account, candidate)
            self._touch(candidate)
            if account:
                self._pin_account(domain, str(account), candidate)
            return candidate, candidate["page"], candidate["lock"]

        def _tab_state(e):  # PATCH_MARKER_2026-10-26-v10-tab-states
            p = e["page"]
            if safe_is_closed(p):
                return "closed"
            if e["lock"].locked():
                return "locked"
            if e.get("reserved"):
                return "reserved"
            if _verification_pending(p):
                return "verify_watch"
            if domain == "xx88code.com" and _xx88_readiness_pending(p):
                return "xx88_ready"
            return "other"

        raise RuntimeError(
            f"No available non-blocked tab for domain '{domain}' "
            f"states={[_tab_state(e) for e in entries]}"
        )

    async def collect_garbage(self, *, idle_ttl: float = 900.0, min_tabs_per_domain: int = 1) -> dict:

        removed = 0
        async with self._setup_lock:
            for domain, entries in list(self._domains.items()):
                survivors = []
                for entry in entries:
                    page = entry["page"]
                    if not safe_is_closed(page) or entry["lock"].locked() or entry.get("reserved") or _verification_pending(page):
                        survivors.append(entry)
                        continue
                    removed += 1
                    _invalidate_page_input_cache(page)
                    for key, cached in list(bot_state.account_pages.items()):
                        if cached is page:
                            bot_state.account_pages.pop(key, None)
                    for account, bound in list(self._account_tabs.get(domain, {}).items()):
                        if bound is entry:
                            self._account_tabs[domain].pop(account, None)
                if survivors:
                    self._domains[domain] = survivors
                else:
                    self._domains.pop(domain, None)
                if len(survivors) != len(entries):
                    self._notify_available(domain)
        return {"removed": removed, "closed": 0, "remaining": sum(len(v) for v in self._domains.values())}

    async def cleanup_idle_memory(
        self,
        *,
        compact_idle_seconds: float = 120.0,
        reload_idle_seconds: float = 900.0,
        reload_min_gap_seconds: float = 45.0,
        max_reloads_per_pass: int = 1,
        max_compacts_per_pass: int = 2,
        force: bool = False,
    ) -> dict:

        now = time.monotonic()
        compacted = 0
        gc_runs = 0
        media_unloaded = 0
        reloaded = 0
        skipped = 0
        max_reloads = max(0, int(max_reloads_per_pass))
        if not force and not bool(getattr(Config, "CHROME_IDLE_RELOAD_ENABLED", False)):
            max_reloads = 0
        max_compacts = max(1, int(max_compacts_per_pass))
        if force:
            max_compacts = max(max_compacts, 4)
        global_gap = max(5.0, float(reload_min_gap_seconds))
        compact_interval = max(30.0, float(compact_idle_seconds))
        reload_threshold_normal = max(compact_interval, float(reload_idle_seconds))
        pressure_reload_idle = max(
            60.0,
            float(getattr(Config, "TAB_POOL_MEMORY_PRESSURE_RELOAD_IDLE_SECONDS", 300.0)),
        )
        unload_media = bool(getattr(Config, "EDGE_IDLE_UNLOAD_MEDIA", True))

        async def _page_gc(target_page) -> bool:
            session = None
            try:
                session = await target_page.context.new_cdp_session(target_page)
                await session.send("HeapProfiler.collectGarbage")
                return True
            except Exception:
                return False
            finally:
                if session is not None:
                    try:
                        await session.detach()
                    except Exception:
                        pass

        for domain, entries in list(self._domains.items()):
            for entry in entries:
                page = entry.get("page")
                lock = entry.get("lock")
                if (
                    lock is None
                    or lock.locked()
                    or entry.get("reserved")
                    or _verification_pending(page)
                    or safe_is_closed(page)
                ):
                    skipped += 1
                    continue

                idle = now - float(entry.get("last_used", now))
                last_maintenance = float(
                    entry.get("last_memory_maintenance", entry.get("last_used", now))
                )
                due_compact = force or (
                    idle >= compact_interval and now - last_maintenance >= compact_interval
                )
                if not due_compact:
                    continue
                if compacted >= max_compacts and reloaded >= max_reloads:

                    continue

                entry["reserved"] = True
                try:

                    try:
                        if await is_cloudflare_present(page, domain=domain):
                            skipped += 1
                            continue
                    except Exception:
                        skipped += 1
                        continue

                    last_reload = float(
                        entry.get("last_memory_reload", entry.get("last_used", now))
                    )
                    reload_threshold = (
                        min(reload_threshold_normal, pressure_reload_idle)
                        if force
                        else reload_threshold_normal
                    )
                    can_reload = (
                        reloaded < max_reloads
                        and idle >= reload_threshold
                        and now - last_reload >= reload_threshold
                        and now - self._last_memory_reload_global >= global_gap
                    )
                    if can_reload:
                        try:
                            await page.reload(
                                wait_until="domcontentloaded",
                                timeout=int(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)),
                            )
                            await _close_unwanted_popups(page)
                            maintenance_at = time.monotonic()
                            entry["last_memory_maintenance"] = maintenance_at
                            entry["last_memory_reload"] = maintenance_at
                            self._last_memory_reload_global = maintenance_at
                            reloaded += 1
                            continue
                        except Exception as exc:
                            logger.debug("⚠️ [TabPool-Memory] reload %s lỗi: %s", domain, exc)

                    if compacted >= max_compacts:
                        continue

                    try:
                        stats = await page.evaluate(
                            """
                            (unloadMedia) => {
                                let removed = 0;
                                let media = 0;
                                // React owns these nodes. Removing toast/status/alert
                                // containers externally can unmount the whole app on
                                // its next render. Only the site's close actions may
                                // remove UI; idle maintenance must preserve the tree.
                                if (unloadMedia) {
                                    for (const el of document.querySelectorAll('video,audio')) {
                                        if (el.closest('form,[role="dialog"],[class*="captcha" i],[class*="turnstile" i]')) continue;
                                        try { el.pause(); } catch (_) {}
                                        try { el.autoplay = false; el.preload = 'none'; } catch (_) {}
                                        try {
                                            // Do not remove React-owned source attributes or call load():
                                            // media error callbacks can put the app into its error boundary.
                                            media++;
                                        } catch (_) {}
                                    }
                                }
                                try { performance.clearResourceTimings(); } catch (_) {}
                                try { performance.clearMarks(); performance.clearMeasures(); } catch (_) {}
                                return {removed, media};
                            }
                            """,
                            unload_media,
                        )
                        media_unloaded += int((stats or {}).get("media", 0) or 0)
                        if await _page_gc(page):
                            gc_runs += 1
                        entry["last_memory_maintenance"] = time.monotonic()
                        compacted += 1
                    except Exception as exc:
                        entry["last_memory_maintenance"] = time.monotonic()
                        logger.debug("⚠️ [TabPool-Memory] compact %s lỗi: %s", domain, exc)
                finally:
                    entry["reserved"] = False
                    self._notify_available(domain)
        return {
            "compacted": compacted,
            "gc": gc_runs,
            "media_unloaded": media_unloaded,
            "reloaded": reloaded,
            "skipped": skipped,
            "pressure": bool(force),
        }


_tab_pool: TabPool | None = None


def live_user_tab_count(domain: str) -> int:
    if _tab_pool is None:
        return 0
    return _tab_pool.open_tab_count(domain)


def available_user_tab_count(domain: str) -> int:
    if _tab_pool is None:
        return 0
    return _tab_pool.available_tab_count(domain)


def verification_blocked(domain: str) -> bool:
    """True khi mọi tab của domain đang chờ xác minh trước khi gửi (dùng cho pull-when-ready)."""
    pool = _tab_pool
    return bool(pool is not None and pool.verification_blocked(domain))


async def wait_for_verification_ready(domain: str, timeout: float = 5.0) -> bool:
    """Wait outside submission semaphores; a shared probe wakes all waiters."""
    pool = _tab_pool
    if pool is None or domain != "xx88code.com":
        return False
    deadline = time.monotonic() + max(0., timeout)
    while pool.verification_blocked(domain):
        wake = pool._event_for(domain)
        wake.clear()
        if not pool.verification_blocked(domain):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(wake.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            return False
    return pool.open_tab_count(domain) > 0


async def dynamic_tab_watcher():
    if not getattr(Config, "DYNAMIC_TAB_SLOTS_ENABLED", False):
        return
    while bot_state.is_running:
        try:
            if _tab_pool is not None:
                await _tab_pool.discover_user_tabs()
            await asyncio.sleep(float(getattr(Config, "DYNAMIC_TAB_SCAN_INTERVAL_SECONDS", 2.0)))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("[Auto-Tabs] scan error: %s", exc)
            await asyncio.sleep(3)


def _check_chrome_cdp_port_reachable(port: int, timeout: float = 1.5, host: str | None = None) -> bool:
    import socket
    try:
        host = host or getattr(Config, "CHROME_CDP_HOST", "127.0.0.1")
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _check_chrome_cdp_port_reachable_async(port: int, timeout: float = 1.5, host: str | None = None) -> bool:

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _check_chrome_cdp_port_reachable, port, timeout, host
    )


async def preload_browsers_and_accounts(account_targets: list):

    global _tab_pool

    if not account_targets:
        logger.info("ℹ️ [Browser] Không có kênh nào thuộc domain trình duyệt đang bật — bỏ qua preload")
        return

    dynamic_mode = bool(getattr(Config, "DYNAMIC_TAB_SLOTS_ENABLED", False))
    pool_size = (int(getattr(Config, "DYNAMIC_TAB_MAX_PER_DOMAIN", 6)) if dynamic_mode
                 else max(1, min(10, int(getattr(Config, "TAB_POOL_SIZE", 3) or 3))))

    domain_channel_count: dict = {}
    for item in account_targets:
        d = item["domain"]
        domain_channel_count[d] = domain_channel_count.get(d, 0) + 1

    domain_tab_cap = (pool_size if dynamic_mode
                      else max(1, int(getattr(Config, "MAX_TAB_PER_DOMAIN_CAP", 5))))
    per_domain_overrides = {}
    for d, count in domain_channel_count.items():

        profile_slots = int(_site_profile_value(d, "tab_slots", domain_tab_cap))
        if d == "tangquaqq88.com":
            profile_slots = min(
                profile_slots,
                max(1, int(getattr(Config, "QQ88_PARALLEL_SLOTS", 2))),
            )
        per_domain_overrides[d] = (domain_tab_cap if dynamic_mode else min(
            domain_tab_cap,
            max(1, profile_slots),
            max(1, count),
        ))
        if d == "tangquaqq88.com":
            # Dynamic scans and startup recovery must obey submission slot limits.
            per_domain_overrides[d] = min(per_domain_overrides[d], max(1, profile_slots), 2)

    _tab_pool = TabPool(max_per_domain=pool_size, per_domain_overrides=per_domain_overrides)

    domain_url_map: dict = {}
    domain_accounts: dict[str, list[str]] = {}
    for item in account_targets:
        d = item["domain"]
        if d not in domain_url_map:
            domain_url_map[d] = item["target_url"]
        for account in item.get("accounts", []) or []:
            username = str(account.get("username", "")).strip() if isinstance(account, dict) else str(account).strip()
            if username and username not in domain_accounts.setdefault(d, []):
                domain_accounts[d].append(username)

    for item in account_targets:
        key = item.get("key", item["domain"])
        bot_state.context_locks[key] = asyncio.Lock()

    site_count = len({item.get("domain") for item in account_targets if item.get("domain")})
    logger.info(
        "✅ [Browser] %s target domain+tài khoản (%s site trình duyệt) đăng ký xong",
        len(account_targets),
        site_count,
    )

    _tab_pool._domain_urls.update({str(k): str(v) for k, v in domain_url_map.items()})
    needed = {}
    for d in domain_url_map:
        needed.setdefault(endpoint_for_domain(d).name, endpoint_for_domain(d))
    _needed_endpoint_names.clear()
    _needed_endpoint_names.update(needed.keys())

    ready_names = set()
    for ep in needed.values():
        ready = await _check_chrome_cdp_port_reachable_async(ep.port, host=ep.host)
        if not ready:
            max_retries = 7
            for attempt in range(1, max_retries + 1):
                logger.info(f"⏳ [{ep.label}-CDP] Cổng {ep.port} chưa phản hồi — thử lại ({attempt}/{max_retries}, mỗi 2s)...")
                await asyncio.sleep(2.0)
                if await _check_chrome_cdp_port_reachable_async(ep.port, host=ep.host):
                    ready = True
                    break
        if ready:
            ready_names.add(ep.name)
            doms = sorted(d for d in domain_url_map if endpoint_for_domain(d) is ep)
            logger.info(f"✅ [{ep.label}-CDP] Cổng {ep.port} đang mở — {ep.label} sẵn sàng cho: {', '.join(doms)}")
        else:

            logger.error(
                f"❌ [{ep.label}-CDP] Cổng {ep.port} KHÔNG phản hồi — {ep.label} CHƯA chạy ở chế độ debug! "
                f"Các submit của domain thuộc {ep.label} sẽ fail-fast cho tới khi trình duyệt được khởi động đúng (run.bat)."
            )

    ready_map = {d: u for d, u in domain_url_map.items() if endpoint_for_domain(d).name in ready_names}
    if ready_map:
        try:
            await _tab_pool.init(domain_url_map=ready_map, domain_accounts=domain_accounts)
            for ep in needed.values():
                if ep.name in ready_names and ep.port in chrome_launcher.CHROME_PROCESSES:
                    await _tab_pool.recover_endpoint(ep)
            logger.info(f"✅ [TabPool] Đã gán tab riêng cho {len(ready_map)} domain")
        except Exception as e:
            logger.warning(f"⚠️ [TabPool] Không init được ngay lúc preload ({e}) — sẽ tự thử lại kiểu lazy")





_SITE_FORM_READY_JS = """([userSelectors, codeSelectors]) => {
    const visible = (selectors, editable = false) => selectors.some(selector => {
        try { return [...document.querySelectorAll(selector)].some(el => {
            const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
            return !el.disabled && (!editable || !el.readOnly)
                && rect.width > 0 && rect.height > 0 && style.display !== 'none'
                && style.visibility !== 'hidden' && style.opacity !== '0';
        }); } catch (_) { return false; }
    });
    return visible(userSelectors) && visible(codeSelectors, true);
}"""


async def _site_form_ready(page, domain: str, *, wait: bool = False, timeout_ms: int = 8000) -> bool:  # PATCH_MARKER_2026-10-26-v10-form-timeout
    profile = get_site_profile(domain)
    if profile is None:
        return False
    selectors = [list(profile.username_selectors), list(profile.code_selectors)]
    try:
        if not wait:
            return bool(await page.evaluate(_SITE_FORM_READY_JS, selectors))
        handle = await page.wait_for_function(_SITE_FORM_READY_JS, arg=selectors, timeout=timeout_ms, polling=100)
        try:
            return True
        finally:
            await handle.dispose()
    except Exception:
        return False


_UI_GUARDED_DOMAINS = frozenset({
    "tangquaqq88.com", "hi88-freecode.pages.dev", "getcodexx8bonus.pages.dev",
    "livemm88.net", "liverr88.net",
})


async def _ensure_submission_ui(page, domain: str, target_url: str, key: str) -> str:
    """Pre-click recovery only; never reload an active verification/submission.

    Broken decorative images alone are not a reason to discard a usable form.
    Cooldown bounds recovery when the site itself stays broken.
    """
    watcher = _PENDING_VERIFY_TASKS.get(id(page))
    if watcher is not None and not watcher.done():
        return "verification"
    if domain not in _UI_GUARDED_DOMAINS or await _site_form_ready(page, domain):
        return "ready"
    if await is_cloudflare_present(page, domain=domain):
        return "verification"
    if domain in ("livemm88.net", "liverr88.net"):
        await open_mm88_code_form(page)
    # A SPA can still be rendering after DOMContentLoaded: give it time first.
    if await _site_form_ready(page, domain, wait=True, timeout_ms=1500):  # PATCH_MARKER_2026-10-26-v10-form-timeout
        return "ready"
    if await is_cloudflare_present(page, domain=domain):
        return "verification"
    logger.warning("[UI-Recovery] %s form unavailable; restoring existing tab", domain)
    return "ready" if await _reload_page_and_refill(page, domain, target_url, key) else "unavailable"


async def _reload_page_and_refill(page, domain: str, target_url: str, key: str):
    try:
        await asyncio.to_thread(chrome_restore, domain)
        timeout_ms = int(float(_site_profile_value(
            domain,
            "navigation_timeout_seconds",
            float(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)) / 1000.0,
        )) * 1000)
        if _page_matches_target(page.url, target_url, domain):
            await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
        else:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        if domain == "llwincode.com":
            await inject_llwin_ui_tweaks(page)
        if domain in ("livemm88.net", "liverr88.net"):
            await open_mm88_code_form(page)
        if domain in _UI_GUARDED_DOMAINS:
            if not await _site_form_ready(page, domain, wait=True):
                raise RuntimeError("Application form is missing or not visible after navigation")
        await scroll_to_input_fields(page)
        await _close_unwanted_popups(page)
        settle = float(_site_profile_value(
            domain,
            "form_settle_seconds",
            getattr(Config, "MM88_FORM_SETTLE_SECONDS", 0.05)
            if domain == "livemm88.net"
            else getattr(Config, "FORM_SETTLE_SECONDS", 0.03),
        ))
        await asyncio.sleep(max(0.02, settle))
        _invalidate_page_input_cache(page)
        return True
    except Exception as e:

        if "interrupted by another navigation" in str(e):

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
        try:
            if _page_matches_target(page.url, target_url, domain):
                ready = (await _site_form_ready(page, domain)
                         if domain in _UI_GUARDED_DOMAINS
                         else await page.evaluate(
                             "() => document.readyState !== 'loading' && !!document.querySelector('input,textarea')"))
                if ready:
                    if domain == "llwincode.com":
                        await inject_llwin_ui_tweaks(page)
                    if domain in ("livemm88.net", "liverr88.net"):
                        await open_mm88_code_form(page)
                    await _close_unwanted_popups(page)
                    _invalidate_page_input_cache(page)
                    logger.info("✅ [%s] goto timeout nhưng form đã có; khôi phục không reload lại", domain)
                    return True
        except Exception:
            pass
        logger.warning(f"⚠️ [{domain}] Lỗi reload trang sau submit: {e}")
        return False


async def _quick_clean_page(page, key: str, domain: str = "") -> bool:
    try:

        dismissed = {"clicked": 0, "remaining": 0}
        profile = get_site_profile(domain) if domain else None
        if profile is None:
            return False
        targeted_dismiss = bool(
            profile and (profile.popup_dismiss_selectors or profile.popup_dismiss_texts)
        )
        if domain == "getcodexx8bonus.pages.dev":
            try:
                await page.locator(".swal2-popup.swal2-show").wait_for(
                    state="visible", timeout=350
                )
            except Exception:
                pass
        if domain:
            dismissed = await _dismiss_result_popup(page, domain)

        popup_state = {"closed": 0, "stuck": False}
        if not targeted_dismiss or dismissed.get("remaining"):
            popup_state = await _close_unwanted_popups(page)

        if dismissed.get("remaining"):
            logger.debug(f"⚠️ [{key}] Popup kết quả còn sau khi bấm nút đóng — escalate reload")
            return False
        if popup_state.get("stuck"):

            logger.debug(f"⚠️ [{key}] Popup còn cứng đầu sau quick-clean — escalate reload")
            return False
        cleared = await page.evaluate(
            """
            ([codeSelectors, userSelectors]) => {
                const first = selectors => {
                    for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                            if (r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none') return el;
                        }
                    }
                    return null;
                };
                const inp=first(codeSelectors), user=first(userSelectors);
                if (!inp || !user || inp===user || inp.disabled || inp.readOnly) return false;
                if (inp.value) {
                    const proto=inp.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:window.HTMLInputElement.prototype;
                    Object.getOwnPropertyDescriptor(proto,'value').set.call(inp,'');
                    inp.dispatchEvent(new Event('input',{bubbles:true}));
                    inp.dispatchEvent(new Event('change',{bubbles:true}));
                }
                const current=first(codeSelectors);
                return !!current && current.value==='' && !current.disabled && !current.readOnly && !!first(userSelectors);
            }
            """, [list(profile.code_selectors), list(profile.username_selectors)],
        )
        return bool(cleared) and await _refresh_used_challenges(page)
    except Exception as e:
        logger.debug(f"⚠️ [{key}] Quick-clean lỗi: {e}")
        return False


async def _clean_page_after_submit(page, domain: str, target_url: str, key: str, force_full: bool = False):
    count = bot_state._submits_since_full_reload.get(key, 0) + 1
    threshold = int(_site_profile_value(
        domain,
        "full_reload_every",
        getattr(Config, "FULL_RELOAD_EVERY_N", 100),
    ))

    if not force_full and (bool(getattr(Config, "BROWSER_REUSE_HEALTHY_FORMS", True)) or count < threshold):
        ok = await _quick_clean_page(page, key, domain)
        if ok:
            bot_state._submits_since_full_reload[key] = count
            return True

    bot_state._submits_since_full_reload[key] = 0
    restored = await _reload_page_and_refill(page, domain, target_url, key)
    if restored:
        _used_challenge_tokens.pop(page, None)
    return restored


async def submit_code_browser(
    user: str, code: str, target_url: str, systems: dict, *, sync_expected: int = 1
) -> dict:

    domain = _normalize_domain(target_url)
    code = normalize_code(code, domain)
    key = f"{domain}|{user}"
    # PATCH_MARKER_2026-10-25-v8-bugfix (#2: setdefault luon evaluate Lock() du key da co -> rac;
    # dung mau if-key nhu _submit_code_browser_locked_inner)
    if key not in bot_state.context_locks:
        bot_state.context_locks[key] = asyncio.Lock()
    lock = bot_state.context_locks[key]
    async with lock:
        try:
            return await _submit_code_browser_locked_inner(
                user, code, target_url, systems, sync_expected=sync_expected
            )
        finally:
            if domain == "tangquaqq88.com":
                _qq88_sync_leave(code, user, sync_expected)


async def submit_code_browser_tab(code: str, target_url: str, systems: dict, *, sync_group=None) -> dict:
    """Submit on an available tab without selecting or writing its account."""
    identity = {}
    try:
        result = await _submit_code_browser_locked_inner(
            "", normalize_code(code, _normalize_domain(target_url)), target_url, systems,
            tab_only=True, sync_group=sync_group, identity=identity,
        )
        result.setdefault("account", identity.get("account", ""))
        return result
    finally:
        if sync_group is not None:
            sync_group["event"].set()


async def _wait_tab_sync_group(group, page, domain: str, code: str) -> None:
    if group is None:
        return
    group["ready"].add((id(page), normalize_code(code, domain)))
    if (len({item[0] for item in group["ready"]}) >= 2
            and len({item[1] for item in group["ready"]}) >= 2):
        group["event"].set()
    try:
        await asyncio.wait_for(group["event"].wait(), timeout=group["timeout"])
    except asyncio.TimeoutError:
        group["event"].set()


def _is_site_shell_result_candidate(text: str, domain: str = "") -> bool:
    # PATCH_MARKER_2026-10-25-v8-bugfix (#5: doc marker tu SiteProfile thay vi hard-code domain +
    # chuoi trong ham. Them domain moi chi can them field vao profile.
    # QQ88/HI88 giu nguyen gia tri cu; domain khong co marker -> False.)
    profile = get_site_profile(str(domain or "").lower())
    if profile is None:
        return False
    prefixes = getattr(profile, "shell_text_prefixes", ())
    pairs = getattr(profile, "shell_text_contains", ())
    if not prefixes and not pairs:
        return False
    value = str(text or "").strip().lower()
    if len(value) < getattr(profile, "shell_text_min_length", 180):
        return False
    return (any(value.startswith(prefix) for prefix in prefixes)
            or any(a in value and b in value for a, b in pairs))


def _is_decisive_result(text: str) -> bool:

    if not text:
        return False
    return classify_result(text) in (
        ResultStatus.SUCCESS_POINTS,
        ResultStatus.SUCCESS_NO_POINTS,
        ResultStatus.FAILED,
        ResultStatus.RATE_LIMITED,
        ResultStatus.ACCOUNT_LIMIT,
        ResultStatus.CAPTCHA_EXPIRED,
        ResultStatus.CONNECTIVITY_ERROR,
    )


def _code_matches_expected(actual: str, expected: str, domain: str) -> bool:

    actual = str(actual or "").strip()
    expected = str(expected or "").strip()
    return normalize_code(actual, domain) == normalize_code(expected, domain)


def _completed_api_result(api_fut) -> str:
    """Read a completed result without letting a cancelled observer break submit."""
    if api_fut is None or not api_fut.done() or api_fut.cancelled():
        return ""
    try:
        text = api_fut.result()
        return text if _is_decisive_result(text) else ""
    except Exception:
        return ""


def _remaining_api_wait_seconds(api_timer, domain: str) -> float:
    """Use a site's API budget from the click, preserving the legacy 1s grace."""
    if api_timer is None or api_timer.click_t is None:
        return 1.0
    budget = max(0.0, float(_site_profile_value(domain, "api_response_timeout_ms", 4500))) / 1000.0
    elapsed = max(0.0, time.perf_counter() - api_timer.click_t)
    return max(1.0, budget - elapsed)


async def _resolve_result(
    api_fut, dom_factory, api_pending=None, *, api_timeout_seconds=1.0, late_dom_factory=None,
) -> str:
    # The API observer already validates endpoint, account and submitted code.
    # Avoid installing a browser poller when its decisive result is ready.
    api_text = _completed_api_result(api_fut)
    if api_text:
        return api_text
    if api_fut is None:
        return await dom_factory()
    dom_task = asyncio.ensure_future(dom_factory())
    try:
        await asyncio.wait({api_fut, dom_task}, return_when=asyncio.FIRST_COMPLETED)
        api_text = _completed_api_result(api_fut)
        if api_text:
            return api_text
        text = await dom_task
        if (not _is_decisive_result(text) and not _needs_manual_verify(text) and not api_fut.done()
                and (api_pending is None or api_pending())):
            wait_seconds = api_timeout_seconds() if callable(api_timeout_seconds) else api_timeout_seconds
            deadline = time.perf_counter() + max(0.0, float(wait_seconds))
            while not api_fut.done() and (api_pending is None or api_pending()):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(api_fut), timeout=min(0.2, remaining))
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    break
                api_text = _completed_api_result(api_fut)
                if api_text:
                    return api_text
                if late_dom_factory is not None:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break
                    try:
                        late_text = await asyncio.wait_for(
                            late_dom_factory(), timeout=min(0.3, remaining),
                        )
                    except Exception:
                        late_text = ""
                    if _is_decisive_result(late_text) or _needs_manual_verify(late_text):
                        text = late_text
                        break
        api_text = _completed_api_result(api_fut)
        if api_text:
            return api_text
        return text
    finally:
        if not dom_task.done():
            dom_task.cancel()
        await asyncio.gather(dom_task, return_exceptions=True)


class _ApiTimer:

    _SKIP = ("challenges.cloudflare.com", "turnstile", "cdn-cgi")

    _PASSIVE_DOMAINS = frozenset(("livemm88.net", "liverr88.net"))
    _REDEMPTION_PATH_HINTS = ("code", "gift", "redeem", "claim")
    _XX8_DOMAIN = "getcodexx8bonus.pages.dev"

    def __init__(self, page, endpoint: str | None = None, *, domain: str = "", submitted_code: str = "", submitted_account: str = ""):
        self._page = page
        self._endpoint = endpoint or None
        self._domain = domain
        self._submitted_code = normalize_code(submitted_code, domain)
        self._submitted_account = str(submitted_account or "").strip().lower()
        self._passive = (
            not self._endpoint
            and domain in self._PASSIVE_DOMAINS
            and bool(self._submitted_code)
            and os.getenv("SITE_PASSIVE_API_RESULTS", "true").strip().lower()
                not in ("0", "false", "off", "no")
        )
        self._t0: dict = {}
        self._tasks: set = set()
        self.api_ms: float | None = None
        self.status: int | None = None
        self.click_t: float | None = None
        self._click_wall_ms: float | None = None
        self.pre_api_ms: float | None = None
        self._timed_request = None
        self._unconfirmed_message = ""
        self._unconfirmed_status: int | None = None
        self._request_failure = ""

        self.text_future = asyncio.get_running_loop().create_future() if (self._endpoint or self._passive) else None

    def _wanted(self, url: str, method: str) -> bool:
        if method.upper() not in ("POST", "PUT", "PATCH"):
            return False
        low = url.lower()
        if any(s in low for s in self._SKIP):
            return False
        return self._endpoint in url if self._endpoint else self._passive

    def _body_values(self, value, depth=0):
        if depth > 8:
            return
        if isinstance(value, dict):
            for item in value.values():
                yield from self._body_values(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from self._body_values(item, depth + 1)
        elif isinstance(value, (str, int, float)):
            yield str(value).strip()

    def _body_matches_request(self, request) -> bool | None:
        """True: body chua dung ma + account. False: body co nhung khong dung.
        None: khong doc duoc body -> goi y giu endpoint-only.

        PATCH_MARKER_2026-10-25-v7-apitimer-std: dung chung cho ca site
        endpoint (tru XX8 von da strict rieng). Kieu body khong phu thuoc
        ten field (kiem tra theo VALUE) nen dung cho moi dang
        {username|account, code|giftcode, ...}.
        """
        raw = getattr(request, "post_data", None) or ""
        if not str(raw).strip():
            return None
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            body = parse_qs(raw, keep_blank_values=True)
        fields = tuple(self._body_values(body))
        if fields:
            code_ok = any(normalize_code(v, self._domain) == self._submitted_code for v in fields)
            acct_ok = (not self._submitted_account) or any(
                v.lower() == self._submitted_account for v in fields)
            return code_ok and acct_ok
        return None

    def _wanted_request(self, request) -> bool:
        if not self._wanted(request.url, request.method):
            return False
        if self._domain == "llwincode.com":
            try:
                body = json.loads(request.post_data or "{}")
                return (isinstance(body, dict)
                        and str(body.get("username", "")).strip().casefold() == self._submitted_account
                        and normalize_code(body.get("code", ""), self._domain) == self._submitted_code)
            except (ValueError, TypeError):
                return False
        if self._endpoint:
            if self._domain == self._XX8_DOMAIN:

                try:
                    body = json.loads(request.post_data or "{}")
                    if not isinstance(body, dict):
                        return False
                    normalized = {str(k).lower(): v for k, v in body.items()}
                    return (
                        str(normalized.get("account", "")).strip().lower() == self._submitted_account
                        and normalize_code(normalized.get("code", ""), self._domain) == self._submitted_code
                    )
                except (ValueError, TypeError):
                    return False
            # PATCH_MARKER_2026-10-25-v7-apitimer-std: endpoint/body correlation.
            # PATCH_MARKER_2026-10-26-parallel-llwin-local-labels: require exact
            # account/code values. Substring/endpoint-only fallback can accept
            # another concurrent attempt or fold a case-sensitive code.
            body_ok = self._body_matches_request(request)
            return body_ok is True

        try:
            path = urlparse(request.url).path.lower()
            raw_body = request.post_data or ""
            try:
                body = json.loads(raw_body)
            except (ValueError, TypeError):
                body = parse_qs(raw_body, keep_blank_values=True)

            def values(value, depth=0):
                if depth > 8:
                    return
                if isinstance(value, dict):
                    for item in value.values():
                        yield from values(item, depth + 1)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        yield from values(item, depth + 1)
                elif isinstance(value, (str, int, float)):
                    yield str(value).strip()

            fields = tuple(values(body))
            if not any(normalize_code(value, self._domain) == self._submitted_code for value in fields):
                return False
            if self._submitted_account and not any(
                value.lower() == self._submitted_account for value in fields
            ):
                return False
            if any(hint in path for hint in self._REDEMPTION_PATH_HINTS):
                return True
            if self._domain == "livemm88.net":
                host = (urlparse(request.url).hostname or "").lower()
                return host == self._domain or host.endswith("." + self._domain)
            return False
        except Exception:
            return False

    def _on_request(self, request):
        try:
            if self.click_t is None:
                return
            if request.resource_type in ("fetch", "xhr") and self._wanted_request(request):
                now = time.perf_counter()
                self._t0[id(request)] = now
                if self.click_t is not None and self.pre_api_ms is None:
                    self.pre_api_ms = (now - self.click_t) * 1000.0
        except Exception:
            pass

    def _on_request_failed(self, request):
        try:
            # Only a request already correlated with this account/code may
            # stop the pending wait. Unrelated iframe failures are ignored.
            if self._t0.pop(id(request), None) is not None:
                self._request_failure = str(request.failure or "request failed")[:240]
        except Exception:
            pass

    def _on_response(self, response):
        try:
            req = response.request
            t0 = self._t0.pop(id(req), None)
            matched = self._wanted_request(req)
            # A response can be delivered without this observer seeing request.
            # Recover only a matching body that started after our click; an old
            # in-flight submission must never become the current code's result.
            response_only = False
            if t0 is None and matched and self._click_wall_ms is not None:
                started_ms = (getattr(req, "timing", None) or {}).get("startTime", -1)
                response_only = (
                    isinstance(started_ms, (int, float))
                    and self._click_wall_ms <= started_ms <= time.time() * 1000.0
                )
                if response_only:
                    t0 = time.perf_counter() - (time.time() * 1000.0 - started_ms) / 1000.0
                    if self.pre_api_ms is None:
                        self.pre_api_ms = started_ms - self._click_wall_ms
            if t0 is not None and self.api_ms is None:
                self.api_ms = (time.perf_counter() - t0) * 1000.0
                self.status = response.status
                self._timed_request = req
            if (
                self.text_future is not None
                and not self.text_future.done()
                and matched
                and t0 is not None
            ):
                task = asyncio.ensure_future(self._read(response))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except Exception:
            pass

    async def _read(self, response):
        try:
            text = await _response_to_result_text(response)
        except Exception:
            text = ""
        if (self.text_future is not None and not self.text_future.done()
                and _is_decisive_result(text)):
            self.text_future.set_result(text)
        elif not _is_decisive_result(text):
            # Keep a matched but unfamiliar response for operator review.
            # HTTP 400 alone does not establish that the gift code is invalid.
            self._unconfirmed_status = response.status
            self._unconfirmed_message = await _response_to_result_text(
                response, message_only=True,
            )

    def unconfirmed_result(self) -> str:
        """Describe an observed API failure without guessing a business outcome."""
        if _completed_api_result(self.text_future):
            return ""
        status = self._unconfirmed_status if self._unconfirmed_status is not None else self.status
        if status is not None:
            message = self._unconfirmed_message or "API response has no recognized result"
            return f"HTTP {status}: {message}"[:1200]
        if self._request_failure:
            return f"API request failed: {self._request_failure}"
        if self.pre_api_ms is not None or self._t0:
            return "API request sent; no response received before result timeout"
        return ""

    def metrics(self) -> dict:
        """Browser timings in milliseconds; absent phases are not fabricated as zero.

        api_ms includes network and server work, so it is not pure network RTT.
        Reused connections often report DNS/TCP/TLS as -1.
        """
        result = {}
        if self.api_ms is not None:
            result.update(api_ms=round(self.api_ms, 1), api_status=self.status)
        if self.pre_api_ms is not None:
            result["pre_api_ms"] = round(self.pre_api_ms, 1)
        try:
            timing = self._timed_request.timing
            for label, start, end in (
                ("dns_ms", "domainLookupStart", "domainLookupEnd"),
                ("connect_ms", "connectStart", "connectEnd"),
                ("tls_ms", "secureConnectionStart", "connectEnd"),
                ("ttfb_ms", "requestStart", "responseStart"),
                ("body_ms", "responseStart", "responseEnd"),
            ):
                a, b = timing.get(start, -1), timing.get(end, -1)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) and 0 <= a <= b:
                    result[label] = round(b - a, 1)
        except Exception:
            pass
        return result

    def mark_click(self):
        self.click_t = time.perf_counter()
        self._click_wall_ms = time.time() * 1000.0

    def attach(self):
        try:
            self._page.on("request", self._on_request)
            self._page.on("response", self._on_response)
            self._page.on("requestfailed", self._on_request_failed)
        except Exception:
            pass

    def detach(self):
        for evt, fn in (
            ("request", self._on_request),
            ("response", self._on_response),
            ("requestfailed", self._on_request_failed),
        ):
            try:
                self._page.remove_listener(evt, fn)
            except Exception:
                pass
        for t in list(self._tasks):
            t.cancel()


async def _submit_code_browser_locked_inner(
    user: str, code: str, target_url: str, systems: dict, *, sync_expected: int = 1,
    tab_only: bool = False, sync_group=None, identity=None,
) -> dict:

    start_time = time.time()
    phase_start = time.perf_counter()
    submit_perf_start = phase_start
    timings: dict[str, float] = {}
    domain = _normalize_domain(target_url)
    if not is_domain_enabled(domain):
        return {"success": False, "message": "Site disabled", "status": "SITE_DISABLED"}
    key = f"{domain}|{user}"

    def mark_phase(name: str) -> None:
        nonlocal phase_start
        now = time.perf_counter()
        timings[name] = round((now - phase_start) * 1000.0, 2)
        phase_start = now

    def timed_result(payload: dict) -> dict:
        timings["total_submit_ms"] = round((time.perf_counter() - submit_perf_start) * 1000.0, 2)
        payload["latency_ms"] = dict(timings)
        return payload

    if _tab_pool is None:
        # PATCH_MARKER_2026-10-25-v8-bugfix (#7: return som nay thieu latency_ms -> dashboard
        # khong co sample; boc timed_result nhu cac nhanh sau.)
        return timed_result({"success": False, "message": "Browser chưa sẵn sàng (no TabPool)", "_infra_failure": True})

    if key not in bot_state.context_locks:
        bot_state.context_locks[key] = asyncio.Lock()

    try:
        tab_entry, page, tab_lock = await _tab_pool.acquire(domain=domain, account=None if tab_only else user)
    except BrowserVerificationPending as exc:
        return timed_result({"success": False, "message": str(exc),
                "failure_kind": "BROWSER_VERIFICATION_WAIT", "keep_page": True})
    except Exception as e:

        msg = str(e)
        if _tab_pool is not None:
            _tab_pool._warn_missing(domain, msg)
        return timed_result({
            "success": False, "message": f"No tab: {msg}",
            "_infra_failure": True, "failure_kind": "TAB_UNAVAILABLE",
        })

    api_timer = None
    submit_clicked = False
    try:
        async with tab_lock:
            tab_entry["reserved"] = False
            _pending_clean = tab_entry.pop("clean_task", None)
            if _pending_clean is not None and not _pending_clean.done():
                clean_wait = max(0.1, float(getattr(Config, "DEFERRED_CLEAN_WAIT_SECONDS", 1.0)))
                try:
                    await asyncio.wait_for(asyncio.shield(_pending_clean), timeout=clean_wait)
                except asyncio.TimeoutError:

                    _pending_clean.cancel()
                    await asyncio.gather(_pending_clean, return_exceptions=True)
                    logger.debug(
                        "⚡ [%s] deferred clean quá %.1fs — hủy để nhường mã kế tiếp",
                        key, clean_wait,
                    )
                except Exception:
                    pass
            mark_phase("tab_wait_ms")
            if not tab_only:
                bot_state.account_pages[key] = page

            if page.is_closed():
                raise RuntimeError("Existing account tab was closed; reopen it manually")

            await _dismiss_pre_submit_popups(page, domain)
            input_key = f"{key}|{id(page)}"

            try:
                page_url = page.url
            except Exception:
                page_url = ""

            if not _page_matches_target(page_url, target_url, domain):
                logger.info(f"🌐 [{domain}] Điều hướng tới {target_url}")
                if domain != "xx88code.com":
                    await asyncio.to_thread(chrome_restore, domain)
                try:
                    await page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=int(float(_site_profile_value(
                            domain,
                            "navigation_timeout_seconds",
                            float(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)) / 1000.0,
                        )) * 1000),
                    )
                    if domain in ("livemm88.net", "liverr88.net"):
                        await open_mm88_code_form(page)
                    await scroll_to_input_fields(page)
                    settle = float(_site_profile_value(
                        domain,
                        "form_settle_seconds",
                        getattr(Config, "MM88_FORM_SETTLE_SECONDS", 0.05)
                        if domain == "livemm88.net"
                        else getattr(Config, "FORM_SETTLE_SECONDS", 0.03),
                    ))
                    await asyncio.sleep(max(0.02, settle))
                    _invalidate_page_input_cache(page)
                except Exception as e:
                    return {"success": False, "message": f"Goto failed: {e}", "_infra_failure": True}

            if domain == "llwincode.com":
                await inject_llwin_ui_tweaks(page)

            if tab_only:
                user = await _read_tab_username(page, domain)
                if not user:
                    return timed_result({"success": False, "message": "Tab has no prefilled account",
                                         "failure_kind": "TAB_UNAVAILABLE", "keep_page": True})
                identity["account"] = user
                key = f"{domain}|tab:{id(page)}"
                input_key = f"{key}|{id(page)}"
                bot_state.account_pages[key] = page

            foreground_ready = False if domain == "xx88code.com" else await _wake_tab_for_submit(page, domain)
            if not foreground_ready and domain != "xx88code.com":
                await asyncio.to_thread(chrome_restore, domain)
            mark_phase("navigation_ms")

            xx88_prefill = _NOFILL
            xx88_via_cookie = False
            if domain == "xx88code.com":
                # Điền form ngay trong lúc chờ token Turnstile: trước đây bot chờ
                # token xong mới điền nên mỗi lần có ~1.2s màn hình đứng im.
                # _fill_profile_form không ném lỗi (đã bắt Exception bên trong).
                ts_task = asyncio.ensure_future(_xx88_submit_readiness(page))
                try:
                    xx88_prefill = await (_fill_tab_only_form(page, domain, user, code) if tab_only
                                          else _fill_profile_form(page, domain, user, code))
                except BaseException:
                    ts_task.cancel()
                    raise
                xx88_ready, xx88_via_cookie = await ts_task
                if not xx88_ready:
                    mark_phase("cf_wait_ms")
                    _arm_xx88_readiness(page)
                    warning_once(
                        ("xx88-verification-wait", id(page)),
                        "⏸️ [XX88] tab chưa xác minh — đã điền sẵn mã, giữ trong hàng chờ, chưa submit",
                    )
                    return timed_result({
                        "success": False, "message": "XX88 browser verification pending",
                        "status": "TOKEN_NOT_READY",
                        "failure_kind": "TOKEN_NOT_READY", "keep_page": True,
                    })
                mark_phase("cf_wait_ms")

            # V79 may reload while checking its CAPTCHA image below; fill it
            # only after that check. Other profiles can use the verified path.
            if xx88_prefill is not _NOFILL:
                fast_verify = xx88_prefill
            else:
                fast_verify = (None if domain == "v79code.com" else
                               await (_fill_tab_only_form(page, domain, user, code) if tab_only
                                      else _fill_profile_form(page, domain, user, code)))
            ui_state = "ready" if fast_verify else await _ensure_submission_ui(page, domain, target_url, key)
            if ui_state != "ready":
                return timed_result({
                    "success": False,
                    "message": "Waiting for verification" if ui_state == "verification" else "Application form unavailable",
                    "status": "PENDING_VERIFICATION" if ui_state == "verification" else "CONNECTIVITY_ERROR",
                    "failure_kind": "PENDING_VERIFICATION" if ui_state == "verification" else "UI_UNAVAILABLE",
                    "_infra_failure": ui_state != "verification",
                    "keep_page": True,
                })

            if domain == "v79code.com":
                v79_captcha_ready, v79_reloaded = await _ensure_v79_captcha_image_ready(
                    page, target_url=target_url
                )
                if v79_reloaded:
                    _invalidate_input_cache(input_key)
                if not v79_captcha_ready:
                    return timed_result({
                        "success": False,
                        "message": "V79 CAPTCHA image failed to load",
                        "status": "PENDING_VERIFICATION",
                        "failure_kind": "V79_CAPTCHA_IMAGE_UNAVAILABLE",
                        "_infra_failure": True,
                        "keep_page": True,
                    })

                if tab_only:
                    fast_verify = await _fill_tab_only_form(page, domain, user, code)

            if tab_only and not fast_verify:
                return timed_result({"success": False, "message": "Tab account or code field changed",
                                     "failure_kind": "TAB_UNAVAILABLE", "keep_page": True})

            if fast_verify is None:
                cached_entry = bot_state._input_cache.get(input_key)
                cache_was_fresh = bool(cached_entry and (time.time() - cached_entry[2]) < bot_state._input_cache_ttl)
                username_input, code_input = await find_input_fields(page, cache_key=input_key, domain=domain)

                if not code_input:
                    await asyncio.sleep(0.05)
                    _invalidate_input_cache(input_key)
                    cache_was_fresh = False
                    username_input, code_input = await find_input_fields(page, cache_key=input_key, domain=domain)

                if not code_input and domain == "liverr88.net":

                    try:
                        await page.goto(
                            target_url,
                            wait_until="domcontentloaded",
                            timeout=int(float(_site_profile_value(domain, "navigation_timeout_seconds", 12.0)) * 1000),
                        )
                        await _wake_tab_for_submit(page)
                        await scroll_to_input_fields(page)
                        _invalidate_input_cache(input_key)
                        username_input, code_input = await find_input_fields(
                            page, cache_key=input_key, domain=domain,
                        )
                    except Exception as recover_exc:
                        logger.warning(
                            "⚠️ [%s|%s] khôi phục ô nhập trước submit lỗi: %s",
                            domain, user, recover_exc,
                        )

                if not code_input:
                    if domain in ("livemm88.net", "liverr88.net") and await open_mm88_code_form(page):
                        _invalidate_input_cache(input_key)
                        username_input, code_input = await find_input_fields(page, cache_key=input_key, domain=domain)

                if not code_input:
                    if await is_cloudflare_present(page, domain=domain):
                        logger.warning(f"⚠️ [{domain}] Cloudflare challenge không có form — cần xác minh thủ công")
                        return timed_result({
                            "success": False,
                            "message": "Cloudflare challenge",
                            "failure_kind": "CLOUDFLARE_CHALLENGE",
                            "_infra_failure": True,
                            "keep_page": True,
                        })
                    return timed_result({
                        "success": False,
                        "message": "Không tìm thấy ô nhập code sau reload (site có thể đã đổi UI)",
                        "failure_kind": "SITE_UI_CHANGED",
                        "_infra_failure": True,
                    })

                if not cache_was_fresh:
                    await scroll_to_input_fields(page)

                preserve_prefilled = False
                try:

                    verify = await page.evaluate(
                        REACT_FILL_PRESERVE_JS,
                        [username_input, code_input, user, code,
                         bool(getattr(Config, "PRESERVE_PREFILLED_USERNAME", True))],
                    )
                    preserve_prefilled = bool(verify.get("preserved"))
                    if preserve_prefilled:
                        logger.debug(f"✅ [{domain}|{user}] giữ tài khoản đã điền sẵn, chỉ nhập code")
                    mark_phase("input_fill_ms")
                    actual_user = (verify.get("actualUser") or "").strip()
                    actual_code = (verify.get("actualCode") or "").strip()
                    if username_input and actual_user != str(user).strip():
                        raise RuntimeError(
                            f"Account input mismatch: expected={user!r} actual={actual_user!r}"
                        )
                    if not _code_matches_expected(actual_code, code, domain):
                        raise RuntimeError(
                            f"Code input mismatch: expected={code!r} actual={actual_code!r}"
                        )
                    tab_entry["prefilled"] = actual_user.lower()
                except Exception as e:
                    _invalidate_input_cache(input_key)
                    username_input, code_input = await find_input_fields(page, cache_key=input_key, domain=domain)
                    if code_input:
                        try:
                            verify = await page.evaluate(
                                REACT_FILL_VERIFY_JS,
                                [username_input, code_input, user, code, True],
                            )
                            actual_user = (verify.get("actualUser") or "").strip()
                            actual_code = (verify.get("actualCode") or "").strip()
                            if username_input and actual_user != str(user).strip():
                                return {
                                    "success": False,
                                    "message": f"Account input mismatch after retry: {actual_user!r}",
                                    "_infra_failure": True,
                                }
                            if not _code_matches_expected(actual_code, code, domain):
                                return {
                                    "success": False,
                                    "message": f"Code input mismatch after retry: {actual_code!r}",
                                    "_infra_failure": True,
                                }
                        except Exception as e2:
                            return {"success": False, "message": f"Fill error: {e2}", "_infra_failure": True}
                    else:
                        return {"success": False, "message": f"Fill error: {e}", "_infra_failure": True}

            else:
                tab_entry["prefilled"] = fast_verify["actualUser"].strip().lower()
                timings["form_dom_calls"] = 1
                timings["form_dom_ms"] = round(float(fast_verify.get("dom_ms", 0)), 2)
                mark_phase("input_fill_ms")

            if domain == "liverr88.net":
                try:
                    rr88_challenge_pending = await is_cloudflare_present(page, domain=domain)
                except Exception:
                    rr88_challenge_pending = True
                if rr88_challenge_pending:
                    if await _wait_for_challenge_token_retries(page, domain):
                        rr88_challenge_pending = False
                if rr88_challenge_pending:
                    logger.warning(
                        "⏸️ [RR88] Turnstile chưa xong — defer code trước submit, "
                        "giữ tab để xác minh thủ công; không tạo watcher giả"
                    )
                    try:
                        await _show_manual_verification_page(
                            page, domain, f"[RR88|{user}|{code}]"
                        )
                    except Exception as fg_exc:
                        logger.warning(
                            "⚠️ [RR88|%s|%s] không đưa tab xác minh ra màn hình: %s",
                            user, code, fg_exc,
                        )
                    mark_phase("challenge_wait_ms")
                    return timed_result({
                        "success": False,
                        "message": "RR88 inline Turnstile token not ready",
                        "status": "TOKEN_NOT_READY",
                        "failure_kind": "TOKEN_NOT_READY",
                        "keep_page": True,
                    })

            if domain not in {"liverr88.net", "xx88code.com"}:
                cf_ready = await _wait_for_challenge_token_retries(page, domain)
                if not cf_ready:
                    mark_phase("cf_wait_ms")
                    warning_once(
                        ("turnstile-token", domain, str(user).casefold()),
                        "⏸️ [%s|%s] challenge chưa có token sau retry window — defer %s",
                        domain, user, code,
                    )
                    return timed_result({
                        "success": False,
                        "message": f"{domain} challenge token not ready",
                        "status": "TOKEN_NOT_READY",
                        "failure_kind": "TOKEN_NOT_READY",
                        "keep_page": True,
                    })

            mark_phase("cf_wait_ms")
            try:

                pre_click_scopes = (
                    'form, [role="dialog"], .modal, [role="alert"], [role="status"]'
                    if domain in {"tangquaqq88.com", "hi88-freecode.pages.dev", "xx88code.com"}
                    else 'form, [role="dialog"], .modal, main, #app, #root'
                )
                pre_click_text = await page.evaluate(
                    """
                    (scopeSelector) => {
                        const scopes = document.querySelectorAll(scopeSelector);
                        let text = '';
                        for (const scope of scopes) {
                            text += (scope.innerText || '') + '\n';
                            if (text.length >= 30000) break;
                        }
                        return text.slice(0, 30000);
                    }
                    """,
                    pre_click_scopes,
                )
            except Exception:
                pre_click_text = ""

            mark_phase("snapshot_ms")

            _profile = get_site_profile(domain)
            api_timer = _ApiTimer(
                page, getattr(_profile, "public_api_endpoint", None),
                domain=domain, submitted_code=code, submitted_account=user,
            )
            api_timer.attach()
            if (
                domain in {"tangquaqq88.com", "hi88-freecode.pages.dev"}
                and bool(getattr(Config, "FOREGROUND_ON_VERIFICATION_SUBMIT", True))
                and not foreground_ready
            ):
                await _show_manual_verification_page(
                    page, domain, f"[Submit|{domain}|{user}|{code}]"
                )

            if (
                domain == "tangquaqq88.com"
                and int(sync_expected or 1) > 1
                and bool(getattr(Config, "QQ88_SYNC_BARRIER", True))
            ):
                if not await _submit_button_ready(page, domain):
                    return timed_result({
                        "success": False,
                        "message": "QQ88 sync: submit button chưa READY",
                        "failure_kind": "SUBMIT_BUTTON_UNAVAILABLE",
                        "_infra_failure": True,
                    })
                barrier_ok, barrier_wait_ms = await _wait_qq88_sync_barrier(
                    code, user, int(sync_expected)
                )
                timings["sync_barrier_wait_ms"] = round(barrier_wait_ms, 2)
                if not barrier_ok:
                    return timed_result({
                        "success": False,
                        "message": "QQ88 sync barrier timeout/cancelled",
                        "status": "SYNC_ABORTED",
                        "failure_kind": "SYNC_ABORTED",
                        "keep_page": True,
                    })

            # Reserve a shared per-domain/account slot at the actual click
            # boundary, after acquiring the tab and preparing the form.
            # Preserve QQ88's configured simultaneous-submit barrier.
            _account_gap, _domain_gap = getattr(Config, "SITE_SUBMIT_GAPS", {}).get(domain, (0.0, 0.0))
            if domain == "tangquaqq88.com" and int(sync_expected or 1) > 1:
                _domain_gap = 0.0
            _pacer_before = time.perf_counter()
            await submit_pacer.reserve(
                domain, user, account_gap=float(_account_gap), domain_gap=float(_domain_gap),
            )
            timings["site_pacer_ms"] = round((time.perf_counter() - _pacer_before) * 1000.0, 2)
            challenge_tokens = await _challenge_token_snapshot(page)
            if domain == "xx88code.com" and not xx88_via_cookie and (
                not challenge_tokens or not challenge_tokens.get("turnstile")
                or challenge_tokens.get("turnstile") == (_used_challenge_tokens.get(page) or {}).get("turnstile")
            ):
                _arm_xx88_readiness(page)
                return timed_result({
                    "success": False, "message": "XX88 verification changed before submit",
                    "status": "BROWSER_VERIFICATION_WAIT",
                    "failure_kind": "BROWSER_VERIFICATION_WAIT", "keep_page": True,
                })
            await _wait_tab_sync_group(sync_group, page, domain, code)
            if tab_only and await _read_tab_username(page, domain) != user:
                return timed_result({"success": False, "message": "Tab account changed before submit",
                                     "failure_kind": "TAB_UNAVAILABLE", "keep_page": True})
            api_timer.mark_click()
            click_started_perf = time.perf_counter()
            # Metric for click must exclude site pacing and sync-barrier wait.
            phase_start = click_started_perf
            # CDP can lose the acknowledgement after the browser dispatched
            # the click. Clear this only when the helper confirms no click.
            submit_clicked = True
            clicked = await click_submit_fast(page, domain=domain)
            submit_clicked = bool(clicked)
            if clicked:
                _used_challenge_tokens[page] = challenge_tokens
            if clicked and domain == "tangquaqq88.com" and int(sync_expected or 1) > 1:
                skew_ms = await _note_qq88_sync_click(code, user)
                if skew_ms is not None:
                    timings["sync_click_skew_ms"] = round(skew_ms, 2)
            timings.setdefault(
                "submit_click_start_offset_ms",
                round((click_started_perf - submit_perf_start) * 1000.0, 2),
            )
            mark_phase("submit_click_ms")
            if not clicked:
                if domain == "xx88code.com":
                    _arm_xx88_readiness(page, need_button=True)
                    return timed_result({
                        "success": False, "message": "XX88 submit control not ready",
                        "status": "BROWSER_VERIFICATION_WAIT",
                        "failure_kind": "BROWSER_VERIFICATION_WAIT", "keep_page": True,
                    })
                return timed_result({
                    "success": False, "message": "No usable submit button",
                    "failure_kind": "SUBMIT_BUTTON_UNAVAILABLE", "_infra_failure": True,
                })

            # PATCH_MARKER_2026-10-26-llwin-slider-logs2: GoCaptcha chi hien SAU
            # khi bam "Nhan code ngay"; site chi gui POST use-code-public
            # sau khi keo xong, nen giai o day, truoc khi doc ket qua.
            # Poll 2s vi khung co the hien muon (anh puzzle load sau click);
            # ban slider cu chi check 1 lan ngay sau click nen de lot truong
            # hop nay, va log live 26/10 15:42 toan NO RESULT sau ~5s.
            # Neu giai xong thi roi xuong luong doc ket qua binh thuong,
            # khong di qua watcher thu cong. Neu khong qua thi tra
            # PENDING_VERIFICATION + keep_page, khong tinh la ma loi.
            if clicked and domain == "llwincode.com":
                try:
                    _llwin_slider_seen = await is_llwin_slider_visible(page)
                    _llwin_poll_deadline = time.monotonic() + 2.0
                    while not _llwin_slider_seen and time.monotonic() < _llwin_poll_deadline:
                        await asyncio.sleep(0.2)
                        _llwin_slider_seen = await is_llwin_slider_visible(page)
                    if _llwin_slider_seen:
                        _llwin_dbg = tempfile.mkdtemp(prefix=f"llwin_slider_{user}_{code}_")
                        logger.info(f"\U0001f9e9 [LLwin] slider hien sau submit \u2014 dang giai (debug: {_llwin_dbg})")
                        if (
                            bool(getattr(Config, "LLWIN_MANUAL_SLIDER_MODE", True))
                            and not bool(getattr(Config, "LLWIN_OS_MOUSE_MODE", False))
                        ):
                            arm_verification_watcher(
                                page, domain=domain, code=code, user=user,
                                target_url=target_url, key=key, systems=systems,
                            )
                            return timed_result({
                                "success": False,
                                "message": "LLwin slider đang chờ kéo thủ công",
                                "status": "PENDING_VERIFICATION",
                                "failure_kind": "PENDING_VERIFICATION",
                                "keep_page": True,
                            })
                        _llwin_ok = await solve_llwin_slider(
                            page, debug_dir=_llwin_dbg,
                            on_os_mouse_required=lambda: _send_captcha_admin_alert(
                                domain, user, message=OS_MOUSE_WARNING,
                            ),
                        )
                        logger.info(f"\U0001f9e9 [LLwin] slider {'QUA' if _llwin_ok else 'CHUA QUA'} (debug: {_llwin_dbg})")
                        mark_phase("llwin_slider_ms")
                        if not _llwin_ok:
                            arm_verification_watcher(
                                page, domain=domain, code=code, user=user,
                                target_url=target_url, key=key, systems=systems,
                                llwin_slider_already_attempted=bool(
                                    getattr(Config, "LLWIN_OS_MOUSE_MODE", False)
                                ),
                            )
                            return timed_result({
                                "success": False,
                                "message": "LLwin slider chua qua \u2014 giu tab, khong tinh la ma loi",
                                "status": "PENDING_VERIFICATION",
                                "failure_kind": "LLWIN_SLIDER_FAILED",
                                "keep_page": True,
                            })
                except Exception as _llwin_exc:
                    logger.warning(f"\u26a0\ufe0f [LLwin] giai slider loi: {_llwin_exc}")
                    arm_verification_watcher(
                        page, domain=domain, code=code, user=user,
                        target_url=target_url, key=key, systems=systems,
                        llwin_slider_already_attempted=bool(
                            getattr(Config, "LLWIN_OS_MOUSE_MODE", False)
                        ),
                    )
                    return timed_result({
                        "success": False,
                        "message": f"LLwin slider loi: {_llwin_exc}",
                        "status": "PENDING_VERIFICATION",
                        "failure_kind": "LLWIN_SLIDER_FAILED",
                        "keep_page": True,
                    })

            if domain in {"tangquaqq88.com", "hi88-freecode.pages.dev", "llwincode.com"}:
                await asyncio.sleep(0.05)
                manual_hint = await _manual_verify_hint_now(page)
                if manual_hint:
                    logger.warning(
                        "🖐️ [%s] phát hiện xác minh thủ công ngay sau submit — chuyển thẳng sang watcher",
                        domain,
                    )
                    mark_phase("challenge_wait_ms")
                    arm_verification_watcher(
                        page, domain=domain, code=code, user=user,
                        target_url=target_url, key=key, systems=systems,
                    )
                    return timed_result({
                        "success": False,
                        "message": "Cần xác thực thủ công",
                        "status": "PENDING_VERIFICATION",
                        "failure_kind": "PENDING_VERIFICATION",
                        "keep_page": True,
                    })

            verified_clicked = False
            if domain in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:
                verify_tokens = await _challenge_token_snapshot(page)
                verified_clicked = await click_verification_button_if_present(page, domain=domain)
                if verified_clicked:
                    _used_challenge_tokens[page] = verify_tokens
            if verified_clicked:
                logger.info(f"✅ [Browser|{domain}] đã bấm nút Xác thực và Cloudflare đã xác minh")
            elif domain in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:

                if await _cf_already_passed(page, domain=domain):
                    await _close_unwanted_popups(page)
                    logger.info(f"✅ [Browser|{domain}] Cloudflare đã xác minh từ trước")
                    verified_clicked = True
                else:

                    try:
                        challenge_pending = await is_cloudflare_present(page, domain=domain)
                    except Exception:
                        challenge_pending = False
                    if challenge_pending:
                        logger.warning(
                            f"⚠️ [{domain}] Turnstile chưa hoàn tất — giữ nguyên trang, "
                            "không reload và chờ xác minh thủ công"
                        )
                        mark_phase("challenge_wait_ms")
                        arm_verification_watcher(
                            page, domain=domain, code=code, user=user,
                            target_url=target_url, key=key, systems=systems,
                        )
                        return timed_result({
                            "success": False,
                            "message": "Turnstile verification pending",
                            "status": "PENDING_VERIFICATION",
                            "failure_kind": "PENDING_VERIFICATION",
                            "keep_page": True,
                        })
            mark_phase("challenge_wait_ms")
            click_elapsed = time.time() - start_time
            logger.info(f"🚀 [Browser|{user}] SUBMIT {code} ({click_elapsed:.2f}s)")

            result_text = ""
            timeout_by_domain = getattr(Config, "RESULT_DETECTION_TIMEOUT_BY_DOMAIN", {})
            default_timeout_ms = (
                7000 if domain == "llwincode.com"
                else getattr(Config, "RESULT_DETECTION_TIMEOUT", 5000)
            )
            profile_timeout_ms = _site_profile_value(
                domain,
                "result_timeout_ms",
                default_timeout_ms,
            )
            result_timeout_ms = timeout_by_domain.get(domain, default_timeout_ms)
            if domain not in timeout_by_domain:
                result_timeout_ms = profile_timeout_ms
            result_timeout_s = float(result_timeout_ms) / 1000.0
            fast_window_ms = int(max(0.0, float(_site_profile_value(
                domain,
                "selector_fast_window_seconds",
                getattr(Config, "RESULT_SELECTOR_FAST_WINDOW_SECONDS", 0.80),
            ))) * 1000)

            abort_token = f"w{time.monotonic_ns()}"
            dom_observer_started = False

            async def _dom_wait() -> str:
                nonlocal dom_observer_started
                dom_observer_started = True
                try:
                    handle = await page.wait_for_function(
                        _RESULT_WAIT_JS,
                        arg=[
                            _get_domain_result_selectors(domain),
                            _PRIORITY_RESULT_SELECTORS,
                            pre_click_text or "",
                            list(_TRANSIENT_CF_PATTERNS),
                            fast_window_ms,
                            int(time.time() * 1000),
                            list(_RESULT_KEYWORDS),
                            abort_token,
                        ],
                        timeout=max(300.0, result_timeout_s * 1000.0),
                        polling=max(20.0, float(_site_profile_value(
                            domain,
                            "result_poll_interval",
                            0.10,
                        )) * 1000.0),
                    )
                    try:
                        _v = str(await handle.json_value() or "").strip()
                        return "" if _v == "__ABORTED__" else _v
                    finally:
                        try:
                            await handle.dispose()
                        except Exception:
                            pass
                except Exception:

                    try:
                        return await detect_result_text(
                            page, domain=domain, before_text=pre_click_text, selector_only=False
                        )
                    except Exception:
                        return ""

            result_text = await _resolve_result(
                api_timer.text_future if api_timer is not None else None,
                _dom_wait,
                api_pending=lambda: bool(api_timer and (api_timer._t0 or api_timer._tasks)),
                api_timeout_seconds=lambda: _remaining_api_wait_seconds(api_timer, domain),
                late_dom_factory=lambda: detect_result_text(
                    page, domain=domain, before_text=pre_click_text, selector_only=False,
                ),
            )

            if dom_observer_started:
                async def _abort_wait(_pg=page, _tok=abort_token):
                    try:
                        await asyncio.wait_for(_pg.evaluate(
                            "t => { (window.__botWaitAbort = window.__botWaitAbort || {})[t] = 1; }",
                            _tok,
                        ), timeout=1.0)
                    except Exception:
                        pass
                _t = asyncio.create_task(_abort_wait())
                _abort_wait_tasks.add(_t)
                _t.add_done_callback(_abort_wait_tasks.discard)

            elapsed = time.time() - start_time
            mark_phase("result_wait_ms")
            if api_timer is not None:
                timings.update(api_timer.metrics())

            status = classify_result(result_text)

            if status in (ResultStatus.NO_RESULT, ResultStatus.AMBIGUOUS):
                grace = max(0.0, min(3.0, float(getattr(
                    Config, "UNKNOWN_RESULT_GRACE_SECONDS", 0.6
                ))))
                deadline = time.monotonic() + grace
                while grace > 0 and time.monotonic() < deadline:
                    late_api = _completed_api_result(api_timer.text_future if api_timer else None)
                    if late_api:
                        result_text = late_api
                        status = classify_result(result_text)
                        logger.info("🔎 [%s] late API result detected in grace window", domain)
                        break
                    if safe_is_closed(page):
                        break
                    await asyncio.sleep(min(0.12, max(0.01, deadline - time.monotonic())))
                    try:
                        late = await detect_result_text(page, domain=domain, before_text=pre_click_text or "")
                    except Exception:
                        late = ""
                    if _is_decisive_result(late):
                        result_text = late
                        status = classify_result(result_text)
                        logger.info("🔎 [%s] late result detected in grace window", domain)
                        break
                late_api = _completed_api_result(api_timer.text_future if api_timer else None)
                if late_api:
                    result_text = late_api
                    status = classify_result(result_text)
                if api_timer is not None:
                    timings.update(api_timer.metrics())
            if (status in (ResultStatus.NO_RESULT, ResultStatus.AMBIGUOUS)
                    and api_timer is not None and not _needs_manual_verify(result_text)):
                api_diagnostic = api_timer.unconfirmed_result()
                if api_diagnostic:
                    # A validated request/response is useful evidence even
                    # when neither API nor DOM confirms the business result.
                    result_text = api_diagnostic
                    status = ResultStatus.AMBIGUOUS
            if status.value in ("NO_RESULT", "AMBIGUOUS") and _needs_manual_verify(result_text):
                logger.warning(
                    f"⚠️ [{domain}] Site yêu cầu xác thực riêng (captcha ảnh) — "
                    f"result_text={result_text[:200]!r}"
                )
                arm_verification_watcher(
                    page, domain=domain, code=code, user=user,
                    target_url=target_url, key=key, systems=systems,
                )
                return timed_result({
                    "success": False,
                    "message": "Cần xác thực thủ công",
                    "status": "PENDING_VERIFICATION",
                    "failure_kind": "PENDING_VERIFICATION",
                    "keep_page": True,
                })

            clean_cb = _clean_page_after_submit
            # submit-opt: XX88 may also defer cleanup, but only after a
            # decisive result; NO_RESULT/AMBIGUOUS keep the old synchronous
            # cleanup so an unclear tab state is never handed to the next code.
            _defer_clean = domain in _DEFER_CLEAN_DOMAINS and (
                domain != "xx88code.com"
                or status.value not in ("NO_RESULT", "AMBIGUOUS")
            )
            if _defer_clean:
                async def clean_cb(pg, dom, url, k, _entry=tab_entry):

                    async def _run():
                        try:
                            await _clean_page_after_submit(pg, dom, url, k)
                        except Exception as exc:
                            logger.debug(f"⚠️ [{k}] deferred clean lỗi: {exc}")
                    _entry["clean_task"] = asyncio.ensure_future(_run())
                    return True
            callbacks = {
                "take_screenshot": take_result_screenshot,
                "clean_page": clean_cb,
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
            result = dict(outcome.result)
            mark_phase("cleanup_ms")
            timings["total_submit_ms"] = round((time.perf_counter() - submit_perf_start) * 1000.0, 2)
            result["latency_ms"] = dict(timings)

            update_latency(domain, user, code, timings)
            return result

    except asyncio.CancelledError:
        if submit_clicked:
            # An outer timeout must not cause the durable queue to replay a
            # redemption that may already have reached the website.
            return timed_result({
                "success": False,
                "message": "Browser request cancelled after possible submit; result unconfirmed",
                "failure_kind": "UNKNOWN_AFTER_SUBMIT", "status": "NO_RESULT",
                "keep_page": True,
            })
        raise
    except Exception as e:
        elapsed = time.time() - start_time
        err_str = str(e)
        if "Target page, context or browser has been closed" in err_str or "TargetClosedError" in type(e).__name__:
            try:
                await get_or_launch_browser_context("shared", force_reconnect=True, domain=domain)
                _invalidate_input_cache(key)
            except Exception as reconnect_exc:
                warning_once(
                    ("reconnect_fail", domain),
                    "⚠️ [Browser|%s] reconnect Edge sau TargetClosed thất bại: %s",
                    domain, reconnect_exc, interval=30.0,
                )
        try:
            systems["performance_monitor"].record_task("submit_code", elapsed, False)
        except Exception:
            pass
        logger.error(f"❌ [Browser|{domain}] {e}")
        if submit_clicked:

            return {
                "success": False, "message": str(e),
                "failure_kind": "UNKNOWN_AFTER_SUBMIT", "status": "NO_RESULT",
            }
        return {"success": False, "message": str(e), "_infra_failure": True}
    finally:
        try:
            tab_entry["reserved"] = False
            if _tab_pool is not None:
                _tab_pool._notify_available(domain)
        except Exception as release_exc:
            logger.warning(
                "⚠️ [Browser|%s] nhả tab thất bại, tab có thể bị kẹt reserved: %s",
                domain, release_exc,
            )
        if api_timer is not None:
            api_timer.detach()


async def _recover_browser_endpoint(ep):
    async def attach_and_restore():
        await _reset_playwright_driver(ep)
        ep.browser = None
        ep.context = None
        ep.retry_at = 0.0
        await get_context_for_endpoint(ep, force_reconnect=True)
        if _tab_pool is not None:
            await _tab_pool.recover_endpoint(ep)

    try:
        if getattr(Config, "CHROME_AUTO_LAUNCH", True) and ep.host in {"127.0.0.1", "localhost", "::1"}:
            success = await chrome_launcher.restart_chrome(
                ep.port, chrome_launcher.profile_for_port(ep.port), attach_and_restore)
        else:
            await attach_and_restore()
            success = True
        ep.recovery_exhausted = not success
        ep.was_down = not success
        return success
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        ep.recovery_exhausted = True
        logger.error("[Browser-Watchdog] %s recovery failed: %s", ep.label, exc)
        return False


async def browser_watchdog():

    interval = max(10.0, float(getattr(Config, "CDP_PING_INTERVAL", 15.0)))
    last_memory_maintenance = 0.0
    while bot_state.is_running:
        try:
            await asyncio.sleep(interval)
            healthy = 0
            for ep in _needed_endpoints():
                reachable = await _check_chrome_cdp_port_reachable_async(ep.port, timeout=2.0, host=ep.host)
                if not reachable and ep.is_connected():
                    logger.debug("⚠️ [Browser-Watchdog] %s CDP %s TCP probe timeout (websocket still connected)", ep.label, ep.port)
                    continue
                if not ep.is_connected():
                    if not ep.was_down:
                        logger.critical(f"❌ [Browser-Watchdog] Mất kết nối {ep.label} CDP {ep.port}; đang reconnect")
                        ep.was_down = True
                    if ep.recovery_task is not None and not ep.recovery_task.done():
                        continue
                    # Exhaustion is latched per outage, preventing launch/alert
                    # storms. A manually restored CDP endpoint can still recover.
                    if ep.recovery_exhausted and not reachable:
                        continue
                    ep.recovery_task = asyncio.create_task(
                        _recover_browser_endpoint(ep), name=f"{ep.name}-recovery")
                    ep.recovery_task.add_done_callback(_consume_cdp_connect_result)
                    continue
                if ep.was_down:
                    logger.info(f"✅ [Browser-Watchdog] {ep.label} CDP {ep.port} phản hồi trở lại")
                    ep.was_down = False
                healthy += 1
                ep.recovery_exhausted = False
            if healthy == 0:
                continue
            stale_keys = [key for key, page in list(bot_state.account_pages.items()) if safe_is_closed(page)]
            for key in stale_keys:

                bot_state.account_pages.pop(key, None)
                bot_state._input_cache.pop(key, None)
                logger.info(
                    "♻️ [Browser-Watchdog] Tab stale %s đã được gỡ; "
                    "TabPool sẽ nhận lại tab sau phục hồi CDP",
                    key,
                )
            global _browser_memory_pressure_requested
            pressure = bool(_browser_memory_pressure_requested)
            maintenance_interval = max(15.0, float(getattr(
                Config, "TAB_POOL_MEMORY_MAINTENANCE_INTERVAL_SECONDS", 90.0
            )))
            if _tab_pool is not None and (
                pressure or time.monotonic() - last_memory_maintenance >= maintenance_interval
            ):
                try:
                    _browser_memory_pressure_requested = False
                    last_memory_maintenance = time.monotonic()
                    await _tab_pool.collect_garbage(
                        idle_ttl=getattr(Config, "TAB_POOL_IDLE_TTL", 900.0),
                        min_tabs_per_domain=getattr(Config, "TAB_POOL_MIN_TABS_PER_DOMAIN", 2),
                    )
                    memory_stats = await _tab_pool.cleanup_idle_memory(
                        compact_idle_seconds=getattr(Config, "TAB_POOL_MEMORY_COMPACT_IDLE_SECONDS", 120.0),
                        reload_idle_seconds=getattr(Config, "TAB_POOL_MEMORY_RELOAD_IDLE_SECONDS", 900.0),
                        reload_min_gap_seconds=getattr(Config, "TAB_POOL_MEMORY_RELOAD_MIN_GAP_SECONDS", 45.0),
                        max_reloads_per_pass=getattr(Config, "TAB_POOL_MEMORY_MAX_RELOADS_PER_PASS", 1),
                        max_compacts_per_pass=getattr(Config, "TAB_POOL_MEMORY_MAX_COMPACTS_PER_PASS", 2),
                        force=pressure,
                    )
                    if (
                        memory_stats.get("compacted")
                        or memory_stats.get("reloaded")
                        or memory_stats.get("gc")
                        or memory_stats.get("media_unloaded")
                    ):
                        _notable = bool(
                            pressure
                            or memory_stats.get("reloaded")
                            or memory_stats.get("media_unloaded")
                        )
                        (logger.info if _notable else logger.debug)(
                            "🧹 [TabPool-Memory] %s", memory_stats
                        )
                except Exception as exc:
                    logger.debug(f"⚠️ [TabPool-GC] cleanup lỗi: {exc}")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug(f"⚠️ browser_watchdog error: {exc}")


async def cleanup_browsers():
    endpoints = (_CHROME, _CHROME2)
    for ep in endpoints:
        if ep.recovery_task is not None:
            ep.recovery_task.cancel()
            await asyncio.gather(ep.recovery_task, return_exceptions=True)
            ep.recovery_task = None
    for ep in endpoints:
        if ep.connect_task is not None:
            if not ep.connect_task.done():
                ep.connect_task.cancel()
            await asyncio.gather(ep.connect_task, return_exceptions=True)
            ep.connect_task = None
        ep.retry_at = 0.0
        ep.last_error = None
    watchers = list(_PENDING_VERIFY_TASKS.values())
    for task in watchers:
        task.cancel()
    if watchers:
        await asyncio.gather(*watchers, return_exceptions=True)
    _PENDING_VERIFY_TASKS.clear()
    _PENDING_VERIFY_PAGES.clear()
    readiness = list(_xx88_ready_tasks.values())
    for task in readiness:
        task.cancel()
    if readiness:
        await asyncio.gather(*readiness, return_exceptions=True)
    _xx88_ready_tasks.clear()

    await asyncio.gather(*(
        _release_page_performance(page) for page in list(_perf_network_sessions)
    ), return_exceptions=True)

    for ep in endpoints:
        ep.context = None
        ep.browser = None
        if ep.pw is not None:
            try:
                await ep.pw.stop()
            except Exception:
                pass
            ep.pw = None
