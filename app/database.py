import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "data" / "app.db"

# WAL mode is required, not optional, given this architecture: the background
# worker writes on a multi-hour cycle while the web server reads concurrently.
# Default SQLite journal mode serializes writers against readers and will
# surface as intermittent "database is locked" errors under that pattern.
SCHEMA = """
CREATE TABLE IF NOT EXISTS youtube_cache (
    video_id        TEXT PRIMARY KEY,
    channel_name    TEXT NOT NULL,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL,
    published_date  TEXT NOT NULL,   -- ISO 8601 UTC, see app/utils.py
    thumbnail_url   TEXT,
    is_saved        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_youtube_category_date
    ON youtube_cache(category, published_date DESC);
CREATE INDEX IF NOT EXISTS idx_youtube_saved ON youtube_cache(is_saved);

CREATE TABLE IF NOT EXISTS user_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id     TEXT NOT NULL,
    title        TEXT NOT NULL,
    accessed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_accessed ON user_history(accessed_at DESC);

CREATE TABLE IF NOT EXISTS substack_cache (
    article_id      TEXT PRIMARY KEY,
    author          TEXT NOT NULL,
    title           TEXT NOT NULL,
    content_html    TEXT,
    published_date  TEXT NOT NULL,
    url             TEXT,
    is_saved        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_substack_date ON substack_cache(published_date DESC);
CREATE INDEX IF NOT EXISTS idx_substack_saved ON substack_cache(is_saved);

CREATE TABLE IF NOT EXISTS rss_cache (
    item_id         TEXT PRIMARY KEY,
    source_name     TEXT NOT NULL,
    module_type     TEXT NOT NULL,   -- 'News' | 'Paper'
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    published_date  TEXT NOT NULL,
    is_saved        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rss_type_date ON rss_cache(module_type, published_date DESC);
CREATE INDEX IF NOT EXISTS idx_rss_saved ON rss_cache(is_saved);

CREATE TABLE IF NOT EXISTS worker_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at   TEXT NOT NULL,
    status   TEXT NOT NULL,
    detail   TEXT
);

-- search.list on the official YouTube Data API costs 100 quota units per
-- call against a 10,000/day default quota (under 100 searches/day before
-- caching). This table makes repeat searches free until TTL expiry.
CREATE TABLE IF NOT EXISTS search_cache (
    cache_key     TEXT PRIMARY KEY,
    query         TEXT NOT NULL,
    course_mode   INTEGER NOT NULL,
    results_json  TEXT NOT NULL,
    backend       TEXT NOT NULL,   -- 'official' | 'invidious'
    cached_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_cache_cached_at ON search_cache(cached_at);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_session():
    """Context manager: commits on clean exit, always closes."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
