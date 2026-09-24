"""Лёгкое оперативное состояние, которое не нужно писать в SQLite."""
from __future__ import annotations

import time

_PRESENCE: dict[int, float] = {}
DEFAULT_ONLINE_TTL = 5 * 60


def touch(user_id: int, timestamp: float | None = None) -> None:
    if int(user_id) <= 0:
        return
    _PRESENCE[int(user_id)] = time.monotonic() if timestamp is None else float(timestamp)


def online_count(ttl: int = DEFAULT_ONLINE_TTL) -> int:
    now_mono = time.monotonic()
    cutoff = now_mono - max(30, int(ttl))
    stale = [uid for uid, seen in _PRESENCE.items() if seen < cutoff]
    for uid in stale:
        _PRESENCE.pop(uid, None)
    return len(_PRESENCE)


def clear() -> None:
    _PRESENCE.clear()
