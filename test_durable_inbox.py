"""Unit tests for durable message acceptance and tracked ingress tasks."""

import asyncio
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from durable_inbox import DurableInbox
from task_tracker import schedule_tracked_task


class DurableInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.inbox = DurableInbox(str(Path(self.tmp.name) / "inbox.db"), max_attempts=2)

    def tearDown(self):
        self.inbox.close()
        self.tmp.cleanup()

    def _enqueue(self, message_id=1, text=None, edited=False):
        return self.inbox.enqueue(
            chat_id=-100001,
            message_id=message_id,
            message_date="2026-09-24T11:30:00+00:00",
            edited=edited,
            text=text or f"CODE-{message_id}",
            has_media=False,
        )

    def test_duplicate_delivery_is_idempotent(self):
        first = self._enqueue(7)
        duplicate = self._enqueue(7)
        edited_duplicate = self._enqueue(7, edited=True)

        self.assertEqual(first, duplicate)
        self.assertEqual(first, edited_duplicate)
        self.assertEqual(len(self.inbox.pending_ids(10)), 1)

    def test_claim_is_atomic_between_threads(self):
        row_id = self._enqueue(8)
        results = []
        lock = threading.Lock()

        def claim():
            value = self.inbox.claim(row_id)
            with lock:
                results.append(value)

        threads = [threading.Thread(target=claim) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.inbox.get(row_id)["status"], "processing")

    def test_retry_or_fail_is_bounded(self):
        row_id = self._enqueue(9)
        self.assertIsNotNone(self.inbox.claim(row_id))

        self.assertEqual(self.inbox.retry_or_fail(row_id, "temporary"), "retried")
        self.assertIsNotNone(self.inbox.claim(row_id))
        self.assertEqual(self.inbox.retry_or_fail(row_id, "permanent"), "failed")
        self.assertEqual(self.inbox.get(row_id)["status"], "failed")

    def test_late_retry_cannot_revive_completed_row(self):
        row_id = self._enqueue(10)
        self.assertIsNotNone(self.inbox.claim(row_id))
        self.assertTrue(self.inbox.complete_item(row_id))

        self.assertEqual(self.inbox.retry_or_fail(row_id, "late worker error"), "completed")
        self.assertEqual(self.inbox.get(row_id)["status"], "completed")


class TrackedIngressTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_returns_without_waiting_for_enqueue(self):
        tasks = set()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_enqueue():
            started.set()
            await release.wait()
            return True

        task = schedule_tracked_task(slow_enqueue(), tasks, "test-ingress-slow")
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertIn(task, tasks)
        self.assertFalse(task.done())

        release.set()
        self.assertTrue(await asyncio.wait_for(task, timeout=1))
        await asyncio.sleep(0)
        self.assertEqual(tasks, set())

    async def test_task_exception_is_consumed_and_removed(self):
        tasks = set()

        async def failing_enqueue():
            raise RuntimeError("simulated sqlite failure")

        task = schedule_tracked_task(failing_enqueue(), tasks, "test-ingress-failing")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertTrue(task.done())
        self.assertEqual(tasks, set())
        # Calling exception confirms the task has a deterministic terminal
        # state; the helper's callback has already consumed/logged it.
        self.assertIsInstance(task.exception(), RuntimeError)

    async def test_cancelled_task_is_removed_without_leaking(self):
        tasks = set()
        blocker = asyncio.Event()

        async def blocked_enqueue():
            await blocker.wait()

        task = schedule_tracked_task(blocked_enqueue(), tasks, "test-ingress-cancel")
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        self.assertEqual(tasks, set())


if __name__ == "__main__":
    unittest.main()
