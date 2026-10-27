"""Launch Microsoft Edge CDP; retain legacy API names for the bot's callers."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
import json
import logging
import os
from pathlib import Path
import subprocess
import socket
import time
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener

from config import Config

logger = logging.getLogger("autobot." + __name__)
BASE_DIR = Path(__file__).resolve().parent
CHROME_PROCESSES: dict[int, subprocess.Popen] = {}


def _chrome_path():
    explicit = str(getattr(Config, "EDGE_PATH", "") or "").strip().strip('"')
    if explicit:
        path = Path(os.path.expandvars(explicit)).expanduser()
        if not path.is_file():
            raise FileNotFoundError("Config.EDGE_PATH does not point to an executable")
        if path.name.lower() != "msedge.exe":
            raise ValueError("Config.EDGE_PATH must point to Microsoft Edge (msedge.exe)")
        return str(path.resolve())
    candidates = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                  Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if os.getenv(key):
            candidates.append(Path(os.environ[key]) / "Microsoft/Edge/Application/msedge.exe")
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    raise FileNotFoundError("Microsoft Edge not found; set EDGE_PATH")


def profile_for_port(port: int) -> str:
    attr = "CHROME_PROFILE_1" if port == Config.CHROME_CDP_PORT else "CHROME_PROFILE_2"
    configured = str(getattr(Config, attr, "") or f"edge_bot_profile_{port}")
    path = Path(os.path.expandvars(configured)).expanduser()
    return str((path if path.is_absolute() else BASE_DIR / path).resolve())


def launch_chrome(port: int, profile_dir: str, extra_args: list = None) -> subprocess.Popen:
    """Open Microsoft Edge with the specified CDP port and dedicated profile."""
    if not 1 <= int(port) <= 65535:
        raise ValueError("Invalid CDP port")
    if _port_in_use(port):
        raise RuntimeError(f"CDP {port} is occupied; close the old bot browser before starting Edge")
    executable = _chrome_path()
    profile = Path(profile_dir).expanduser().resolve()
    if any(part.lower().startswith("chrome_bot_profile_") for part in profile.parts):
        raise ValueError("Edge requires its own profile; Chrome profile is preserved")
    local_data = os.getenv("LOCALAPPDATA")
    if local_data:
        personal = (Path(local_data) / "Microsoft/Edge/User Data").resolve()
        if profile == personal or personal in profile.parents:
            raise ValueError("Use a dedicated Edge bot profile, not the personal Edge User Data directory")
    if profile == BASE_DIR or profile == Path(profile.anchor):
        raise ValueError("Use a dedicated Edge bot profile directory")
    profile.mkdir(parents=True, exist_ok=True)
    args = [executable, f"--remote-debugging-port={int(port)}",
            "--remote-debugging-address=127.0.0.1", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--disable-popup-blocking",
            "--disable-notifications", "--disable-background-timer-throttling",
            # PATCH_MARKER_2026-10-26-v9-core-ocr-sqlite: keep background bot tabs rendering.
            "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
            # Giam viec nen/am thanh cua chinh Edge; khong dung DOM, cookie hay captcha.
            "--mute-audio", "--autoplay-policy=user-gesture-required",
            "--disable-background-networking", "--disable-component-update",
            "--disable-sync", "--disable-default-apps",
            "--disable-features=Translate,OptimizationHints,MediaRouter,CalculateNativeWinOcclusion"]
    for arg in extra_args or []:
        if str(arg).startswith(("--remote-debugging-", "--user-data-dir")):
            raise ValueError("extra_args cannot override CDP/profile ownership")
        args.append(str(arg))
    # A new process group keeps console Ctrl+C under the bot's shutdown control.
    return subprocess.Popen(args, shell=False, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def _port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=max(0.01, timeout)):
            return True
    except OSError:
        return False


def assert_edge_endpoint(port: int, host: str = "127.0.0.1", timeout: float = 3.0) -> None:
    """Reject another browser before Playwright can attach to its CDP session."""
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://{host}:{int(port)}/json/version",
                     timeout=max(0.01, timeout)) as response:
        data = json.loads(response.read(65536))
        if not (response.status == 200 and isinstance(data, dict)
                and str(data.get("webSocketDebuggerUrl", "")).startswith(("ws://", "wss://"))):
            raise RuntimeError(f"CDP {port} is not a valid browser endpoint")
        identity = str(data.get("Browser", "")) + " " + str(data.get("User-Agent", ""))
        if not any(marker in identity for marker in ("Edg/", "Edge/", "MicrosoftEdge/")):
            raise RuntimeError(f"CDP {port} belongs to another browser, not Microsoft Edge; "
                               "stop the old bot first or select unused EDGE_CDP_PORT/EDGE2_CDP_PORT")


def is_chrome_alive(port: int, timeout: float = 3.0) -> bool:
    """Compatibility API: True only for a live Microsoft Edge CDP endpoint."""
    try:
        assert_edge_endpoint(port, timeout=timeout)
        return True
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def wait_for_chrome_ready(port: int, timeout: float = 30.0) -> bool:
    """Poll CDP every 0.5 seconds without exceeding the readiness deadline."""
    deadline = time.monotonic() + max(0.0, timeout)
    while (remaining := deadline - time.monotonic()) > 0:
        if is_chrome_alive(port, timeout=min(3.0, remaining)):
            return True
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    return False


def _request_close(process):
    if os.name != "nt":
        process.terminate()
        return
    # WM_CLOSE gives Edge a chance to persist profile/session data on Windows.
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    def close_window(hwnd, unused):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process.pid:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)
        return True
    user32.EnumWindows(callback_type(close_window), 0)


def kill_chrome(process: subprocess.Popen) -> None:
    """Request graceful close, wait five seconds, then kill only this process."""
    if process.poll() is not None:
        return
    try:
        _request_close(process)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def _stop_owned(port):
    process = CHROME_PROCESSES.get(port)
    if process is not None:
        await asyncio.to_thread(kill_chrome, process)
        CHROME_PROCESSES.pop(port, None)


async def ensure_chromes_ready(domains: Iterable[str] | None = None):
    """Prepare only selected sites' endpoints; None preserves both-browser startup."""
    if not getattr(Config, "CHROME_AUTO_LAUNCH", True):
        return
    secondary_domains = {
        str(item).strip().lower().removeprefix("www.")
        for item in str(getattr(Config, "CHROME2_DOMAINS", "") or "").split(",")
        if str(item).strip()
    }
    selected = None if domains is None else {
        str(item).strip().lower().removeprefix("www.") for item in domains if str(item).strip()
    }
    use_secondary = bool(Config.CHROME2_ENABLED)
    needs_primary = selected is None or any(
        not use_secondary or domain not in secondary_domains for domain in selected
    )
    needs_secondary = use_secondary and (
        selected is None or bool(selected & secondary_domains)
    )
    endpoints = []
    if needs_primary:
        endpoints.append((Config.CHROME_CDP_HOST, Config.CHROME_CDP_PORT))
    if needs_secondary:
        endpoints.append((Config.CHROME2_CDP_HOST, Config.CHROME2_CDP_PORT))
    if len({port for _, port in endpoints}) != len(endpoints):
        raise RuntimeError("Edge CDP ports must be distinct")
    # Validate every occupied endpoint before opening either browser. This avoids
    # opening Edge 1 and only then discovering Chrome still owns Edge 2's port.
    for host, port in endpoints:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("EDGE_AUTO_LAUNCH requires local CDP hosts")
        if await asyncio.to_thread(_port_in_use, port, host):
            await asyncio.to_thread(assert_edge_endpoint, port, host)
    for host, port in endpoints:
        if await asyncio.to_thread(is_chrome_alive, port):
            logger.info("[Edge-Launcher] Reusing Edge CDP %s (not owned by bot)", port)
            continue
        CHROME_PROCESSES[port] = launch_chrome(port, profile_for_port(port))
        if not await asyncio.to_thread(wait_for_chrome_ready, port, Config.CHROME_LAUNCH_TIMEOUT):
            logger.error("[Edge-Launcher] Edge CDP %s not ready after %ss", port, Config.CHROME_LAUNCH_TIMEOUT)
            raise RuntimeError(f"Edge CDP {port} startup failed")
        logger.info("[Edge-Launcher] Edge CDP %s ready", port)


async def send_chrome_alert(port: int) -> bool:
    token = str(getattr(Config, "ALERT_BOT_TOKEN", "") or "").strip()
    chat = getattr(Config, "ALERT_CHAT_ID", 0) or getattr(Config, "TELEGRAM_ADMIN_ID", 0)
    if not token or not chat:
        logger.critical("[Edge-Alert] Missing ALERT_BOT_TOKEN or admin chat ID")
        return False
    body = urlencode({"chat_id": str(chat), "text": f"AutoBot: Edge CDP {port} failed to recover after 3 attempts."}).encode()
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST")
    def post():
        with build_opener().open(request, timeout=max(1.0, Config.ALERT_HTTP_TIMEOUT)) as response:
            data = json.loads(response.read())
            return 200 <= response.status < 300 and data.get("ok") is True
    try:
        success = await asyncio.to_thread(post)
        if not success:
            logger.error("[Edge-Alert] Telegram rejected the alert")
        return success
    except Exception as exc:
        # Do not log exception URLs: they can contain the bot token.
        logger.error("[Edge-Alert] Alert failed (%s)", type(exc).__name__)
        return False


async def restart_chrome(port: int, profile_dir: str, on_ready) -> bool:
    """Three bounded attempts; never terminate an unowned browser process."""
    for attempt, backoff in enumerate((5.0, 15.0, 30.0), 1):
        await asyncio.sleep(backoff)
        try:
            # A user may have restored Edge while the backoff was pending.
            if not await asyncio.to_thread(is_chrome_alive, port):
                await _stop_owned(port)
                if await asyncio.to_thread(_port_in_use, port):
                    await asyncio.to_thread(assert_edge_endpoint, port)
                CHROME_PROCESSES[port] = launch_chrome(port, profile_dir)
                if not await asyncio.to_thread(wait_for_chrome_ready, port, Config.CHROME_LAUNCH_TIMEOUT):
                    raise RuntimeError("CDP readiness timeout")
            await on_ready()
            logger.info("[Browser-Watchdog] Edge CDP %s recovered on attempt %s", port, attempt)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[Browser-Watchdog] Edge CDP %s recovery %s/3: %s", port, attempt, exc)
    # The final failed launch must not be orphaned; retain ownership if close fails.
    try:
        await _stop_owned(port)
    except Exception as exc:
        logger.warning("[Edge-Launcher] Could not stop failed owned process (%s)", type(exc).__name__)
    logger.critical("[Browser-Watchdog] Edge CDP %s recovery exhausted after 3 attempts", port)
    await send_chrome_alert(port)
    return False


async def shutdown_chromes():
    if getattr(Config, "CHROME_KILL_ON_SHUTDOWN", True):
        for port in list(CHROME_PROCESSES):
            try:
                await _stop_owned(port)
            except Exception as exc:
                logger.error("[Edge-Launcher] Closing owned Edge %s failed (%s)", port, type(exc).__name__)
