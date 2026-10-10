"""
Substack ingestion, split into two phases so no database connection is open
during network I/O:

    articles = fetch_feed(author, cookie, known_ids)   # network + parse
    with db_session() as conn:
        store_articles(conn, articles)                 # DB only

Per entry:

1. Pull the RSS feed. Free-tier and preview content arrives in full.
2. If the entry is NEW and looks paywall-truncated, retry with the stored
   session cookie against the article's own page and extract the full body.
3. Sanitize to the minimalist tag set (p, h1, h2, a) before storage —
   dropping scripts, images, and tracking tags per Phase 2 structural-bloat
   correction.

Entries whose id is already stored are skipped before any of that. Storage
is INSERT OR IGNORE, so refetching a stored article's full page could never
change what is stored; it only cost a request per entry per cycle.

If the cookie has expired, `_fetch_full_article` returns None and the
snippet is stored instead — silent degradation, not a crash, matching the
spec's stated failure mode. Note the consequence: a snippet stored while the
cookie was bad is not upgraded later when the cookie is fixed.
"""

import feedparser
import httpx
from bs4 import BeautifulSoup

from app.utils import insert_or_ignore_many, normalize_date

ALLOWED_TAGS = {"p", "h1", "h2", "a"}
TRUNCATION_MARKERS = (
    "subscribe to continue reading",
    "this post is for paid subscribers",
    "continue reading",
)

# httpx's default User-Agent is commonly rejected by Cloudflare-fronted hosts.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


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
        resp = httpx.get(
            url,
            headers={**_HEADERS, "Cookie": f"substack.sid={cookie}"},
            timeout=10.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        body = soup.select_one("div.available-content") or soup.select_one("article")
        return str(body) if body else None
    except httpx.HTTPError:
        return None


def fetch_feed(author: str, cookie: str = "", known_ids=frozenset()) -> list[dict]:
    """Network + parse only. Returns only articles whose id is not in
    `known_ids`. Raises on HTTP errors so a bad author slug appears in the
    worker summary instead of silently yielding an empty feed."""
    clean_author = author.strip("/")
    if "." in clean_author:
        url = f"https://{clean_author}/feed"
    else:
        url = f"https://{clean_author}.substack.com/feed"
    resp = httpx.get(url, headers=_HEADERS, timeout=10.0, follow_redirects=True)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)

    articles = []
    for entry in parsed.entries:
        article_id = entry.get("id") or entry.get("link")
        if not article_id or article_id in known_ids:
            continue
        content_blocks = entry.get("content") or []
        # Fall back to the summary when the entry carries no content block
        # (the previous `entry.get("content", [{}])` default made that
        # fallback unreachable).
        raw_html = (content_blocks[0].get("value") if content_blocks else "") or entry.get("summary", "")
        if _looks_truncated(raw_html):
            full = _fetch_full_article(entry.get("link"), cookie)
            if full:
                raw_html = full
        articles.append({
            "article_id": article_id,
            "author": author,
            "title": entry.get("title", "Untitled"),
            "content_html": sanitize_html(raw_html),
            "published_date": normalize_date(entry.get("published_parsed")),
            "url": entry.get("link", ""),
        })
    return articles


def store_articles(conn, articles: list[dict]) -> int:
    """DB only. Returns the number of rows submitted. Chunks are small
    because content_html can be large."""
    insert_or_ignore_many(
        conn, "substack_cache",
        ("article_id", "author", "title", "content_html", "published_date", "url"),
        [(a["article_id"], a["author"], a["title"], a["content_html"],
          a["published_date"], a["url"]) for a in articles],
        chunk_size=20,
    )
    return len(articles)
