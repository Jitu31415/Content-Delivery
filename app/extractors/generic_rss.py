"""
Generic RSS/Atom ingestion, split into two phases so that no database
connection is open during network I/O or parsing:

    records = fetch_feed_records(url, name, module_type, known_ids)  # network + parse
    with db_session() as conn:
        store_records(conn, records, module_type)                    # DB only

Each record carries a `summary`: the feed's own description or full content,
reduced to a small set of text tags (see sanitize_summary). The app shows it on
its own /item page, so an item can be read without leaving the site at all.

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

Summary lifecycle
-----------------
* New items: summary stored on insert ('' if the feed offers none).
* Legacy rows (summary IS NULL, stored before this column existed): filled in
  once, when they next appear in a feed. '' is stored when the feed has no
  text, so they are not retried forever.
* Items already holding a summary (NOT NULL) are passed in as `known_ids` and
  skipped before any HTML parsing, so steady-state cycles parse nothing.
"""

import calendar
import html as _html
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup
from bs4.element import (CData, Comment, Declaration, Doctype, NavigableString,
                         ProcessingInstruction)

RETENTION_DAYS = {"News": 1, "Paper": 365, "Blog": 365}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_SUMMARY_CHARS = 50_000

# Statement-size limits: a multi-row INSERT carrying 100 full-text posts would
# be megabytes in one remote request.
_INSERT_MAX_ROWS = 100
_INSERT_MAX_BYTES = 200_000
_UPDATE_MAX_ROWS = 10

_SUMMARY_TAGS = {"p", "br", "em", "strong", "b", "i", "ul", "ol", "li", "blockquote",
                 "h2", "h3", "h4", "code", "pre", "sub", "sup", "hr"}
_HEADING_MAP = {"h1": "h3", "h5": "h4", "h6": "h4"}
_SUMMARY_DROP = ["script", "style", "noscript", "iframe", "object", "embed", "svg", "form",
                 "button", "input", "select", "textarea", "img", "video", "audio", "canvas",
                 "picture", "source", "link", "meta", "template", "dialog", "head", "title"]
_NON_TEXT_NODES = (Comment, Doctype, CData, ProcessingInstruction, Declaration)


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


def sanitize_summary(raw: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """Reduce feed-supplied HTML (or plain text) to a closed set of text tags,
    with no attributes, links, images, scripts or styles. Safe to render with
    |safe; main.py runs it again at render time as defence in depth.
    Anchors are unwrapped (their text stays) so no outbound link survives."""
    if not raw or not raw.strip():
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(_SUMMARY_DROP):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if tag.name in _HEADING_MAP:
            tag.name = _HEADING_MAP[tag.name]
        if tag.name not in _SUMMARY_TAGS:
            tag.unwrap()
        else:
            tag.attrs = {}
    for node in list(soup.find_all(string=lambda s: isinstance(s, _NON_TEXT_NODES))):
        node.extract()
    if not soup.get_text(strip=True):
        return ""

    out, total = [], 0
    for node in soup.contents:
        # str() of a text node is its RAW characters, not escaped markup: a feed
        # carrying double-encoded HTML (&lt;script&gt;, common in RSS) would
        # otherwise come out as a live <script> tag. Tags serialise safely via str().
        piece = _html.escape(str(node), quote=False) if isinstance(node, NavigableString) else str(node)
        if len(piece) > max_chars:
            # One node alone exceeds the cap: keep its text only, then stop.
            piece = "<p>" + _html.escape(node.get_text(" ", strip=True)[:max_chars]) + "&hellip;</p>"
        if out and total + len(piece) > max_chars:
            out.append("<p><em>(truncated)</em></p>")
            break
        out.append(piece)
        total += len(piece)
        if total >= max_chars:
            out.append("<p><em>(truncated)</em></p>")
            break
    return "".join(out).strip()


def _entry_html(entry) -> str:
    """The longer of the entry's full content and its summary/description."""
    content = entry.get("content") or []
    best = max((c.get("value", "") or "" for c in content), key=len, default="")
    summary = entry.get("summary", "") or ""
    return best if len(best) >= len(summary) else summary


def fetch_feed_records(feed_url: str, source_name: str, module_type: str,
                       known_ids=frozenset()) -> list[tuple]:
    """Network + parse only. Returns rows shaped for rss_cache:
    (item_id, source_name, module_type, title, url, published_date, summary),
    restricted to the module's retention window and to entries NOT in
    `known_ids` (ids that already hold a summary), so nothing is sanitised
    twice."""
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
        if not item_id or item_id in known_ids:
            continue
        records.append((item_id, source_name, module_type,
                        entry.get("title", "Untitled"), url, published,
                        sanitize_summary(_entry_html(entry))))
    return records


def _chunks(rows, size_of, max_rows, max_bytes):
    chunk, total = [], 0
    for row in rows:
        size = size_of(row)
        if chunk and (len(chunk) >= max_rows or total + size > max_bytes):
            yield chunk
            chunk, total = [], 0
        chunk.append(row)
        total += size
    if chunk:
        yield chunk


def store_records(conn, records: list[tuple], module_type: str) -> int:
    """DB only. One SELECT to learn what is stored (and which legacy rows lack
    a summary), then size-bounded multi-row INSERTs for new rows and CASE
    UPDATEs for legacy rows.

    The returned count is the number of NEW rows submitted. It is an upper
    bound: INSERT OR IGNORE would still drop a row whose item_id collides
    with one from another source."""
    if not records:
        return 0
    source_name = records[0][1]
    cutoff = retention_cutoff_iso(module_type)
    existing = {
        r["item_id"]: bool(r["missing"])
        for r in conn.execute(
            "SELECT item_id, (summary IS NULL) AS missing FROM rss_cache "
            "WHERE source_name = ? AND published_date >= ?",
            (source_name, cutoff),
        ).fetchall()
    }
    new = [r for r in records if r[0] not in existing]
    legacy = [(r[0], r[6]) for r in records if existing.get(r[0])]

    for chunk in _chunks(new, lambda r: len(r[6]) + 500, _INSERT_MAX_ROWS, _INSERT_MAX_BYTES):
        placeholders = ",".join(["(?,?,?,?,?,?,?)"] * len(chunk))
        conn.execute(
            "INSERT OR IGNORE INTO rss_cache "
            "(item_id, source_name, module_type, title, url, published_date, summary) "
            f"VALUES {placeholders}",
            tuple(v for row in chunk for v in row),
        )

    for chunk in _chunks(legacy, lambda r: len(r[1]) + 200, _UPDATE_MAX_ROWS, _INSERT_MAX_BYTES):
        whens = " ".join(["WHEN ? THEN ?"] * len(chunk))
        ids = ",".join("?" * len(chunk))
        params = [v for row in chunk for v in row] + [row[0] for row in chunk]
        conn.execute(
            f"UPDATE rss_cache SET summary = CASE item_id {whens} END "
            f"WHERE item_id IN ({ids}) AND summary IS NULL",
            params,
        )
    return len(new)
