"""Luồng bán tự động cho ảnh code QQ88.

Bot tải ảnh, tự tìm và cắt vùng chứa code, phóng to rồi hiện một cửa sổ nhỏ.
Người dùng nhìn ảnh, gõ code và nhấn Enter. Code gõ tay được đưa vào đúng
đường submit hiện có, nên các bước dedup, hàng đợi và dashboard giữ nguyên.

Bật bằng biến môi trường:  QQ88_MANUAL_CONFIRM=1
Chỉnh thời gian chờ:       QQ88_MANUAL_TIMEOUT_S=25

Cửa sổ chạy trong một thread riêng (Tk chỉ an toàn khi dùng trong một thread),
không chặn event loop của bot và không đụng vào console của dashboard.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import queue
import re
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from logger_setup import logger

try:
    import cv2

    _CV2 = True
except ImportError:  # pragma: no cover
    cv2 = None
    _CV2 = False

_TAG = "⌨️ [QQ88-MANUAL]"
_CODE_RE = re.compile(r"^[A-Za-z0-9]+$")


# --------------------------------------------------------------------------
# Thống kê cho dashboard
# --------------------------------------------------------------------------
_stats_lock = threading.Lock()
_stats = {
    "pending": 0,
    "confirmed": 0,
    "skipped": 0,
    "timed_out": 0,
    "last_answer_ms": None,
}


def get_stats() -> dict:
    with _stats_lock:
        return dict(_stats)


def _bump(**delta) -> None:
    with _stats_lock:
        for key, value in delta.items():
            if key == "last_answer_ms":
                _stats[key] = value
            else:
                _stats[key] = max(0, _stats[key] + value)
    try:  # dashboard là tuỳ chọn, lỗi import không được làm hỏng luồng chính
        import dashboard

        dashboard._refresh()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Tìm và cắt vùng chứa code (chỉ để người xem, không đọc chữ)
# --------------------------------------------------------------------------
def locate_code_region(pil_img: Image.Image):
    """Trả về (x0, y0, x1, y1) của dòng chữ đậm nằm trên nền đỏ, hoặc None.

    Chỉ dùng màu sắc để biết nên cắt ở đâu cho người xem; không nhận dạng ký tự.
    """
    if not _CV2:
        return None
    rgb = pil_img.convert("RGB")
    W0, H0 = rgb.size
    scale = min(1.0, 800.0 / max(W0, H0))
    small = rgb.resize((max(1, int(W0 * scale)), max(1, int(H0 * scale)))) if scale < 1.0 else rgb
    bgr = cv2.cvtColor(np.array(small), cv2.COLOR_RGB2BGR)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    red = (((h <= 12) | (h >= 165)) & (s >= 100) & (v >= 100)).astype(np.uint8) * 255
    near_red = cv2.dilate(red, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    dark = ((v < 90) & (near_red > 0)).astype(np.uint8) * 255
    link = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (31, 3)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(link, 8)
    H, W = link.shape
    best = None
    for i in range(1, n):
        x, y, w, hh, area = (int(t) for t in stats[i])
        if w < W * 0.15 or hh < 10:
            continue
        ratio = w / float(hh)
        if ratio < 4.0:
            continue
        if best is None or ratio * area > best[0]:
            best = (ratio * area, (x, y, x + w, y + hh))
    if best is None:
        return None
    x0, y0, x1, y1 = best[1]
    inv = 1.0 / scale
    return (int(x0 * inv), int(y0 * inv), int(x1 * inv), int(y1 * inv))


def prepare_preview(image_path: str, out_dir: str | None = None) -> str:
    """Tạo ảnh PNG đã cắt + phóng to cho người đọc. Nếu không tìm được vùng code thì dùng cả ảnh."""
    out_dir = out_dir or tempfile.gettempdir()
    out = Path(out_dir) / f"qq88_preview_{int(time.time() * 1000)}.png"
    with Image.open(image_path) as src:
        img = src.convert("RGB")
    try:
        box = locate_code_region(img)
        if box:
            x0, y0, x1, y1 = box
            pad_x = max(30, int((x1 - x0) * 0.25))
            pad_y = max(24, int((y1 - y0) * 1.2))
            crop = img.crop((max(0, x0 - pad_x), max(0, y0 - pad_y),
                             min(img.width, x1 + pad_x), min(img.height, y1 + pad_y)))
            factor = min(6, max(2, math.ceil(820 / max(1, crop.width))))
            crop = crop.resize((crop.width * factor, crop.height * factor), Image.Resampling.LANCZOS)
            crop.save(out)
            return str(out)
        logger.warning("%s không tìm thấy vùng code — hiện cả ảnh", _TAG)
        side = max(img.size)
        if side > 900:
            ratio = 900.0 / side
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.Resampling.LANCZOS)
        img.save(out)
        return str(out)
    finally:
        img.close()


# --------------------------------------------------------------------------
# Cửa sổ nhập (Tk, một thread riêng)
# --------------------------------------------------------------------------
class _Request:
    def __init__(self, preview_path: str, lengths: tuple, timeout_s: float):
        self.preview_path = preview_path
        self.lengths = tuple(int(n) for n in lengths)
        self.deadline = time.monotonic() + float(timeout_s)
        self.created = time.perf_counter()
        self.future: concurrent.futures.Future = concurrent.futures.Future()
        self.cancelled = False

    def finish(self, code, reason: str) -> None:
        if not self.future.done():
            self.future.set_result((code, reason))


class _UiThread:
    def __init__(self):
        self._queue: "queue.Queue[_Request]" = queue.Queue()
        self._thread = None
        self._ready = threading.Event()
        self._error = None
        self._showing = False
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._main, name="manual-confirm-ui", daemon=True)
                self._thread.start()
        self._ready.wait(timeout=5.0)
        return self._error is None and self._ready.is_set()

    def submit(self, req: _Request) -> None:
        self._queue.put(req)

    # ---- thread chính của Tk ----
    def _main(self):
        try:
            import tkinter as tk
        except Exception as exc:
            self._error = f"tkinter không khả dụng: {exc}"
            self._ready.set()
            return
        try:
            self._tk = tk
            self._root = tk.Tk()
            self._root.withdraw()
        except Exception as exc:
            self._error = f"không tạo được cửa sổ: {exc}"
            self._ready.set()
            return
        self._ready.set()
        self._root.after(50, self._poll)
        self._root.mainloop()

    def _poll(self):
        try:
            if not self._showing:
                try:
                    req = self._queue.get_nowait()
                except queue.Empty:
                    req = None
                if req is not None:
                    if req.cancelled or time.monotonic() >= req.deadline:
                        _bump(pending=-1, timed_out=1)
                        req.finish(None, "timeout")
                        logger.info("%s bỏ qua ảnh đã quá hạn trước khi kịp hiện", _TAG)
                    else:
                        self._show(req)
        except Exception as exc:
            logger.error("%s lỗi UI: %s", _TAG, exc)
            self._showing = False
        finally:
            self._root.after(50, self._poll)

    def _show(self, req: _Request) -> None:
        tk = self._tk
        self._showing = True
        win = tk.Toplevel(self._root)
        win.title("QQ88: gõ code rồi Enter (Esc = bỏ qua)")
        win.attributes("-topmost", True)
        try:
            photo = tk.PhotoImage(file=req.preview_path)
            tk.Label(win, image=photo).pack(padx=8, pady=8)
            win._photo = photo  # giữ tham chiếu để Tk không thu hồi ảnh
        except Exception as exc:
            tk.Label(win, text=f"Không hiện được ảnh: {exc}").pack(padx=8, pady=8)

        var = tk.StringVar()
        entry = tk.Entry(win, textvariable=var, font=("Consolas", 22), justify="center", width=20)
        entry.pack(padx=8, pady=4)
        note = tk.Label(win, text="", fg="#b00020", font=("Segoe UI", 10))
        note.pack()
        timer = tk.Label(win, text="", fg="#555555", font=("Segoe UI", 10))
        timer.pack(pady=(0, 6))

        started = time.perf_counter()
        state = {"done": False}

        def close(code, reason):
            if state["done"]:
                return
            state["done"] = True
            elapsed = (time.perf_counter() - started) * 1000.0
            if code:
                _bump(pending=-1, confirmed=1, last_answer_ms=elapsed)
            elif reason == "timeout":
                _bump(pending=-1, timed_out=1)
            else:
                _bump(pending=-1, skipped=1)
            logger.info("%s kết quả=%s sau %.0fms (từ lúc hiện cửa sổ)", _TAG, reason, elapsed)
            try:
                win.destroy()
            finally:
                self._showing = False
                req.finish(code, reason)

        def submit(_event=None):
            value = var.get().strip().replace(" ", "")
            if not value:
                note.config(text="Chưa nhập code")
                return
            if not _CODE_RE.match(value):
                note.config(text="Code chỉ gồm chữ và số")
                return
            if req.lengths and len(value) not in req.lengths:
                note.config(text=f"Độ dài {len(value)} không hợp lệ (cần {'/'.join(map(str, req.lengths))})")
                return
            close(value, "typed")

        def skip(_event=None):
            close(None, "skipped")

        def tick():
            if state["done"]:
                return
            remaining = req.deadline - time.monotonic()
            if req.cancelled or remaining <= 0:
                close(None, "timeout")
                return
            timer.config(text=f"Còn {remaining:0.0f}s")
            win.after(200, tick)

        entry.bind("<Return>", submit)
        win.bind("<Escape>", skip)
        win.protocol("WM_DELETE_WINDOW", skip)
        row = tk.Frame(win)
        row.pack(pady=(0, 8))
        tk.Button(row, text="Gửi (Enter)", command=submit, width=14).pack(side="left", padx=4)
        tk.Button(row, text="Bỏ qua (Esc)", command=skip, width=14).pack(side="left", padx=4)

        try:
            import winsound

            winsound.Beep(1200, 120)
        except Exception:
            win.bell()
        win.lift()
        win.after(30, lambda: (win.focus_force(), entry.focus_set()))
        tick()


_ui = _UiThread()


# --------------------------------------------------------------------------
# API cho main_script
# --------------------------------------------------------------------------
async def ask_code(image_path: str, *, timeout_s: float = 25.0, lengths: tuple = (10, 12)):
    """Hiện ảnh đã cắt cho người dùng và chờ họ gõ code.

    Trả về (code | None, lý do) với lý do: typed / skipped / timeout / ui_unavailable / preview_error.
    """
    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(None, _ui.start):
        logger.error("%s cửa sổ nhập không khả dụng: %s", _TAG, _ui._error)
        return None, "ui_unavailable"

    t0 = time.perf_counter()
    try:
        preview = await loop.run_in_executor(None, prepare_preview, image_path)
    except Exception as exc:
        logger.error("%s không tạo được ảnh xem trước: %s", _TAG, exc)
        return None, "preview_error"
    logger.info("%s đã cắt ảnh xem trước sau %.0fms", _TAG, (time.perf_counter() - t0) * 1000.0)

    req = _Request(preview, lengths, timeout_s)
    _bump(pending=1)
    _ui.submit(req)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(req.future), timeout=timeout_s + 5.0)
    except asyncio.TimeoutError:
        req.cancelled = True
        return None, "timeout"
    except asyncio.CancelledError:
        req.cancelled = True
        raise
    finally:
        try:
            Path(preview).unlink(missing_ok=True)
        except Exception:
            pass
