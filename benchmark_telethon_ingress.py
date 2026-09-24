"""Benchmark the old blocking callback against the tracked background callback."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass

from task_tracker import schedule_tracked_task


@dataclass
class RunResult:
    mode: str
    messages: int
    enqueue_delay_ms: float
    executor_workers: int
    callback_return_ms: float
    durable_complete_ms: float
    callback_p50_us: float
    callback_p95_us: float
    max_inflight_enqueue: int


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction)))
    return ordered[index]


async def run_once(mode: str, messages: int, delay_ms: float, workers: int) -> RunResult:
    executor_gate = asyncio.Semaphore(workers)
    completed = 0
    inflight = 0
    max_inflight = 0
    completed_lock = asyncio.Lock()
    all_done = asyncio.Event()

    async def fake_durable_enqueue(_message_id: int) -> int:
        nonlocal completed, inflight, max_inflight
        async with executor_gate:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
            await asyncio.sleep(delay_ms / 1000.0)
            inflight -= 1
            async with completed_lock:
                completed += 1
                if completed == messages:
                    all_done.set()
            return _message_id

    callback_latencies_us: list[float] = []
    tracked_tasks: set[asyncio.Task] = set()
    callback_start = time.perf_counter()

    for message_id in range(messages):
        started = time.perf_counter()
        if mode == "before-await":
            await fake_durable_enqueue(message_id)
        else:
            schedule_tracked_task(
                fake_durable_enqueue(message_id),
                tracked_tasks,
                f"benchmark-ingress-{message_id}",
            )
        callback_latencies_us.append((time.perf_counter() - started) * 1_000_000)

    callback_return_ms = (time.perf_counter() - callback_start) * 1000.0
    if mode == "before-await":
        durable_complete_ms = callback_return_ms
    else:
        await asyncio.wait_for(all_done.wait(), timeout=max(5.0, messages * delay_ms / workers / 1000.0 * 10.0))
        await asyncio.gather(*list(tracked_tasks), return_exceptions=True)
        durable_complete_ms = (time.perf_counter() - callback_start) * 1000.0

    return RunResult(
        mode=mode,
        messages=messages,
        enqueue_delay_ms=delay_ms,
        executor_workers=workers,
        callback_return_ms=callback_return_ms,
        durable_complete_ms=durable_complete_ms,
        callback_p50_us=percentile(callback_latencies_us, 0.50),
        callback_p95_us=percentile(callback_latencies_us, 0.95),
        max_inflight_enqueue=max_inflight,
    )


async def main(args) -> None:
    results = [
        await run_once("before-await", args.messages, args.delay_ms, args.workers),
        await run_once("after-background-task", args.messages, args.delay_ms, args.workers),
    ]
    payload = [result.__dict__ for result in results]
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    before, after = results
    print("\nSummary")
    print(f"messages={args.messages} enqueue_delay_ms={args.delay_ms} executor_workers={args.workers}")
    print(f"callback return speedup: {before.callback_return_ms / max(after.callback_return_ms, 1e-9):.1f}x")
    print(f"callback p95 reduction: {before.callback_p95_us / max(after.callback_p95_us, 1e-9):.1f}x")
    print(f"durable completion before={before.durable_complete_ms:.2f}ms after={after.durable_complete_ms:.2f}ms")
    print(f"max concurrent enqueue before={before.max_inflight_enqueue} after={after.max_inflight_enqueue}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=2000)
    parser.add_argument("--delay-ms", type=float, default=2.0)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    asyncio.run(main(args))
