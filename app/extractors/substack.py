"""
Substack ingestion, in three stages per entry:

1. Pull the RSS feed. Free-tier and preview content arrives in full.
2. If the entry looks paywall-truncated, retry with the stored session
   cookie against the article's own page and extract the full body.
3. Sanitize to the minimalist tag set (p, h1, h2, a) before storage —
   dropping scripts, images, and tracking tags per Phase 2 structural-bloat
   correction.

If the cookie has expired, `_fetch_full_article` returns None and the
snippet is stored instead — silent degradation, not a crash, matching the
spec's stated failure mode. The admin endpoint exists precisely to shorten
how long that degraded state persists.
"""

import feedparser
import httpx
from bs4 import BeautifulSoup

from app.utils import normalize_date

ALLOWED_TAGS = {"p", "h1", "h2", "a"}
TRUNCATION_MARKERS = (
    "subscribe to continue reading",
    "this post is for paid subscribers",
    "continue reading",
)


def sanitize_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    for tag in soup.find_all(True):
        if tag.name not in ALLOWED_TAGS:
            tag.unwrap()
    for tag in soup.find_all("a"):
        href = tag.get("href", "")
        tag.attrs = {"href": href} if href else {}
    return str(soup)


def _looks_truncated(html: str) -> bool:
    lowered = (html or "").lower()
    return any(m in lowered for m in TRUNCATION_MARKERS) or len(html or "") < 500


def _fetch_full_article(url: str, cookie: str) -> str | None:
    if not url or not cookie:
        return None
    try:
        resp = httpx.get(url, headers={"Cookie": f"substack.sid={cookie}"}, timeout=10.0)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        body = soup.select_one("div.available-content") or soup.select_one("article")
        return str(body) if body else None
    except httpx.HTTPError:
        return None


def fetch_feed(author: str, cookie: str = "") -> list[dict]:
    url = f"https://{author}.substack.com/feed"
    parsed = feedparser.parse(url)
    articles = []
    for entry in parsed.entries:
        content_blocks = entry.get("content", [{}])
        raw_html = content_blocks[0].get("value", "") if content_blocks else entry.get("summary", "")
        if _looks_truncated(raw_html):
            full = _fetch_full_article(entry.get("link"), cookie)
            if full:
                raw_html = full
        articles.append({
            "article_id": entry.get("id") or entry.get("link"),
            "author": author,
            "title": entry.get("title", "Untitled"),
            "content_html": sanitize_html(raw_html),
            "published_date": normalize_date(entry.get("published_parsed")),
            "url": entry.get("link", ""),
        })
    return articles


def ingest_author(conn, author: str, cookie: str) -> int:
    articles = fetch_feed(author, cookie)
    inserted = 0
    for a in articles:
        cur = conn.execute(
            "INSERT OR IGNORE INTO substack_cache "
            "(article_id, author, title, content_html, published_date, url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (a["article_id"], a["author"], a["title"], a["content_html"],
             a["published_date"], a["url"]),
        )
        inserted += cur.rowcount
    return inserted
