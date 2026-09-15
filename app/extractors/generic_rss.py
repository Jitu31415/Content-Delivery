import requests
import feedparser
from datetime import datetime, timezone
from time import mktime

def _parse_date(entry) -> str:
    """Extracts and normalizes the timestamp using feedparser's structured objects."""
    # feedparser standardizes dates into a struct_time object
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct:
        dt = datetime.fromtimestamp(mktime(time_struct), timezone.utc)
        return dt.isoformat()
    return datetime.now(timezone.utc).isoformat()

def ingest_feed(conn, feed_url: str, source_name: str, module_type: str) -> int:
    # 1. Defeat 403 Forbidden blocks via User-Agent spoofing
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    response = requests.get(feed_url, headers=headers, timeout=15)
    response.raise_for_status()
    
    # 2. Parse the raw byte string with feedparser
    feed = feedparser.parse(response.content)
    inserted = 0
    
    for entry in feed.entries:
        title = entry.get("title", "Untitled")
        url = entry.get("link", "")
        item_id = entry.get("id", url)
        published_date = _parse_date(entry)
        
        # 3. Database Execution
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