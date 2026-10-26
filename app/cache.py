"""
Tiny in-process TTL cache for global, read-mostly query results.

The data behind the list pages only changes when a worker cycle finishes or a
star is toggled, and each uncached view costs a fresh Turso connection plus
round trips. Entries expire after CACHE_TTL_SECONDS (default 60) and the whole
cache is cleared when a cycle ends or a star changes, so pages never serve data
older than the last change by more than the TTL.

Per-device data (read state, courses, history) is never cached here, and cached
values must be treated as read-only: callers copy rows before annotating them.
"""

import os
import threading
import time

_lock = threading.Lock()
_store = {}
_MAX_ENTRIES = 500


def _default_ttl() -> float:
    try:
        return float(os.environ.get("CACHE_TTL_SECONDS", "60"))
    except ValueError:
        return 60.0


def get_or_set(key, loader, ttl=None):
    ttl = _default_ttl() if ttl is None else ttl
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    value = loader()          # outside the lock; a racing duplicate load is harmless
    if ttl > 0:
        with _lock:
            if len(_store) >= _MAX_ENTRIES:
                _store.clear()
            _store[key] = (now + ttl, value)
    return value


def clear() -> None:
    with _lock:
        _store.clear()
