"""Bounded and deduplicated Telegram media downloads."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from logger_setup import logger


@dataclass
class DownloadMetrics:
    path: Optional[str]
    size_bytes: Optional[int]
    elapsed_ms: float
    bytes_per_sec: Optional[float]
    attempts: int
    dedup_hit: bool


class MediaDownloadManager:
    """Limit concurrent downloads and coalesce duplicate in-flight requests.

    ✅ QUAN TRỌNG: dedup chỉ áp dụng cho ĐÚNG 1 TIN NHẮN CỤ THỂ (khoá =
    chat_id + message_id + media_id, xem key_for()) — KHÔNG dedup rộng theo
    chỉ mỗi media_id. Nếu 2 kênh khác nhau đăng trùng cùng 1 ảnh/video gần
    như đồng thời (rất hay gặp với các kênh QQ88/Hi88 hay đăng lại banner
    giống nhau), mỗi kênh vẫn phải được tải/OCR riêng — dedup theo nội dung
    file sẽ khiến kênh thứ 2 bị âm thầm BỎ QUA OCR dù là tin nhắn khác hẳn.
    """

    def __init__(
        self,
        max_concurrent: int = 2,
        retries: int = 1,
        retry_delay: float = 0.5,
        max_size_bytes: Optional[int] = None,
    ):
        self.semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
        self.retries = max(0, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        # ✅ Giới hạn dung lượng tải tối đa — chặn tải về những file bất
        # thường lớn (video dài quá mức, ảnh độ phân giải khủng do đăng
        # nhầm) vốn không phải giftcode thật. None = không giới hạn.
        self.max_size_bytes = int(max_size_bytes) if max_size_bytes else None
        self._inflight: dict[tuple[Any, Any, Any], asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def key_for(event: Any) -> tuple[Any, Any, Any]:
        """Khoá dedup theo (chat_id, message_id, media_id) — xem docstring
        của class ở trên về lý do KHÔNG được rút gọn khoá này."""
        message = getattr(event, "message", None)
        media = getattr(message, "media", None)
        media_id = None
        try:
            if getattr(media, "document", None) is not None:
                media_id = getattr(media.document, "id", None)
            elif getattr(media, "photo", None) is not None:
                media_id = getattr(media.photo, "id", None)
        except Exception:
            media_id = None
        return (
            getattr(event, "chat_id", None),
            getattr(message, "id", None),
            media_id,
        )

    async def download(self, event: Any, output_dir: str | Path) -> DownloadMetrics:
        """Tải media từ event. Nếu đã có 1 lượt tải ĐANG CHẠY cho đúng tin
        nhắn này (dedup theo key_for), dùng chung kết quả thay vì tải lại."""
        key = self.key_for(event)
        async with self._lock:
            current = self._inflight.get(key)
            if current is None or current.done():
                task = asyncio.create_task(self._download_once(event, output_dir))
                self._inflight[key] = task
                task.add_done_callback(
                    lambda completed, _key=key, _task=task: asyncio.create_task(
                        self._remove_inflight_when_done(_key, _task)
                    )
                )
                dedup_hit = False
            else:
                task = current
                dedup_hit = True

        try:
            try:
                # shield: nếu caller bị cancel (vd timeout xử lý tin nhắn),
                # lượt tải thật vẫn tiếp tục chạy tới cùng cho các caller
                # dedup khác đang chờ chung, không bị huỷ giữa chừng.
                result = await asyncio.wait_for(asyncio.shield(task), timeout=300.0)
            except asyncio.TimeoutError:
                logger.error(f"❌ [Download] Timeout 300s chờ tải xong (key={key})")
                result = DownloadMetrics(
                    path=None, size_bytes=None, elapsed_ms=300_000.0,
                    bytes_per_sec=None, attempts=0, dedup_hit=dedup_hit,
                )

            if dedup_hit:
                # ✅ FIX: KHÔNG mutate object 'result' dùng chung giữa nhiều
                # coroutine (race condition) — luôn tạo bản sao riêng cho
                # lượt dedup-hit này.
                result = DownloadMetrics(
                    path=result.path,
                    size_bytes=result.size_bytes,
                    elapsed_ms=result.elapsed_ms,
                    bytes_per_sec=result.bytes_per_sec,
                    attempts=result.attempts,
                    dedup_hit=True,
                )
            return result
        finally:
            if not dedup_hit and task.done():
                async with self._lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)

    async def _remove_inflight_when_done(self, key, task) -> None:
        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _download_once(self, event: Any, output_dir: str | Path) -> DownloadMetrics:
        started = time.perf_counter()
        attempts = 0
        path = None

        async with self.semaphore:
            for attempts in range(1, self.retries + 2):
                try:
                    path = await self._download_with_size_check(event, output_dir)
                    if path:
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if attempts > self.retries:
                        logger.error(f"❌ [Download] Thất bại sau {attempts} lần thử: {e}")
                    else:
                        wait_s = self.retry_delay * attempts
                        logger.warning(
                            f"⚠️ [Download] Lần {attempts} lỗi ({e}) → retry sau {wait_s:.1f}s"
                        )
                        await asyncio.sleep(wait_s)

        elapsed = max(time.perf_counter() - started, 1e-9)
        size_bytes = None
        if path:
            try:
                size_bytes = Path(path).stat().st_size
            except OSError:
                pass

        bytes_per_sec = round(size_bytes / elapsed, 2) if size_bytes is not None else None

        if path and size_bytes is not None:
            logger.debug(
                f"✅ [Download] {Path(path).name} — {size_bytes / 1024:.1f}KB, "
                f"{elapsed * 1000:.0f}ms"
                + (f", {bytes_per_sec / 1_000_000:.2f}MB/s" if bytes_per_sec else "")
                + f" (lần {attempts})"
            )

        return DownloadMetrics(
            path=str(path) if path else None,
            size_bytes=size_bytes,
            elapsed_ms=round(elapsed * 1000, 2),
            bytes_per_sec=bytes_per_sec,
            attempts=attempts,
            dedup_hit=False,
        )

    async def _download_with_size_check(self, event: Any, output_dir: str | Path) -> Optional[str]:
        """Tải 1 lượt — kiểm tra dung lượng TRƯỚC khi tải (nếu Telegram báo
        trước size qua document.size) để không tốn băng thông cho file rõ
        ràng quá khổ, và kiểm tra LẠI sau khi tải xong (phòng khi size khai
        báo trước không chính xác) — xoá ngay file tạm nếu vượt giới hạn.

        Dùng event/message downloader nếu có; durable replay có thể bọc
        Telethon Message trong SimpleNamespace nên fallback qua TelegramClient.
        """
        message = getattr(event, "message", None)
        media = getattr(message, "media", None) if message else None

        declared_size = None
        try:
            if getattr(media, "document", None) is not None:
                declared_size = getattr(media.document, "size", None)
        except Exception:
            declared_size = None

        if self.max_size_bytes and declared_size and declared_size > self.max_size_bytes:
            raise ValueError(
                f"File quá khổ (khai báo trước): {declared_size / 1_000_000:.1f}MB > "
                f"{self.max_size_bytes / 1_000_000:.1f}MB giới hạn"
            )

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        downloader = getattr(event, "download_media", None)
        if downloader is None and message is not None:
            downloader = getattr(message, "download_media", None)
        if downloader is not None:
            path = await downloader(file=str(output_dir))
        else:
            # Deferred import avoids a module cycle during main_script startup.
            import main_script as _ms
            telegram_client = getattr(_ms, "client", None)
            if telegram_client is None or message is None:
                return None
            path = await telegram_client.download_media(message, file=str(output_dir))

        if not path:
            return None

        if self.max_size_bytes:
            try:
                actual_size = Path(path).stat().st_size
            except OSError:
                actual_size = None
            if actual_size is not None and actual_size > self.max_size_bytes:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass
                raise ValueError(
                    f"File tải về quá khổ: {actual_size / 1_000_000:.1f}MB > "
                    f"{self.max_size_bytes / 1_000_000:.1f}MB giới hạn"
                )

        return path


def cleanup_stale_files(directory: Union[str, Path], max_age_seconds: float = 3600.0) -> int:
    """Xoá các FILE (không đụng thư mục con) cũ hơn max_age_seconds trong
    'directory'. Trả về số file đã xoá.

    An toàn:
      - Chỉ xoá FILE, không đụng thư mục con.
      - Bỏ qua lỗi từng file riêng lẻ (đang bị khoá, quyền truy cập...).
      - Không xoá/tạo gì nếu 'directory' không tồn tại (trả về 0).

    ✅ FIX: luôn ép 'directory' về Path ngay đầu hàm — nếu caller lỡ truyền
    str (vd cleanup_stale_files("logs/tmp")), gọi .exists() thẳng lên str
    sẽ ném AttributeError vì str không có method này.
    """
    directory = Path(directory)
    if not directory.exists():
        return 0

    now = time.time()
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0

    for entry in entries:
        try:
            if not entry.is_file():
                continue
            age = now - entry.stat().st_mtime
            if age > max_age_seconds:
                entry.unlink()
                removed += 1
        except Exception:
            continue

    return removed
