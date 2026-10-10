"""
Generic RSS/Atom ingestion, split into two phases so that no database
connection is open during network I/O:

    records = fetch_feed_records(url, name, module_type)   # network + parse
    with db_session() as conn:
        store_records(conn, records, module_type)          # DB only

Retention invariant
-------------------
RETENTION_DAYS is the single source of truth for how long each module_type
is kept. worker._prune() deletes by it, and fetch_feed_records() refuses to
return entries older than it. If ingest accepted older entries than prune
keeps, every cycle would insert them and then delete them again.

The prune compares against date('now', '-N days') (midnight, date-only
string); the ingest cutoff is the full timestamp N days ago. A full
timestamp on the same date sorts after the date-only prefix, so everything
ingested is guaranteed to survive the next prune.
"""

import calendar
from datetime import datetime, timedelta, timezone

import feedparser
import requests

RETENTION_DAYS = {"News": 1, "Paper": 365, "Blog": 365}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# 6 bound parameters per row; 100 rows = 600 variables, below SQLite's
# historical 999-variable limit.
_INSERT_CHUNK = 100


def retention_cutoff_iso(module_type: str) -> str:
    days = RETENTION_DAYS[module_type]
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _parse_date(entry) -> str:
    """Extracts and normalizes the timestamp using UTC-safe conversions."""
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct:
        try:
            dt = datetime.fromtimestamp(calendar.timegm(time_struct), timezone.utc)
            return dt.isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def fetch_feed_records(feed_url: str, source_name: str, module_type: str) -> list[tuple]:
    """Network + parse only. Returns rows shaped for rss_cache:
    (item_id, source_name, module_type, title, url, published_date),
    already restricted to the module's retention window."""
    response = requests.get(feed_url, headers=_HEADERS, timeout=(5, 15))
    response.raise_for_status()
    feed = feedparser.parse(response.content)

    cutoff = retention_cutoff_iso(module_type)
    records = []
    for entry in feed.entries:
        published = _parse_date(entry)
        if published < cutoff:
            continue
        url = entry.get("link", "")
        item_id = entry.get("id") or url
        if not item_id:
            continue
        records.append((item_id, source_name, module_type,
                        entry.get("title", "Untitled"), url, published))
    return records


def store_records(conn, records: list[tuple], module_type: str) -> int:
    """DB only. One SELECT to learn what is already stored, then multi-row
    INSERTs for the remainder: in steady state this is one round trip and
    zero inserts per feed.

    The returned count is the number of rows submitted for insert. It is an
    upper bound: INSERT OR IGNORE would still drop a row whose item_id
    collides with one from another source."""
    if not records:
        return 0
    source_name = records[0][1]
    cutoff = retention_cutoff_iso(module_type)
    existing = {
        r["item_id"]
        for r in conn.execute(
            "SELECT item_id FROM rss_cache WHERE source_name = ? AND published_date >= ?",
            (source_name, cutoff),
        ).fetchall()
    }
    new = [r for r in records if r[0] not in existing]
    for i in range(0, len(new), _INSERT_CHUNK):
        chunk = new[i:i + _INSERT_CHUNK]
        placeholders = ",".join(["(?,?,?,?,?,?)"] * len(chunk))
        conn.execute(
            "INSERT OR IGNORE INTO rss_cache "
            "(item_id, source_name, module_type, title, url, published_date) "
            f"VALUES {placeholders}",
            tuple(v for row in chunk for v in row),
        )
    return len(new)
