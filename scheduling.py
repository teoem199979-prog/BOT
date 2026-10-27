"""Lập lịch theo độ tươi + lấy mã khi tab sẵn sàng (pull-when-ready).

Vì sao có file này (số liệu từ logs.zip, 22-25/10/2026):
  * xx88code.com: tuổi mã lúc bắt đầu gửi median 48s, p90 99s (trần 120s); chờ hàng đợi median 6s,
    p90 33s; 570/591 lượt gửi kết thúc ở BROWSER_VERIFICATION_WAIT (tab chưa xác minh xong).
  * Hàng đợi FIFO nên worker luôn lấy mã CŨ nhất trước, trong khi mã mới mới còn lượt.
  * Worker lấy mã rồi mới phát hiện tab chưa sẵn sàng => hoãn, lấy mã khác, lặp lại.

Hai thay đổi, đều theo domain trong .env/config và tắt được bằng cách để trống danh sách:
  FRESHEST_FIRST_DOMAINS     hàng đợi lấy mã tươi nhất trước (xem xx88_priority_queue.FreshestFirstQueue)
  VERIFICATION_GATE_DOMAINS  worker không lấy mã khi mọi tab đang chờ xác minh; chờ sự kiện "tab sẵn sàng"
"""
from __future__ import annotations

from config import Config


def _domains(name: str) -> tuple:
    return tuple(getattr(Config, name, ()) or ())


def freshest_first_enabled(domain: str) -> bool:
    return domain in _domains("FRESHEST_FIRST_DOMAINS")


def verification_gate_enabled(domain: str) -> bool:
    return domain in _domains("VERIFICATION_GATE_DOMAINS")


async def hold_until_verification_ready(domain: str, engine, timeout: float = 1.0) -> bool:
    """Trả True nếu vừa phải chờ (người gọi nên kiểm tra lại điều kiện rồi mới lấy mã).

    engine cần có verification_blocked(domain) -> bool và wait_for_verification_ready(domain, timeout).
    Lỗi ở đây không được làm hỏng worker: gặp lỗi thì coi như không chặn.
    """
    if engine is None or not verification_gate_enabled(domain):
        return False
    try:
        if not engine.verification_blocked(domain):
            return False
        await engine.wait_for_verification_ready(domain, timeout=max(0.05, float(timeout)))
        return True
    except Exception:
        return False


class EarlyDispatch:
    """Gửi mã ngay khi OCR video phát hiện (on_code), rồi đối chiếu với kết quả cuối.

    on_code(item) chạy trong luồng worker OCR và an toàn với luồng; việc gửi thật (coroutine submit)
    chạy trên event loop. Mã đã gửi sớm được loại khỏi lô cuối để không bị gửi hai lần (hai tài
    khoản khác nhau); mã mà lần gửi sớm bị lỗi vẫn nằm trong lô cuối nên không bị mất.
    """

    def __init__(self, loop, submit):
        self._loop = loop
        self._submit = submit
        self._futures = []
        self._done = set()
        self.errors = []

    async def _run(self, item):
        await self._submit(item)
        self._done.add(item.get("code"))

    def on_code(self, item) -> None:
        import asyncio
        self._futures.append(asyncio.run_coroutine_threadsafe(self._run(dict(item)), self._loop))

    async def settle(self, codes, emitted):
        """Chờ các lần gửi sớm xong; trả (codes còn lại cho lô cuối, tập mã đã gửi sớm thành công)."""
        import asyncio
        pending = list(self._futures)
        if pending:
            outcomes = await asyncio.gather(*(asyncio.wrap_future(f) for f in pending), return_exceptions=True)
            self.errors = [o for o in outcomes if isinstance(o, BaseException)]
        skipped = {code for code in (emitted or ()) if code in self._done}
        remaining = [item for item in (codes or []) if item.get("code") not in skipped]
        return remaining, skipped
