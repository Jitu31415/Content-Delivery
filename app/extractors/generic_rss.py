import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
import email.utils

def _parse_date(date_str: str) -> str:
    """Translates both RSS (RFC 822) and Atom (ISO 8601) timestamps into universal ISO 8601 UTC."""
    try:
        # Parse traditional RSS dates
        dt = email.utils.parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        try:
            # Parse strict Atom dates
            return datetime.fromisoformat(date_str.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()

def ingest_feed(conn, feed_url: str, source_name: str, module_type: str) -> int:
    # 1. Defeat 403 Forbidden blocks via User-Agent spoofing
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    response = requests.get(feed_url, headers=headers, timeout=15)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.content, "xml")
    
    # 2. Target both RSS and Atom payload blocks
    articles = soup.find_all(["item", "entry"])
    inserted = 0
    
    for article in articles:
        title_tag = article.find("title")
        title = title_tag.text.strip() if title_tag else "Untitled"
        
        # Atom often includes multiple link tags; prefer the alternate HTML link
        link_tag = article.find("link", rel="alternate") or article.find("link")
        url = ""
        if link_tag:
            url = link_tag.get("href") if link_tag.has_attr("href") else link_tag.text.strip()
            
        date_tag = article.find(["pubDate", "published", "updated"])
        date_text = date_tag.text.strip() if date_tag else ""
        published_date = _parse_date(date_text)
            
        id_tag = article.find(["guid", "id"])
        item_id = id_tag.text.strip() if id_tag else url
        
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