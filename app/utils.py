"""
Shared utilities.

Date normalization exists because feedparser exposes `published` as a raw
RFC-822 string (e.g. "Wed, 06 Aug 2025 12:00:00 GMT"). That format does not
sort correctly under SQLite's lexicographic string ordering, and it does not
compare correctly against `date('now', '-30 days')` in the pruning query.
Every extractor must normalize to ISO 8601 UTC before INSERT.
"""

import calendar
from datetime import datetime, timezone


def normalize_date(parsed_struct) -> str:
    """Convert a feedparser *_parsed time.struct_time to an ISO 8601 UTC string.

    Falls back to the current time if the struct is missing (better than
    storing an unsortable raw string, and better than crashing ingestion
    on a single malformed feed entry).
    """
    if parsed_struct:
        try:
            ts = calendar.timegm(parsed_struct)
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    return datetime.now(timezone.utc).isoformat()


def insert_or_ignore_many(conn, table: str, columns: tuple, rows: list, chunk_size: int = 100) -> None:
    """Multi-row INSERT OR IGNORE: one round trip per chunk instead of one
    per row (the Turso driver's executemany is deliberately iterative).
    `table` and `columns` must be trusted constants, never user input.
    len(columns) * chunk_size must stay under SQLite's bound-variable limit
    (999 on older builds)."""
    if not rows:
        return
    assert len(columns) * chunk_size <= 999, "chunk too large for SQLite variable limit"
    cols = ", ".join(columns)
    group = "(" + ",".join("?" * len(columns)) + ")"
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]
        conn.execute(
            f"INSERT OR IGNORE INTO {table} ({cols}) VALUES {','.join([group] * len(chunk))}",
            tuple(v for row in chunk for v in row),
        )
