
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from logger_setup import logger
from media_identity import telegram_media_identity
import sys


def _main_module():

    mod = sys.modules.get("__main__")
    if mod is not None and hasattr(mod, "client"):
        return mod
    import main_script as mod
    return mod


@dataclass
class DownloadMetrics:
    path: Optional[str]
    size_bytes: Optional[int]
    elapsed_ms: float
    bytes_per_sec: Optional[float]
    attempts: int
    dedup_hit: bool


class MediaDownloadManager:

    def __init__(
        self,
        max_concurrent: int = 2,
        retries: int = 1,
        retry_delay: float = 0.5,
        max_size_bytes: Optional[int] = None,
        fast_download: bool = False,
        fast_min_bytes: int = 4 * 1024 * 1024,
        fast_workers: int = 4,
        fast_chunk_kb: int = 512,
    ):
        self.semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
        self.retries = max(0, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))

        self.max_size_bytes = int(max_size_bytes) if max_size_bytes else None
        self.fast_download = bool(fast_download)
        self.fast_min_bytes = max(1024 * 1024, int(fast_min_bytes))
        self.fast_workers = max(2, min(8, int(fast_workers)))
        self.fast_chunk_size = max(128, min(2048, int(fast_chunk_kb))) * 1024
        self._inflight: dict[tuple[Any, ...], asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()

    async def _ensure_client_connected(self, client: Any) -> None:

        if client is None or client.is_connected():
            return
        async with self._connect_lock:
            if client.is_connected():
                return
            main_module = _main_module()
            config = getattr(main_module, "Config", None)
            timeout = max(
                1.0,
                float(getattr(config, "TELEGRAM_CONNECT_TIMEOUT", 20.0)),
            )
            logger.warning(
                "⚠️ [Download] Telegram disconnected — reconnect trước khi tải media"
            )
            await asyncio.wait_for(client.connect(), timeout=timeout)
            if not client.is_connected():
                raise ConnectionError("Telegram client chưa connected sau reconnect")

    @staticmethod
    def _revive_dead_exported_senders(client: Any) -> int:

        borrowed = getattr(client, "_borrowed_senders", None)
        if not isinstance(borrowed, dict):
            return 0
        revived = 0
        for dc_id, pair in list(borrowed.items()):
            try:
                state, sender = pair
                if sender is not None and not sender.is_connected():
                    state._connected = False
                    revived += 1
                    logger.warning(
                        "⚠️ [Download] Sender media DC %s đã rớt — đánh dấu nối lại", dc_id
                    )
            except Exception:
                continue
        return revived

    @staticmethod
    def key_for(event: Any) -> tuple[Any, Any, Any]:

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

        # OCR callers own separate temporary directories and immediately rename
        # their input after downloading. A shared path from another caller can
        # disappear before this caller reads it, and owner cancellation must not
        # cancel a separate job using a different OCR policy.
        key = (*self.key_for(event), str(Path(output_dir).resolve()))
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

                result = await asyncio.wait_for(asyncio.shield(task), timeout=300.0)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                logger.error(f"❌ [Download] Timeout 300s chờ tải xong (key={key})")
                result = DownloadMetrics(
                    path=None, size_bytes=None, elapsed_ms=300_000.0,
                    bytes_per_sec=None, attempts=0, dedup_hit=dedup_hit,
                )

            if dedup_hit:

                result = DownloadMetrics(
                    path=result.path,
                    size_bytes=result.size_bytes,
                    elapsed_ms=result.elapsed_ms,
                    bytes_per_sec=result.bytes_per_sec,
                    attempts=result.attempts,
                    dedup_hit=True,
                )
            return result
        except asyncio.CancelledError:
            if not dedup_hit:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
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
        telegram_client = None
        if self.fast_download and declared_size and declared_size >= self.fast_min_bytes:
            try:
                _ms = _main_module()
                telegram_client = getattr(_ms, "client", None)
                if telegram_client is not None and getattr(media, "document", None) is not None:
                    await self._ensure_client_connected(telegram_client)
                    fast_path = await self._multipart_download(
                        telegram_client, message, output_dir, declared_size,
                        chat_id=getattr(event, "chat_id", None),
                    )
                    if fast_path:
                        return fast_path
                    logger.warning("⚠️ [Download] Multipart không trả file, fallback Telethon")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("⚠️ [Download] Multipart lỗi, fallback Telethon: %s", exc)
        downloader = getattr(event, "download_media", None)
        if downloader is None and message is not None:
            downloader = getattr(message, "download_media", None)
        if downloader is not None:
            _ms = _main_module()
            telegram_client = telegram_client or getattr(_ms, "client", None)
            await self._ensure_client_connected(telegram_client)
            path = await downloader(file=str(output_dir))
        else:
            _ms = _main_module()
            telegram_client = telegram_client or getattr(_ms, "client", None)
            if telegram_client is None or message is None:
                return None
            await self._ensure_client_connected(telegram_client)
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

    async def _multipart_download(
        self, client: Any, message: Any, output_dir: str | Path, file_size: int,
        *, chat_id: Any = None,
    ) -> Optional[str]:
        chat_id = chat_id if chat_id is not None else getattr(message, "chat_id", None)
        media_id = telegram_media_identity(message)
        if chat_id is None or media_id is None:
            raise ValueError("Multipart download requires a chat ID and stable media ID")
        media_key = {
            "chat_id": str(chat_id),
            "message_id": str(getattr(message, "id", "")),
            "media_id": media_id,
            "file_size": int(file_size),
        }
        session = json.dumps(media_key, sort_keys=True, separators=(",", ":"))
        file_key = hashlib.sha256(session.encode("utf-8")).hexdigest()
        file_info = getattr(message, "file", None)
        name = getattr(file_info, "name", None) or ""
        if not name:
            ext = getattr(file_info, "ext", None) or ""
            name = f"telegram_{getattr(message, 'id', 'media')}{ext}"
        suffix = Path(name).suffix
        if len(suffix) > 12 or not suffix[1:].isalnum():
            suffix = ""
        final_path = Path(output_dir) / f"telegram_{file_key}{suffix}"
        temp_path = final_path.with_name(final_path.name + ".multipart.part")
        manifest_path = temp_path.with_name(temp_path.name + ".json")
        chunk = min(self.fast_chunk_size, 512 * 1024)
        chunks = max(1, (file_size + chunk - 1) // chunk)
        if file_size < 8 * 1024 * 1024:
            size_workers = 2
        elif file_size < 16 * 1024 * 1024:
            size_workers = 4
        else:
            size_workers = 8
        workers = min(self.fast_workers, size_workers, chunks)
        done: set[int] = set()
        try:
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                saved.get("file_size") == file_size
                and saved.get("chunk_size") == chunk
                and saved.get("session") == session
                and saved.get("media_key") == media_key
            ):
                done = {int(i) for i in saved.get("chunks_done", []) if 0 <= int(i) < chunks}
        except (OSError, ValueError, TypeError, AttributeError):
            done = set()
        if not temp_path.exists() or temp_path.stat().st_size != file_size:
            with temp_path.open("wb") as handle:
                handle.truncate(file_size)
            done.clear()
        resumed_count = len(done)
        manifest_lock = asyncio.Lock()
        retry_count = 0
        started = time.perf_counter()

        manifest_writes = 0
        last_manifest_write = 0.0

        def write_manifest_sync() -> None:

            nonlocal manifest_writes, last_manifest_write
            payload = {
                "file_size": file_size,
                "chunk_size": chunk,
                "chunks_done": sorted(done),
                "session": session,
                "media_key": media_key,
            }
            tmp_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
            with tmp_manifest.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(payload))
            os.replace(tmp_manifest, manifest_path)
            manifest_writes += 1
            last_manifest_write = time.perf_counter()

        async def save_manifest(force: bool = False) -> None:

            if not force and time.perf_counter() - last_manifest_write < 1.0:
                return
            write_manifest_sync()

        async def fetch_chunk(index: int, handle) -> None:
            nonlocal retry_count
            if index in done:
                return
            offset = index * chunk
            remaining = min(chunk, file_size - offset)
            for attempt in range(1, 4):
                received = 0
                try:
                    await self._ensure_client_connected(client)
                    async for data in client.iter_download(
                        message,
                        offset=offset,
                        limit=1,
                        chunk_size=chunk,
                        request_size=chunk,
                        file_size=file_size,
                    ):
                        block = bytes(data)[:remaining]
                        if block:
                            handle.seek(offset)
                            handle.write(block)
                            received += len(block)
                        break
                    if received != remaining:
                        raise IOError(f"multipart chunk mismatch at {offset}: {received}/{remaining} bytes")
                    handle.flush()
                    async with manifest_lock:
                        done.add(index)
                        await save_manifest()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if attempt >= 3:
                        raise
                    main_down = not client.is_connected()
                    revived = 0
                    if isinstance(exc, ConnectionError):
                        revived = self._revive_dead_exported_senders(client)
                    if not main_down and not revived:
                        raise
                    retry_count += 1
                    await self._ensure_client_connected(client)
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

        try:
            async def fetch_lane(lane: int) -> None:
                with temp_path.open("r+b") as handle:
                    for index in range(lane, chunks, workers):
                        await fetch_chunk(index, handle)

            lanes = [asyncio.create_task(fetch_lane(lane)) for lane in range(workers)]
            try:
                await asyncio.gather(*lanes)
            finally:
                for lane in lanes:
                    if not lane.done():
                        lane.cancel()
                await asyncio.gather(*lanes, return_exceptions=True)
            if len(done) != chunks:
                raise IOError(f"multipart manifest incomplete: {len(done)}/{chunks}")
            temp_path.replace(final_path)
            manifest_path.unlink(missing_ok=True)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            logger.info(
                "[METRIC] step=media_download multipart_ms=%.1f bytes=%s "
                "chunks_total=%s chunks_resumed=%s retry_count=%s speed_mbps=%.2f",
                elapsed_ms,
                file_size,
                chunks,
                resumed_count,
                retry_count,
                (file_size / max(time.perf_counter() - started, 1e-9)) / 1_000_000,
            )
            return str(final_path)
        except BaseException:

            try:
                write_manifest_sync()
            except Exception:
                pass
            raise
