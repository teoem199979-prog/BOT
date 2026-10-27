import logging
import os
import sys
import threading
from collections import deque
from typing import Optional
from code_identity import normalize_code

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text


REFRESH_INTERVAL = 1.0 / 2
_dirty = False
_flush_thread: Optional[threading.Thread] = None
_flush_stop = threading.Event()

_lock = threading.Lock()

_rows: deque = deque(maxlen=100)
_stats = {
    "success": 0,
    "failed": 0,
    "total": 0,
    "last_telegram_rtt_ms": None,
    "last_concurrent": 0,
    "last_concurrent_elapsed_ms": None,
    "server_rtts_ms": deque(maxlen=200),
    "timing_records": 0,
    "download_active": 0,
    "download_completed": 0,
    "download_dedup": 0,
    "ocr_scans": 0,
    "ocr_codes": 0,
    "ocr_no_code": 0,
    "ocr_errors": 0,
    "last_download_ms": None,
    "last_download_mb_s": None,
    "last_download_size_mb": None,
    "last_download_attempts": None,
}
_live: Optional[Live] = None
_console: Optional[Console] = None
_row_id = 0

def disable_console_logging():

    logger = logging.getLogger("autobot")
    for h in list(logger.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.FileHandler
        ):
            logger.removeHandler(h)


def _fmt_ms(ms) -> str:

    if ms is None:
        return "-"
    try:
        return f"{float(ms):.0f}ms"
    except Exception:
        return "-"

def _format_latency(latency: Optional[dict]) -> str:

    if not latency:
        return "-"

    def _num(key):
        try:
            v = latency.get(key)
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    parts = []
    tab = _num("tab_wait_ms")
    if tab is not None and tab >= 100:
        parts.append(f"tab{tab:.0f}")
    gap = _num("pre_api_ms")
    if gap is not None and gap >= 100:
        parts.append(f"gap{gap:.0f}")
    api = _num("api_ms")
    if api is not None:
        parts.append(f"api{api:.0f}")
    else:
        res = _num("result_wait_ms")
        if res is not None:
            parts.append(f"res{res:.0f}")
    tot = _num("total_submit_ms")
    if tot is not None:
        parts.append(f"tot{tot:.0f}")
    return " ".join(parts) or "-"

def _status_style(status: str):

    upper = (status or "").upper()
    if "THÀNH CÔNG" in upper or "SUCCESS" in upper:
        return "bold green", f"✓ {status}"
    if any(k in upper for k in ("THẤT BẠI", "FAILED", "SAI", "HẾT HẠN", "LỖI")):
        return "bold red", f"✗ {status}"
    return "bold yellow", f"? {status}"

def _build_renderable():

    with _lock:
        stats = dict(_stats)
        stats["server_rtts_ms"] = list(_stats["server_rtts_ms"])
        rows_snapshot = list(_rows)

    server_rtts = stats["server_rtts_ms"]
    avg_server_rtt = (sum(server_rtts) / len(server_rtts)) if server_rtts else None

    header1 = Text()
    header1.append("⚡ RTT Telegram: ", style="bold")
    header1.append(_fmt_ms(stats["last_telegram_rtt_ms"]), style="cyan")
    header1.append("   |   ", style="dim")
    header1.append("🚀 Bắn ", style="bold")
    header1.append(f"{stats['last_concurrent']}", style="bold yellow")
    header1.append(" nick đồng thời: ", style="bold")
    header1.append(_fmt_ms(stats["last_concurrent_elapsed_ms"]), style="cyan")
    header1.append("   |   ", style="dim")
    header1.append("📡 RTT Server TB: ", style="bold")
    header1.append(_fmt_ms(avg_server_rtt), style="magenta")

    header2 = Text()
    header2.append("📊 Thống kê: ", style="bold")
    header2.append(f"{stats['success']} Thành công", style="bold green")
    header2.append(" / ", style="dim")
    header2.append(f"{stats['failed']} Thất bại", style="bold red")
    header2.append(f"   (Tổng {stats['total']} lượt submit)", style="dim")

    header3 = Text()
    header3.append("⬇ Download: ", style="bold")
    header3.append(f"{stats['download_completed']} xong", style="green")
    header3.append(f" / {stats['download_dedup']} dedup", style="yellow")
    if stats["last_download_mb_s"] is not None:
        header3.append(
            f" | gần nhất {stats['last_download_mb_s']:.2f} MB/s, "
            f"{stats['last_download_size_mb']:.2f} MB, {stats['last_download_ms']:.0f}ms",
            style="cyan",
        )
    header3.append(f" | đang tải={stats['download_active']}", style="bold yellow")
    header3.append(f" | timing={stats['timing_records']}", style="dim")
    header3.append(
        f" | OCR={stats['ocr_scans']} lượt/{stats['ocr_codes']} mã/"
        f"{stats['ocr_no_code']} không mã/{stats['ocr_errors']} lỗi", style="cyan",
    )

    header4 = None
    try:
        import manual_confirm

        mc = manual_confirm.get_stats()
        if mc["pending"] or mc["confirmed"] or mc["skipped"] or mc["timed_out"]:
            header4 = Text()
            header4.append("⌨ Xác nhận tay: ", style="bold")
            header4.append(f"chờ {mc['pending']}", style="bold yellow")
            header4.append(f" | đã gõ {mc['confirmed']}", style="green")
            header4.append(f" | bỏ qua {mc['skipped']}", style="dim")
            header4.append(f" | hết giờ {mc['timed_out']}", style="red")
            if mc["last_answer_ms"] is not None:
                header4.append(f" | lần gần nhất {mc['last_answer_ms'] / 1000.0:.1f}s", style="cyan")
    except Exception:
        header4 = None

    table = Table(
        show_header=True,
        header_style="bold white on blue",
        expand=True,
        box=box.ROUNDED,
        pad_edge=False,
    )

    table.add_column("#", justify="right", style="dim", width=4, no_wrap=True)

    table.add_column("Trang", style="cyan", width=14, no_wrap=True)

    table.add_column("Tài Khoản", style="bright_cyan", width=12, no_wrap=True)

    table.add_column("Giftcode", style="magenta bold", width=12, no_wrap=True)

    table.add_column("Trạng Thái", justify="center", width=14, no_wrap=True)

    table.add_column("RTT API", justify="right", width=8, style="yellow", no_wrap=True)

    term_width = _console.size.width if _console else 120
    show_latency = term_width >= 155
    if show_latency:
        table.add_column("Latency phases", style="dim", width=32, no_wrap=True, overflow="ellipsis")

    table.add_column("Phản Hồi RAW", style="white", min_width=25, ratio=1, no_wrap=True, overflow="ellipsis")

    term_height = _console.size.height if _console else 30

    max_visible_rows = max(5, term_height - 9 - (1 if header4 is not None else 0))
    rows_to_render = rows_snapshot[-max_visible_rows:]

    for row in rows_to_render:
        style, display = _status_style(row["status"])
        cells = [
            str(row["id"]),
            row["domain"],
            row["account"],
            row["code"],
            f"[{style}]{display}[/{style}]",
            row["rtt"],
        ]
        if show_latency:
            cells.append(row.get("latency", "-"))
        cells.append(row["raw"])
        table.add_row(*cells)

    header_panel = Panel(
        Group(header1, header2, header3, *([header4] if header4 is not None else [])),
        border_style="cyan",
        title="🎯 OCR Hunter",
        title_align="center",
        expand=True,
    )
    return Group(header_panel, table)

def _flush_loop():

    global _dirty
    while not _flush_stop.wait(REFRESH_INTERVAL):
        if _live is None:
            continue
        with _lock:
            should_flush = _dirty
            _dirty = False
        if not should_flush:
            continue
        try:
            _live.update(_build_renderable(), refresh=True)
        except Exception:
            pass

def start_dashboard():

    global _live, _console, _flush_thread
    if _live is not None:
        return
    if os.name == "nt":
        from logger_setup import _enable_windows_ansi
        _enable_windows_ansi()
    disable_console_logging()
    interactive = bool(sys.stdout.isatty())
    _console = Console(
        force_terminal=True if interactive else None,
        force_interactive=True if interactive else None,
        legacy_windows=False if os.name == "nt" and interactive else None,
    )
    _live = Live(
        _build_renderable(), console=_console, auto_refresh=False,
        screen=True, vertical_overflow="crop", refresh_per_second=2
    )
    _live.start(refresh=True)

    _flush_stop.clear()
    _flush_thread = threading.Thread(target=_flush_loop, name="dashboard-flush", daemon=True)
    _flush_thread.start()


def stop_dashboard():

    global _live, _flush_thread
    _flush_stop.set()
    if _flush_thread is not None:
        try:
            _flush_thread.join(timeout=1.0)
        except Exception:
            pass
        _flush_thread = None
    if _live:
        try:
            _live.update(_build_renderable(), refresh=True)
            _live.stop()
        except Exception:
            pass
        _live = None


def _refresh():

    global _dirty
    if _live is None:
        return
    with _lock:
        _dirty = True

def update_dashboard(
    domain: str,
    account: str,
    code: str,
    status: str,
    rtt_ms: Optional[float] = None,
    raw_response: str = "",
    telegram_rtt_ms: Optional[float] = None,
    latency_ms: Optional[dict] = None,
):

    global _row_id
    code = normalize_code(code, domain)
    # Counters stay live even when the Rich Live screen is disabled
    # (DASHBOARD_ENABLED=false): they are O(1)-ish bookkeeping used by the
    # /status admin command, while all rendering stays gated on _live.

    clean_raw = " ".join((raw_response or "").replace("\r", " ").replace("\n", " ").split())

    for noise in ("No popup Close", "popup Close", "Close"):
        if len(clean_raw) > len(noise) and noise in clean_raw:
            clean_raw = clean_raw.replace(noise, "").strip()

    raw = " ".join(clean_raw.split()) if clean_raw else "-"
    if len(raw) > 160:
        raw = raw[:160] + "..."

    def _bucket(value: str) -> str:
        upper = (value or "").upper()
        if "THÀNH CÔNG" in upper or "SUCCESS" in upper:
            return "success"
        if "THẤT BẠI" in upper or "FAILED" in upper:
            return "failed"
        return "other"

    with _lock:
        existing = next(
            (
                row for row in _rows
                if row["domain"] == domain
                and row["account"] == account
                and row["code"] == code
            ),
            None,
        )
        if existing is None:
            _row_id += 1
            _rows.append(
                {
                    "id": _row_id,
                    "domain": domain,
                    "account": account,
                    "code": code,
                    "status": status,
                    "rtt": _fmt_ms(rtt_ms),
                    "raw": raw,
                    "latency": _format_latency(latency_ms),
                }
            )
            _stats["total"] += 1
            old_bucket = "other"
        else:
            old_bucket = _bucket(existing.get("status", ""))
            existing.update(
                status=status,
                rtt=_fmt_ms(rtt_ms),
                raw=raw,
                latency=_format_latency(latency_ms),
            )

        new_bucket = _bucket(status)
        if old_bucket in ("success", "failed"):
            _stats[old_bucket] = max(0, _stats[old_bucket] - 1)
        if new_bucket in ("success", "failed"):
            _stats[new_bucket] += 1

        # server_rtts_ms chỉ nhận api_ms thật (xem update_latency), không nhận
        # tổng thời gian submit vì nó gồm cả thời gian chờ hết timeout + cleanup.
        if telegram_rtt_ms is not None:
            _stats["last_telegram_rtt_ms"] = telegram_rtt_ms

    _refresh()

def update_latency(domain: str, account: str, code: str, latency_ms: Optional[dict]) -> None:
    code = normalize_code(code, domain)
    text = _format_latency(latency_ms)
    api_ms = None
    wait_ms = None
    try:
        if latency_ms and latency_ms.get("api_ms") is not None:
            api_ms = float(latency_ms["api_ms"])
        elif latency_ms and latency_ms.get("result_wait_ms") is not None:
            wait_ms = float(latency_ms["result_wait_ms"])
    except (TypeError, ValueError):
        api_ms = wait_ms = None
    with _lock:
        for row in reversed(_rows):
            if row["domain"] == domain and row["account"] == account and row["code"] == code:
                row["latency"] = text
                # RTT API thật = thời gian từ request tới phản hồi của website.
                # Nếu site chưa trả lời trong thời gian chờ thì hiện ">Nms".
                if api_ms is not None:
                    row["rtt"] = _fmt_ms(api_ms)
                elif wait_ms is not None:
                    row["rtt"] = ">" + _fmt_ms(wait_ms)
                break
        if api_ms is not None:
            _stats["server_rtts_ms"].append(api_ms)
    _refresh()


def report_ocr_result(result: dict):
    with _lock:
        _stats["ocr_scans"] += 1
        if result.get("success") and result.get("codes"):
            _stats["ocr_codes"] += len(result["codes"])
        elif result.get("retryable", False):
            _stats["ocr_errors"] += 1
        else:
            _stats["ocr_no_code"] += 1
    _refresh()


def report_download_started():
    with _lock:
        _stats["download_active"] += 1
    _refresh()


def report_download_finished():
    with _lock:
        _stats["download_active"] = max(0, _stats["download_active"] - 1)
    _refresh()


def report_timing_record(record: dict):

    with _lock:
        _stats["timing_records"] += 1
        if record.get("media"):
            _stats["download_completed"] += 1
            if record.get("download_dedup_hit"):
                _stats["download_dedup"] += 1
            elapsed = record.get("download_elapsed_ms")
            size = record.get("file_size_bytes")
            speed = record.get("download_bytes_per_sec")
            _stats["last_download_ms"] = float(elapsed) if elapsed is not None else None
            _stats["last_download_mb_s"] = float(speed) / 1_000_000 if speed is not None else None
            _stats["last_download_size_mb"] = float(size) / 1_000_000 if size is not None else None
            _stats["last_download_attempts"] = record.get("download_attempts")
    _refresh()


def get_dashboard_snapshot() -> dict:

    with _lock:
        return {
            "success": _stats["success"],
            "failed": _stats["failed"],
            "total": _stats["total"],
            "download_active": _stats["download_active"],
            "download_completed": _stats["download_completed"],
            "rows_tracked": len(_rows),
        }
