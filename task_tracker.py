from __future__ import annotations

import asyncio
from typing import Any, Coroutine

from logger_setup import logger


def schedule_tracked_task(
    coro: Coroutine[Any, Any, Any],
    task_set: set[asyncio.Task],
    name: str,
) -> asyncio.Task:
    """Schedule a background coroutine and remove it after completion."""
    task = asyncio.create_task(coro, name=name)
    task_set.add(task)

    def _finish(done_task: asyncio.Task) -> None:
        task_set.discard(done_task)
        if done_task.cancelled():
            return
        try:
            error = done_task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("❌ Background task '%s' lỗi: %s", name, error)

    task.add_done_callback(_finish)
    return task


__all__ = ["schedule_tracked_task"]
