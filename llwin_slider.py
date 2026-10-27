"""Async GoCaptcha slider solver for llwincode.com (LLwin).

PATCH_MARKER_2026-10-26-llwin-slider
PATCH_MARKER_2026-10-26-llwin-slider-logs2

OS mouse mode uses the same image gap detector, then drives the real Windows
cursor through PyAutoGUI. Native renderer geometry supplies DPI/zoom-aware
screen coordinates. A failed physical drag falls back to the Playwright mouse.

Why this file exists instead of importing slider_captcha_solver.py:
the standalone solver uses the *sync* Playwright API, while the bot
uses ``playwright.async_api``. A sync solver cannot drive an async
page, and running it in ``asyncio.to_thread`` would not fix that:
the page object itself is async. The legacy Playwright mode retains
edge-template gap detection and in-hold correction. OS mouse mode
uses that detector for one physical drag before the Playwright fallback.

1. Detection (OpenCV) runs in ``asyncio.to_thread`` because it only
   touches image bytes, never the page. All Playwright calls are
   awaited on the bot's own event loop.
2. No shared ``slider_ratio.txt`` / ``gocap_live_*.png`` files in the
   working directory. The ratio is learned inside one attempt (the
   in-hold correction already measures tile_delta / drag_delta), and
   debug screenshots are written only when the caller passes a
   per-submit ``debug_dir``. Parallel tabs therefore cannot
   overwrite each other's files.

Selectors are the live llwincode.com selectors verified 2026-10-26:
wrapper Kpdey, body KJKNu, picture LRwbY, tile 8pkQD,
dragBlock bFlwx, dragSlideBar noauW, iconBlock mVB8B. The tile
locator falls back to a body div holding a data:image/png img, so a
hash rebuild does not hard-fail the whole solve.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import logging
import math
import random
import sys
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger("autobot.llwin_slider")
OS_MOUSE_WARNING = (
    "🤖 Bot đang điều khiển chuột thật để kéo Captcha. Vui lòng không chạm vào chuột!"
)
_OS_MOUSE_LOCK = asyncio.Lock()
_VIEWPORT_METRICS_JS = """() => ({
    width: innerWidth, height: innerHeight, dpr: devicePixelRatio,
    focused: document.hasFocus(), visible: document.visibilityState === 'visible',
    visual_scale: window.visualViewport ? window.visualViewport.scale : 1
})"""

GOCAPTCHA = {
    "wrapper": ".gocaptcha-module_wrapper__Kpdey",
    "body": ".gocaptcha-module_body__KJKNu",
    "picture": ".gocaptcha-module_picture__LRwbY",
    "tile_class": ".index-module_tile__8pkQD",
    "drag_block": ".gocaptcha-module_dragBlock__bFlwx",
    "drag_bar": ".gocaptcha-module_dragSlideBar__noauW",
    "icon_block": ".gocaptcha-module_iconBlock__mVB8B",
}


def _find_gap_from_png(png_bytes: bytes, piece_box: tuple[int, int, int, int]) -> dict:
    """Edge-template gap detection, sync (runs in a thread).

    Mirrors find_gap_by_edge_template in slider_captcha_solver.py,
    but takes PNG bytes instead of a file path.
    """
    import cv2
    import numpy as np

    data = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Khong giai ma duoc anh captcha")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 50, 150)

    px, py, pw, ph = [int(v) for v in piece_box]
    if px < 0 or py < 0 or px + pw > edges.shape[1] or py + ph > edges.shape[0]:
        raise ValueError(f"piece_box {piece_box} vuot khoi anh {edges.shape[::-1]}")
    templ = edges[py:py + ph, px:px + pw]
    if templ.size == 0 or float(templ.mean()) == 0:
        raise ValueError("Template vien cua manh rong (anh qua mo / box sai)")

    res = cv2.matchTemplate(edges, templ, cv2.TM_CCOEFF_NORMED)
    res2 = res.copy()
    x0 = max(0, px - 20)
    y0 = max(0, py - 20)
    x1 = min(res2.shape[1], px + pw + 20)
    y1 = min(res2.shape[0], py + ph + 20)
    res2[y0:y1, x0:x1] = -1.0

    flat = np.argsort(res2.ravel())[::-1]
    best = None
    second = None
    for idx in flat[:200]:
        y, x = np.unravel_index(idx, res2.shape)
        score = float(res2[y, x])
        if abs(y - py) <= 15:
            if best is None:
                best = (int(x), int(y), score)
            elif second is None and abs(x - best[0]) > 10:
                second = (int(x), int(y), score)
                break
    if best is None:
        raise RuntimeError("Khong tim thay peak cung hang y voi manh")
    gap_x, gap_y, score = best
    return {
        "piece_x": px, "piece_y": py, "piece_w": pw, "piece_h": ph,
        "gap_x": gap_x, "gap_y": gap_y,
        "dx": int(gap_x - px), "dy": int(gap_y - py),
        "score": score,
        "second_score": second[2] if second else None,
        "image_w": int(img.shape[1]), "image_h": int(img.shape[0]),
    }


async def _tile_locator(page):
    for sel in (GOCAPTCHA["tile_class"],
                ".gocaptcha-module_body__KJKNu div:has(img[src^='data:image/png'])"):
        loc = page.locator(sel).first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                return loc
        except Exception:  # noqa: BLE001 - thu selector tiep theo
            continue
    return page.locator(GOCAPTCHA["tile_class"]).first


async def _move_eased(page, x0: float, y0: float, from_off: float, to_off: float,
                      delay_scale: float = 0.5) -> float:
    dist = to_off - from_off
    if abs(dist) < 0.5:
        return to_off
    steps = max(8, min(28, int(abs(dist) / 5) + 5))
    for i in range(1, steps + 1):
        t = i / steps
        e = 1 - pow(1 - t, 3)
        await page.mouse.move(x0 + from_off + dist * e,
                              y0 + random.uniform(-1.0, 1.0), steps=1)
        await asyncio.sleep(random.uniform(0.006, 0.014) * delay_scale)
    return to_off


async def is_llwin_slider_visible(page) -> bool:
    """True only if the GoCaptcha wrapper is actually on screen."""
    try:
        loc = page.locator(GOCAPTCHA["wrapper"]).first
        return await loc.count() > 0 and await loc.is_visible()
    except Exception:  # noqa: BLE001
        return False


def _activate_edge_window(marker: str) -> int:
    """Activate the uniquely identified Edge window; never guess by window size."""
    if sys.platform != "win32":
        raise RuntimeError("LLWIN OS mouse mode requires Windows")
    import ctypes
    from ctypes import wintypes
    import psutil

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    deadline = time.monotonic() + 1.5
    matches = []
    while time.monotonic() < deadline:
        matches.clear()

        @callback_type
        def collect(hwnd, _):
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, len(name))
            if name.value != "Chrome_WidgetWin_1":
                return True
            title = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
            user32.GetWindowTextW(hwnd, title, len(title))
            if marker not in title.value:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                if psutil.Process(pid.value).name().lower() == "msedge.exe":
                    matches.append(hwnd)
            except (psutil.Error, OSError):
                pass
            return True

        user32.EnumWindows(collect, 0)
        if matches:
            break
        time.sleep(0.05)
    if len(matches) != 1:
        raise RuntimeError("Could not identify the Edge window for the LLWIN tab")
    hwnd = matches[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    elif not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() != hwnd:
        raise RuntimeError("Windows did not activate the LLWIN Edge window")
    return hwnd


async def _focus_edge_for_os_mouse(page) -> int:
    """Restore and activate the browser window containing this exact page."""
    session = None
    try:
        session = await page.context.new_cdp_session(page)
        window = await session.send("Browser.getWindowForTarget")
        if window.get("bounds", {}).get("windowState") == "minimized":
            await session.send("Browser.setWindowBounds", {
                "windowId": window["windowId"], "bounds": {"windowState": "normal"},
            })
    except Exception as exc:
        logger.debug("Could not restore LLWIN window using CDP: %s", exc)
    finally:
        if session is not None:
            try:
                await session.detach()
            except Exception:
                pass
    await page.bring_to_front()
    marker = f"__llwin_window_{uuid.uuid4().hex}__"
    old_title = await page.evaluate("""token => {
        const old = document.title; document.title = token; return old;
    }""", marker)
    try:
        hwnd = await asyncio.to_thread(_activate_edge_window, marker)
        await page.bring_to_front()
        if not await page.evaluate("() => document.hasFocus() && document.visibilityState === 'visible'"):
            raise RuntimeError("LLWIN tab is not focused in the active Edge window")
        return hwnd
    finally:
        try:
            await page.evaluate("""([token, old]) => {
                if (document.title === token) document.title = old;
            }""", [marker, old_title])
        except Exception as exc:
            logger.debug("Could not restore LLWIN page title: %s", exc)


def _viewport_to_screen(metrics, bounds, x: float, y: float) -> tuple[int, int]:
    """Map CSS viewport pixels to a validated native renderer client rectangle."""
    width, height, dpr = (float(metrics[k]) for k in ("width", "height", "dpr"))
    left, top, native_width, native_height = bounds
    values = (width, height, dpr, left, top, native_width, native_height, x, y)
    if not all(math.isfinite(v) for v in values):
        raise RuntimeError("Invalid OS mouse geometry")
    if min(width, height, dpr, native_width, native_height) <= 0:
        raise RuntimeError("Empty OS mouse viewport")
    if not metrics.get("focused") or not metrics.get("visible"):
        raise RuntimeError("LLWIN page is not visible and focused")
    if not math.isclose(float(metrics.get("visual_scale", 1)), 1.0, abs_tol=0.001):
        raise RuntimeError("Pinch-zoomed viewports are not supported for OS dragging")
    if (abs(native_width - width * dpr) > 3
            or abs(native_height - height * dpr) > 3):
        raise RuntimeError("Native renderer does not match the page DPI/zoom")
    if not (0 <= x < width and 0 <= y < height):
        raise RuntimeError("OS drag extends outside the page viewport")
    # Native/CSS scale already includes Windows DPI and browser page zoom.
    return (round(left + x * native_width / width),
            round(top + y * native_height / height))


@contextmanager
def _windows_mouse_context(metrics):
    """Read physical pixels on the same DPI-aware thread that moves the mouse.

    Chromium's renderer child excludes tabs, toolbar, borders, and side panels.
    If Edge does not expose one unambiguous matching child, do not guess offsets.
    """
    if sys.platform != "win32":
        raise RuntimeError("LLWIN OS mouse mode requires Windows")
    import ctypes
    from ctypes import wintypes
    import psutil

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    previous_dpi = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    if not previous_dpi:
        raise RuntimeError("Could not enable per-monitor DPI awareness for OS mouse")
    try:
        hwnd = user32.GetForegroundWindow()
        if metrics.get("expected_hwnd") and hwnd != metrics["expected_hwnd"]:
            raise RuntimeError("The active Edge window is not the LLWIN tab's window")
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not hwnd or psutil.Process(pid.value).name().lower() != "msedge.exe":
            raise RuntimeError("The foreground window is not Microsoft Edge")
        matches = []

        @callback_type
        def collect(child, _):
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child, name, len(name))
            if (name.value != "Chrome_RenderWidgetHostHWND"
                    or not user32.IsWindowVisible(child)):
                return True
            rect, origin = wintypes.RECT(), wintypes.POINT()
            if (user32.GetClientRect(child, ctypes.byref(rect))
                    and user32.ClientToScreen(child, ctypes.byref(origin))):
                bounds = (origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top)
                try:
                    _viewport_to_screen(metrics, bounds, 0, 0)
                except (RuntimeError, ValueError, KeyError):
                    pass
                else:
                    matches.append((child, bounds))
            return True

        user32.EnumChildWindows(hwnd, collect, 0)
        if len(matches) != 1:
            raise RuntimeError("Could not identify a unique visible Edge viewport")
        renderer, bounds = matches[0]

        def viewport_is_current():
            # Tab switches can keep the same top-level Edge HWND. Also reject
            # a moved/resized viewport after the coordinates were captured.
            rect, origin = wintypes.RECT(), wintypes.POINT()
            return bool(
                user32.IsWindowVisible(renderer)
                and user32.GetClientRect(renderer, ctypes.byref(rect))
                and user32.ClientToScreen(renderer, ctypes.byref(origin))
                and (origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top) == bounds
            )

        yield user32, hwnd, bounds, viewport_is_current
    finally:
        user32.SetThreadDpiAwarenessContext(previous_dpi)


def _perform_os_drag(metrics, handle_box, dx: float, stop_event) -> None:
    """Blocking physical input; only call inside the serialized worker thread."""
    with _windows_mouse_context(metrics) as (user32, hwnd, bounds, viewport_is_current):
        import pyautogui

        cx = handle_box["x"] + handle_box["width"] / 2
        cy = handle_box["y"] + handle_box["height"] / 2
        x, y = _viewport_to_screen(metrics, bounds, cx, cy)
        end_x, end_y = _viewport_to_screen(metrics, bounds, cx + dx, cy)
        # PyAutoGUI supports the primary monitor. Reject rather than clamp.
        if not all(pyautogui.onScreen(px, py) for px, py in
                   ((x, y - 2), (x, y + 2), (end_x, end_y - 2), (end_x, end_y + 2))):
            raise RuntimeError("OS drag must stay on the primary monitor")

        def check_active():
            if stop_event.is_set():
                raise RuntimeError("OS drag cancelled")
            if user32.GetForegroundWindow() != hwnd:
                raise RuntimeError("Edge lost focus during OS drag")
            if not viewport_is_current():
                raise RuntimeError("Edge viewport changed during OS drag")

        check_active()
        logger.warning("EXECUTING OS-LEVEL MOUSE DRAG at (%d, %d)", x, y)
        pyautogui.moveTo(x, y, duration=0.2, _pause=False)
        pressed = False
        try:
            check_active()
            pressed = True  # Even a partially failed mouseDown needs a release.
            pyautogui.mouseDown(_pause=False)
            duration = random.uniform(0.8, 1.2)
            steps = 30
            for step in range(1, steps + 1):
                check_active()
                target_x = round(x + (end_x - x) * step / steps)
                target_y = end_y if step == steps else y + random.randint(-2, 2)
                pyautogui.moveTo(target_x, target_y, _pause=False)
                if stop_event.wait(duration / steps):
                    raise RuntimeError("OS drag cancelled")
        finally:
            if pressed:
                try:
                    pyautogui.mouseUp(_pause=False)
                except Exception:
                    # A corner failsafe can reject mouseUp. Release only; never
                    # disable the process-wide PyAutoGUI failsafe for movement.
                    user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
                    raise


async def _run_os_mouse_drag(page, *, on_os_mouse_required=None, debug_dir=None) -> bool:
    """Try one physical drag; failures leave the slider for the existing watcher."""
    try:
        # All LLWIN tabs share one physical mouse. Include foregrounding and
        # geometry capture in this lock so another tab cannot steal the cursor.
        async with _OS_MOUSE_LOCK:
            expected_hwnd = await _focus_edge_for_os_mouse(page)
            original_metrics = await page.evaluate(_VIEWPORT_METRICS_JS)
            body = page.locator(GOCAPTCHA["body"]).first
            handle = page.locator(GOCAPTCHA["drag_block"]).first
            await handle.wait_for(state="visible", timeout=10000)
            await handle.scroll_into_view_if_needed()
            tile = await _tile_locator(page)
            png = await body.screenshot()
            body_box = await body.bounding_box()
            tile_box = await tile.bounding_box()
            handle_box = await handle.bounding_box()
            if not (body_box and tile_box and handle_box):
                raise RuntimeError("Missing LLWIN slider bounding boxes")
            import cv2
            import numpy as np

            image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None or body_box["width"] <= 0:
                raise RuntimeError("Could not decode LLWIN slider image")
            image_scale = image.shape[1] / body_box["width"]
            piece = (
                round((tile_box["x"] - body_box["x"]) * image_scale),
                round((tile_box["y"] - body_box["y"]) * image_scale),
                round(tile_box["width"] * image_scale),
                round(tile_box["height"] * image_scale),
            )
            detection = await asyncio.to_thread(_find_gap_from_png, png, piece)
            if detection["score"] < 0.20 or detection["dx"] <= 0:
                raise RuntimeError("LLWIN slider gap is not reliable enough for OS drag")
            if debug_dir is not None:
                dbg = Path(debug_dir)
                dbg.mkdir(parents=True, exist_ok=True)
                (dbg / "gocap_os_live.png").write_bytes(png)
            if on_os_mouse_required is not None:
                await on_os_mouse_required()
            else:
                logger.warning(OS_MOUSE_WARNING)
            metrics = await page.evaluate(_VIEWPORT_METRICS_JS)
            metrics["expected_hwnd"] = expected_hwnd
            # Telegram/detection can yield for seconds. Never reuse a handle or
            # gap measured before a resize, zoom, or page layout change.
            if any(not math.isclose(float(original_metrics[key]), float(metrics[key]),
                                    abs_tol=0.001)
                   for key in ("width", "height", "dpr", "visual_scale")):
                raise RuntimeError("Edge viewport changed while preparing OS drag")
            for locator, original in ((body, body_box), (tile, tile_box), (handle, handle_box)):
                current = await locator.bounding_box()
                if not current or any(abs(current[key] - original[key]) > 0.5
                                      for key in ("x", "y", "width", "height")):
                    raise RuntimeError("LLWIN slider moved while preparing OS drag")
            stop_event = threading.Event()
            worker = asyncio.create_task(asyncio.to_thread(
                _perform_os_drag, metrics, handle_box,
                detection["dx"] / image_scale, stop_event,
            ))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling to_thread does not stop native input. Keep the lock
                # until the worker has released the button, even if cancelled again.
                stop_event.set()
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    try:
                        worker.result()
                    except Exception:
                        pass
                raise
            wrapper = page.locator(GOCAPTCHA["wrapper"]).first
            for _ in range(15):
                if not await wrapper.is_visible():
                    return True
                await asyncio.sleep(0.1)
            logger.warning("LLWIN OS mouse drag did not pass; falling back to PENDING_VERIFICATION")
    except Exception as exc:
        logger.warning("LLWIN OS mouse drag failed; falling back to PENDING_VERIFICATION: %s", exc)
    return False


async def solve_llwin_slider(page, *, max_attempts: int = 2,
                             debug_dir: str | Path | None = None,
                             on_manual_required=None, on_os_mouse_required=None) -> bool:
    """Solve one open GoCaptcha slider on an async Playwright page.

    Returns True when the wrapper disappears after a drag (the
    solver's success signal, same as the standalone version). In manual
    mode, wait for the user to solve the challenge without mouse automation.
    Raises on missing boxes / unreadable images so the caller can
    log the failure instead of silently treating it as a code result.
    OS mouse mode tries Playwright on failure; if both fail, the caller can
    retain the page in the PENDING_VERIFICATION watcher.
    """
    from config import Config

    if bool(getattr(Config, "LLWIN_OS_MOUSE_MODE", False)):
        try:
            if await _run_os_mouse_drag(
                page, debug_dir=debug_dir, on_os_mouse_required=on_os_mouse_required,
            ):
                return True
        except Exception as exc:
            logger.warning("LLWIN OS mouse drag failed: %s", exc)
        logger.warning("LLWIN OS mouse failed; trying the Playwright mouse")
        try:
            return await _solve_llwin_slider_playwright(page, max_attempts=max_attempts, debug_dir=debug_dir)
        except Exception as exc:
            logger.warning("LLWIN Playwright mouse failed; falling back to PENDING_VERIFICATION: %s", exc)
            return False

    if bool(getattr(Config, "LLWIN_MANUAL_SLIDER_MODE", True)):
        await page.bring_to_front()
        if on_manual_required is not None:
            await on_manual_required()
        print("WAITING FOR MANUAL SLIDER...", flush=True)
        deadline = asyncio.get_running_loop().time() + 60.0
        while asyncio.get_running_loop().time() < deadline:
            if not await is_llwin_slider_visible(page):
                return True
            await asyncio.sleep(0.5)
        return False

    return await _solve_llwin_slider_playwright(page, max_attempts=max_attempts, debug_dir=debug_dir)


async def _solve_llwin_slider_playwright(page, *, max_attempts: int = 2,
                                        debug_dir: str | Path | None = None) -> bool:
    """Use Playwright's page mouse and the existing gap correction."""
    ratio = 1.0
    dbg = Path(debug_dir) if debug_dir else None
    if dbg is not None:
        dbg.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, max_attempts + 1):
        await page.wait_for_selector(GOCAPTCHA["wrapper"], timeout=10000)
        await page.wait_for_selector(GOCAPTCHA["body"], timeout=10000)
        body_loc = page.locator(GOCAPTCHA["body"])
        tile_loc = await _tile_locator(page)
        drag_loc = page.locator(GOCAPTCHA["drag_block"])
        body_box = await body_loc.bounding_box()
        tile_box = await tile_loc.bounding_box()
        drag_box = await drag_loc.bounding_box()
        if not (body_box and tile_box and drag_box):
            raise RuntimeError("Khong lay duoc bounding_box GoCaptcha "
                               "(wrapper/body/tile/drag_block sai?)")
        await page.wait_for_timeout(120)
        tile_box = await tile_loc.bounding_box() or tile_box
        png_bytes = await body_loc.screenshot()
        if dbg is not None:
            (dbg / f"gocap_live_{attempt}.png").write_bytes(png_bytes)
        import cv2 as _cv2
        import numpy as _np
        _img = _cv2.imdecode(_np.frombuffer(png_bytes, dtype=_np.uint8),
                             _cv2.IMREAD_COLOR)
        dpr = (_img.shape[1] / body_box["width"]) if body_box["width"] else 1.0
        piece_rel = (
            int((tile_box["x"] - body_box["x"]) * dpr),
            int((tile_box["y"] - body_box["y"]) * dpr),
            int(tile_box["width"] * dpr),
            int(tile_box["height"] * dpr),
        )
        det = await asyncio.to_thread(_find_gap_from_png, png_bytes, piece_rel)
        if dbg is not None:
            import cv2 as _cv2w
            vis = _cv2w.imdecode(_np.frombuffer(png_bytes, dtype=_np.uint8),
                                 _cv2w.IMREAD_COLOR)
            _cv2w.rectangle(vis, (det["piece_x"], det["piece_y"]),
                            (det["piece_x"] + det["piece_w"],
                             det["piece_y"] + det["piece_h"]), (0, 0, 255), 2)
            _cv2w.rectangle(vis, (det["gap_x"], det["gap_y"]),
                            (det["gap_x"] + det["piece_w"],
                             det["gap_y"] + det["piece_h"]), (0, 255, 0), 2)
            _cv2w.imwrite(str(dbg / f"gocap_live_{attempt}_debug.png"), vis)
        if det["score"] < 0.20:
            if attempt < max_attempts:
                await page.locator(GOCAPTCHA["icon_block"] + " svg").nth(1).click()
                await page.wait_for_timeout(500)
            continue
        target_tile_x = body_box["x"] + det["gap_x"] / dpr
        dx_vp = det["dx"] / dpr
        cx = drag_box["x"] + drag_box["width"] / 2
        cy = drag_box["y"] + drag_box["height"] / 2
        tile0_x = tile_box["x"]
        await page.mouse.move(cx, cy, steps=3)
        delay_scale = 0.5 if attempt == 1 else 1.0
        pressed = False
        try:
            pressed = True  # Release even if the down acknowledgement is lost.
            await page.mouse.down()
            await asyncio.sleep(0.05)
            cur = await _move_eased(page, cx, cy, 0, dx_vp / ratio, delay_scale)
            await page.wait_for_timeout(90)
            tile_cur = await tile_loc.bounding_box()
            if tile_cur:
                tile_delta = tile_cur["x"] - tile0_x
                if abs(cur) > 5 and abs(tile_delta) > 1:
                    ratio = tile_delta / cur
                for _ in range(2):
                    tile_cur = await tile_loc.bounding_box()
                    if not tile_cur:
                        break
                    residual = target_tile_x - tile_cur["x"]
                    if abs(residual) <= 2:
                        break
                    cur = await _move_eased(page, cx, cy, cur,
                                            cur + residual / ratio, delay_scale)
                    await page.wait_for_timeout(90)
            cur = await _move_eased(page, cx, cy, cur, cur + 3, delay_scale)
            await page.wait_for_timeout(30)
            cur = await _move_eased(page, cx, cy, cur, cur - 3, delay_scale)
        finally:
            if pressed:
                await page.mouse.up()
        still = True
        for _ in range(10):
            await page.wait_for_timeout(150)
            try:
                still = await page.locator(GOCAPTCHA["wrapper"]).is_visible()
            except Exception:  # noqa: BLE001
                still = False
            if not still:
                break
        if not still:
            return True
        if attempt < max_attempts:
            try:
                await page.locator(GOCAPTCHA["icon_block"] + " svg").nth(1).click()
                await page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001
                pass
    return False
