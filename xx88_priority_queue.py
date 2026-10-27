

import asyncio

XX88_SPOILER_CHANNEL_ID = -1002817093108


def is_priority_xx88_spoiler(item):
    if not isinstance(item, dict):
        return False
    try:
        chat_id = int(item.get('chat_id') or 0)
    except (TypeError, ValueError):
        return False
    return (
        chat_id == XX88_SPOILER_CHANNEL_ID
        and str(item.get('source_kind') or item.get('source') or '').lower() == 'spoiler'
    )


class XX88SpoilerPriorityQueue(asyncio.Queue):

    def _put(self, item):
        if not is_priority_xx88_spoiler(item):
            return super()._put(item)

        insertion = 0
        for pending in self._queue:
            if not is_priority_xx88_spoiler(pending):
                break
            insertion += 1
        self._queue.insert(insertion, item)


def _freshness_age(item, now_perf=None):
    """Tuổi của mã (giây): ưu tiên giờ tin nhắn; không có thì tính từ lúc vào hàng đợi."""
    import time
    from freshness import age_seconds
    if isinstance(item, dict):
        age = age_seconds(item.get('msg_ts'))
        if age is not None:
            return age
        queued = item.get('_queued_perf')
        if queued is not None:
            return max(0.0, (now_perf if now_perf is not None else time.perf_counter()) - float(queued))
    return 0.0


class FreshestFirstQueue(XX88SpoilerPriorityQueue):
    """Như XX88SpoilerPriorityQueue nhưng lấy mã TƯƠI NHẤT trước thay vì FIFO.

    Mã spoiler ưu tiên (nằm ở đầu hàng đợi) vẫn đi trước mã khác; trong cùng nhóm thì mã có tuổi
    nhỏ nhất đi trước, hòa thì mã vào trước đi trước. Mã quá cũ vẫn bị worker loại theo
    CODE_MAX_AGE_SECONDS như trước, nên không có mã bị giữ vô hạn.
    """

    def _get(self):
        queue = self._queue
        if len(queue) <= 1:
            return queue.popleft()
        limit = 0
        for pending in queue:
            if not is_priority_xx88_spoiler(pending):
                break
            limit += 1
        span = limit or len(queue)
        import time
        now = time.perf_counter()
        best_index, best_age = 0, None
        for index in range(span):
            age = _freshness_age(queue[index], now)
            if best_age is None or age < best_age:
                best_index, best_age = index, age
        item = queue[best_index]
        del queue[best_index]
        return item
