from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from durable_inbox import DurableInbox


def main():
    with TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "inbox.db")
        inbox = DurableInbox(db_path, max_attempts=5)
        total = 1000

        def insert(i):
            return inbox.enqueue(
                chat_id=-100001,
                message_id=i,
                message_date="2026-09-24T11:20:00+00:00",
                edited=False,
                text=f"CODE-{i}",
                has_media=False,
            )

        with ThreadPoolExecutor(max_workers=32) as pool:
            ids = list(pool.map(insert, range(total)))

        assert all(ids), "at least one durable insert failed"
        assert len(set(ids)) == total, "unique events did not get unique rows"
        assert len(inbox.pending_ids(total + 10)) == total

        # Same event is idempotent under concurrent duplicate delivery.
        with ThreadPoolExecutor(max_workers=32) as pool:
            duplicate_ids = list(pool.map(lambda _: insert(7), range(100)))
        assert set(duplicate_ids) == {ids[7]}
        assert len(inbox.pending_ids(total + 10)) == total
        inbox.close()
    print(f"ok: {total} concurrent durable inserts and duplicate deliveries")


if __name__ == "__main__":
    main()
