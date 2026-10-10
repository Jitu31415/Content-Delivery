import requests
import feedparser
import calendar
from datetime import datetime, timezone

def _parse_date(entry) -> str:
    """Extracts and normalizes the timestamp using UTC-safe conversions."""
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct:
        try:
            # timegm prevents local timezone bounds crashes
            dt = datetime.fromtimestamp(calendar.timegm(time_struct), timezone.utc)
            return dt.isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()

def ingest_feed(feed_url: str, source_name: str, module_type: str) -> int:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    
    # 1. Network execution
    response = requests.get(feed_url, headers=headers, timeout=10)
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    
    # 2. Memory compilation (Zero database locks)
    records = []
    for entry in feed.entries:
        title = entry.get("title", "Untitled")
        url = entry.get("link", "")
        item_id = entry.get("id", url)
        published_date = _parse_date(entry)
        
        records.append((item_id, source_name, module_type, title, url, published_date))
        
    if not records:
        return 0
        
    # 3. Single-Trip Database Execution (Lightning fast)
    try:
        with db_session() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO rss_cache (item_id, source_name, module_type, title, url, published_date) VALUES (?, ?, ?, ?, ?, ?)",
                records
            )
            # Calculate how many new rows were actually appended
            inserted = conn.execute("SELECT changes()").fetchone()[0]
            return inserted
    except Exception as e:
        print(f"Batch insert failed for {source_name}: {e}")
        return 0