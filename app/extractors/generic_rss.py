import feedparser

from app.utils import normalize_date


def ingest_feed(conn, url: str, source_name: str, module_type: str) -> int:
    """module_type is 'News' or 'Paper' — shared table, filtered at query time."""
    parsed = feedparser.parse(url)
    inserted = 0
    for entry in parsed.entries:
        item_id = entry.get("id") or entry.get("link")
        if not item_id:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO rss_cache "
            "(item_id, source_name, module_type, title, url, published_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, source_name, module_type, entry.get("title", "Untitled"),
             entry.get("link", ""), normalize_date(entry.get("published_parsed"))),
        )
        inserted += cur.rowcount
    return inserted
