"""
Reading-side queries: per-device read state, "continue reading", saved items,
local search, and the home page's data.

Read state is per device (the same device_id cookie that scopes history and
courses). Saved items are global, as before: is_saved lives on the item row.
"""

import re
from datetime import datetime, timezone

from app.utils import insert_or_ignore_many

# Columns the list pages need. Never includes `summary` (tens of KB per row).
RSS_COLS = "item_id, module_type, source_name, published_date, url, title, is_saved"
KINDS = ("rss", "substack")

_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- read state

def mark_read(conn, device_id: str, kind: str, item_id: str) -> None:
    """The reader opened this item: counts as read and as 'recently opened'."""
    conn.execute(
        "INSERT INTO read_items (device_id, kind, item_id, read_at, opened) VALUES (?, ?, ?, ?, 1) "
        "ON CONFLICT(device_id, kind, item_id) DO UPDATE SET read_at = excluded.read_at, opened = 1",
        (device_id, kind, item_id, _now()),
    )


def mark_many(conn, device_id: str, kind: str, ids: list) -> int:
    """Bulk 'mark all read'. Items already read keep their timestamp, and bulk
    marks do not appear under 'continue reading' (opened = 0)."""
    now = _now()
    insert_or_ignore_many(
        conn, "read_items", ("device_id", "kind", "item_id", "read_at", "opened"),
        [(device_id, kind, i, now, 0) for i in ids], chunk_size=100,
    )
    return len(ids)


def read_set(conn, device_id: str, kind: str, ids: list) -> set:
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT item_id FROM read_items WHERE device_id = ? AND kind = ? AND item_id IN ({marks})",
        (device_id, kind, *ids),
    ).fetchall()
    return {r["item_id"] for r in rows}


def recent(conn, device_id: str, limit: int = 6) -> list:
    """Items this device opened most recently, newest first."""
    return conn.execute(
        "SELECT ri.kind AS kind, ri.item_id AS item_id, ri.read_at AS read_at, "
        "COALESCE(r.title, s.title) AS title, COALESCE(r.source_name, s.author) AS source "
        "FROM read_items ri "
        "LEFT JOIN rss_cache r ON ri.kind = 'rss' AND r.item_id = ri.item_id "
        "LEFT JOIN substack_cache s ON ri.kind = 'substack' AND s.article_id = ri.item_id "
        "WHERE ri.device_id = ? AND ri.opened = 1 AND COALESCE(r.title, s.title) IS NOT NULL "
        "ORDER BY ri.read_at DESC LIMIT ?",
        (device_id, limit),
    ).fetchall()


def unread_counts(conn, device_id: str) -> dict:
    """{"Paper": n, "Blog": n, "News": n, "substack": n} for this device."""
    counts = {"Paper": 0, "Blog": 0, "News": 0, "substack": 0}
    for r in conn.execute(
        "SELECT r.module_type AS t, COUNT(*) AS n FROM rss_cache r WHERE NOT EXISTS ("
        "SELECT 1 FROM read_items ri WHERE ri.device_id = ? AND ri.kind = 'rss' AND ri.item_id = r.item_id) "
        "GROUP BY r.module_type", (device_id,),
    ).fetchall():
        counts[r["t"]] = r["n"]
    counts["substack"] = conn.execute(
        "SELECT COUNT(*) AS n FROM substack_cache s WHERE NOT EXISTS ("
        "SELECT 1 FROM read_items ri WHERE ri.device_id = ? AND ri.kind = 'substack' AND ri.item_id = s.article_id)",
        (device_id,),
    ).fetchone()["n"]
    return counts


# ---------------------------------------------------------------- home / saved

def hub_global(conn) -> dict:
    latest = conn.execute(f"SELECT {RSS_COLS} FROM rss_cache ORDER BY published_date DESC LIMIT 8").fetchall()
    saved = conn.execute(
        "SELECT (SELECT COUNT(*) FROM rss_cache WHERE is_saved = 1) "
        "+ (SELECT COUNT(*) FROM substack_cache WHERE is_saved = 1) "
        "+ (SELECT COUNT(*) FROM youtube_cache WHERE is_saved = 1) AS n"
    ).fetchone()["n"]
    return {"latest": latest, "saved_count": saved}


def saved(conn, limit: int = 200) -> dict:
    return {
        "articles": conn.execute(
            f"SELECT {RSS_COLS} FROM rss_cache WHERE is_saved = 1 ORDER BY published_date DESC LIMIT ?",
            (limit,)).fetchall(),
        "substack": conn.execute(
            "SELECT article_id, author, title, published_date, url, is_saved FROM substack_cache "
            "WHERE is_saved = 1 ORDER BY published_date DESC LIMIT ?", (limit,)).fetchall(),
        "videos": conn.execute(
            "SELECT video_id, title, channel_name, thumbnail_url, published_date, is_saved FROM youtube_cache "
            "WHERE is_saved = 1 ORDER BY published_date DESC LIMIT ?", (limit,)).fetchall(),
    }


# ---------------------------------------------------------------- search

def search_terms(q: str) -> list:
    """Words of two or more characters, plus single digits ("lecture 3"); single
    letters are noise and are dropped. At most five terms."""
    return [w for w in re.split(r"\s+", (q or "").strip().lower()) if len(w) >= 2 or w.isdigit()][:5]


def _like(columns: list, terms: list):
    """Every term must match at least one column. LIKE wildcards in the user's
    text are escaped, so '50%' or 'a_b' search for those characters."""
    clause = " AND ".join(
        "(" + " OR ".join(f"lower({c}) LIKE ? ESCAPE '\\'" for c in columns) + ")" for _ in terms)
    params = [f"%{t.translate(_LIKE_ESCAPE)}%" for t in terms for _ in columns]
    return clause, params


def search(conn, q: str, deep: bool = False, limit: int = 30) -> dict:
    """Titles and sources by default (fast). deep=True also searches the stored
    text, which reads far more data, so it is opt-in."""
    terms = search_terms(q)
    out = {"articles": [], "substack": [], "videos": [], "terms": terms}
    if not terms:
        return out
    cols = ["title", "source_name"] + (["summary"] if deep else [])
    clause, params = _like(cols, terms)
    out["articles"] = conn.execute(
        f"SELECT {RSS_COLS} FROM rss_cache WHERE {clause} ORDER BY published_date DESC LIMIT ?",
        (*params, limit)).fetchall()
    cols = ["title", "author"] + (["content_html"] if deep else [])
    clause, params = _like(cols, terms)
    out["substack"] = conn.execute(
        "SELECT article_id, author, title, published_date, url, is_saved FROM substack_cache "
        f"WHERE {clause} ORDER BY published_date DESC LIMIT ?", (*params, limit)).fetchall()
    clause, params = _like(["title", "channel_name"], terms)
    out["videos"] = conn.execute(
        "SELECT video_id, title, channel_name, thumbnail_url, published_date, is_saved FROM youtube_cache "
        f"WHERE {clause} ORDER BY published_date DESC LIMIT ?", (*params, limit)).fetchall()
    return out
