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

def ingest_feed(conn, feed_url: str, source_name: str, module_type: str) -> int:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    response = requests.get(feed_url, headers=headers, timeout=15)
    response.raise_for_status()
    
    feed = feedparser.parse(response.content)
    inserted = 0
    
    for entry in feed.entries:
        title = entry.get("title", "Untitled")
        url = entry.get("link", "")
        item_id = entry.get("id", url)
        published_date = _parse_date(entry)
        
        try:
            conn.execute(
                "INSERT OR IGNORE INTO rss_cache (item_id, source_name, module_type, title, url, published_date) VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, source_name, module_type, title, url, published_date)
            )
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                inserted += 1
        except Exception as e:
            print(f"Skipping {item_id}: {e}")
            
    return inserted