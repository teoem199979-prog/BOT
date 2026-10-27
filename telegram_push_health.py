"""Bounded recovery of Telethon's public update stream; no session resets/joins."""
import asyncio
import time


def watchdog_backoff_seconds(attempt, *, base_seconds=30.0, max_seconds=300.0):
    attempt = max(1, int(attempt))
    base = min(300.0, max(0.0, float(base_seconds)))
    cap = min(300.0, max(base, float(max_seconds)))
    return min(cap, base * (2 ** (attempt - 1)))


class TelegramPushHealth:
    def __init__(self, client, log, *, clock=time.monotonic, stall_seconds=120,
                 cooldown_seconds=300, timeout=10):
        self.client, self.log, self.clock = client, log, clock
        self.stall = max(60, stall_seconds)
        self.cooldown = max(300, cooldown_seconds)
        self.timeout = timeout
        self.last_push = clock()
        self.fresh_poll_count = 0
        self.priority_pending = False
        self.next_recovery = 0.0
        self.recovering = False
        self.attempts = 0
        self.push_first = 0
        self.push_late = 0
        self.unproductive_recoveries = 0

    # PATCH_MARKER_2026-10-25-v4-poll-ocr
    def seconds_since_push(self):
        """Số giây từ lần push giao lần đầu gần nhất (Rx-Stat in ra)."""
        try:
            return max(0.0, self.clock() - float(self.last_push))
        except (TypeError, ValueError):
            return -1.0

    def note_push(self, age_seconds=None, *, first_delivery=True):
        # Old catch-up messages do not prove that live delivery has recovered.
        if age_seconds is None or age_seconds < 0 or age_seconds > 60:
            return
        if not first_delivery:
            self.push_late += 1
            if age_seconds > 1.0:
                return
        else:
            self.push_first += 1
        self.last_push = self.clock()
        self.unproductive_recoveries = 0
        self.fresh_poll_count = 0
        self.priority_pending = False

    def note_poll(self, age_seconds, *, priority=False):
        if 0 <= age_seconds <= 60:
            self.fresh_poll_count += 1
            self.priority_pending = self.priority_pending or priority

    def hold(self, seconds):
        """Hoãn recovery trong lúc receiver đang bị FloodWait.

        Recovery bật lại update stream cũng gửi request lên Telegram; hoãn lại
        trong lúc đang bị giới hạn để không kéo dài flood.
        """
        try:
            seconds = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return
        self.next_recovery = max(self.next_recovery, self.clock() + seconds + 1.0)

    async def enable(self, *, catch_up=False):
        async def activate():
            await self.client.set_receive_updates(True)
            if catch_up:
                pass  # Recovery does not queue old updates through catch_up().
        await asyncio.wait_for(activate(), timeout=self.timeout)

    async def maybe_recover(self):
        now = self.clock()
        if (self.recovering or (self.fresh_poll_count < 3 and not self.priority_pending) or now - self.last_push < self.stall
                or now < self.next_recovery or not self.client.is_connected()):
            return False
        if self.unproductive_recoveries >= 2:
            return False
        self.recovering = True
        self.attempts += 1
        self.unproductive_recoveries += 1
        self.next_recovery = now + max(
            self.cooldown, watchdog_backoff_seconds(self.unproductive_recoveries)
        )
        # Reset before awaiting: newly delivered push/poll events must not be lost.
        self.fresh_poll_count = 0
        priority = self.priority_pending
        self.priority_pending = False
        try:
            await self.enable(catch_up=False)
            self.log.warning(
                "[Push-Health] Update stream re-enabled; waiting for first-delivery push "
                "(watchdog_attempt=%s priority=%s push_first=%s push_late=%s)",
                self.attempts, priority, self.push_first, self.push_late,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                flood_seconds = max(0, float(getattr(exc, "seconds", 0)))
            except (TypeError, ValueError):
                flood_seconds = 0
            self.next_recovery = max(self.next_recovery, self.clock() + flood_seconds + 1)
            self.log.warning("[Push-Health] Re-enable failed: %s; cooldown respected", type(exc).__name__)
            return False
        finally:
            self.recovering = False

    async def run(self):
        while True:
            await asyncio.sleep(15)
            await self.maybe_recover()
