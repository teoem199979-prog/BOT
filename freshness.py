

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _to_utc(value: Any) -> datetime | None:

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_seconds(value: Any, now: datetime | None = None) -> float | None:

    dt = _to_utc(value)
    if dt is None:
        return None
    ref = _to_utc(now) if now is not None else datetime.now(timezone.utc)
    if ref is None:
        raise ValueError("now must be a valid datetime")
    return max(0.0, (ref - dt).total_seconds())


def is_stale(value: Any, max_age: float, now: datetime | None = None) -> tuple[bool, float | None]:

    if max_age is None or float(max_age) <= 0:
        return False, None
    age = age_seconds(value, now)
    if age is None:
        return False, None
    return age > float(max_age), age


def first_known_age(candidates, max_age: float, now: datetime | None = None) -> tuple[bool, float | None]:

    for value in candidates:
        stale, age = is_stale(value, max_age, now)
        if age is not None:
            return stale, age
    return False, None
