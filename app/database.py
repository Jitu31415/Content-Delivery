import os
import sqlite3
import libsql  # switched from libsql_experimental: current Turso docs point to this
                # package now. Not the cause of the populate bug below — both
                # packages handle rowcount/executescript identically — but
                # libsql_experimental is pre-1.0 and no longer the documented path.
from contextlib import contextmanager

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# Bump this whenever init_db()'s DDL, migrations or indexes change. init_db()
# is skipped entirely (two cheap statements) when the stored version matches,
# because on Turso the schema persists across container restarts and
# re-running ~20 remote DDL statements on every cold start delays port
# binding (uvicorn does not listen until lifespan startup returns).
SCHEMA_VERSION = 3


def dict_factory(cursor, row):
    fields = [column[0] for column in cursor.description]
    return {key: value for key, value in zip(fields, row)}

class TursoDictCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def rowcount(self):
        """Exposes the number of rows modified by the last query."""
        return getattr(self.cursor, 'rowcount', -1)

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict_factory(self.cursor, row) if row else None

    def fetchall(self):
        return [dict_factory(self.cursor, row) for row in self.cursor.fetchall()]

class TursoDictConnection:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=None):
        if params is None:
            return TursoDictCursor(self.conn.execute(query))
        return TursoDictCursor(self.conn.execute(query, params))

    def executemany(self, query, seq_of_params):
        """Bypasses remote bulk-insert driver bugs by forcing iterative atomic executions.
        Prefer app.utils.insert_or_ignore_many() for inserts: it sends one
        multi-row statement per chunk instead of one round trip per row."""
        for params in seq_of_params:
            self.conn.execute(query, params)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()

@contextmanager
def db_session():
    """Universal session used by main.py, worker.py, and all extractors."""
    if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
        raw_conn = libsql.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        conn = TursoDictConnection(raw_conn)
    else:
        conn = sqlite3.connect("app.db")
        conn.row_factory = dict_factory

    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def _ensure_columns(conn, table, column_defs):
    """Adds any column in `column_defs` that's missing from an
    already-existing table. Necessary because CREATE TABLE IF NOT EXISTS is
    a no-op against a table that already exists — it does NOT reconcile a
    changed schema, so on your live Turso database (which already had
    youtube_cache/substack_cache from before this fix), the corrected
    CREATE TABLE statements below never touched the existing tables. This
    is what ran on every startup and actually fixed it. Safe to call every
    time init_db() runs: does nothing once a column is already present.
    column_defs: list of (column_name, sql_type_declaration) tuples.
    """
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in cur.fetchall()}
    for name, decl in column_defs:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _schema_is_current(conn) -> bool:
    row = conn.execute("SELECT val FROM system_state WHERE key = 'schema_version'").fetchone()
    return bool(row) and row["val"] == str(SCHEMA_VERSION)


def init_db():
    """Creates/migrates the schema, unless the stored schema_version already
    matches SCHEMA_VERSION, in which case it returns after two statements."""
    with db_session() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS system_state (key TEXT UNIQUE, val TEXT)")
        if _schema_is_current(conn):
            return

        conn.execute("""
            CREATE TABLE IF NOT EXISTS rss_cache (
                item_id TEXT PRIMARY KEY, module_type TEXT,
                source_name TEXT, published_date TEXT,
                url TEXT, title TEXT, is_saved INTEGER DEFAULT 0, summary TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS youtube_cache (
                video_id TEXT PRIMARY KEY, title TEXT,
                channel_name TEXT, category TEXT, published_date TEXT,
                thumbnail_url TEXT, is_saved INTEGER DEFAULT 0
            )
        """)
        # thumbnail_url was missing above — youtube.py's ingestion has
        # always inserted into it. That's the actual populate failure: every
        # INSERT OR IGNORE into youtube_cache raised "table youtube_cache has
        # no column named thumbnail_url" and was silently swallowed by
        # worker.py's per-source try/except, so it looked like ingestion was
        # just quietly doing nothing rather than erroring loudly.

        conn.execute("""
            CREATE TABLE IF NOT EXISTS substack_cache (
                article_id TEXT PRIMARY KEY, title TEXT,
                author TEXT, published_date TEXT, url TEXT,
                content_html TEXT, is_saved INTEGER DEFAULT 0
            )
        """)
        # Same bug, same shape: content_html and url were missing — every
        # insert from substack.py failed identically.

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT,
                title TEXT, accessed_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT,
                status TEXT,
                detail TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_cache (
                cache_key TEXT PRIMARY KEY, query TEXT, course_mode INTEGER,
                results_json TEXT, backend TEXT, cached_at TEXT
            )
        """)
        # This table didn't exist at all in the version you uploaded —
        # youtube.py's get_cached_search()/set_cached_search() both query it
        # unconditionally, so /youtube/search would fail with "no such
        # table: search_cache" the moment someone used the search bar. Same
        # class of bug as the two above: schema drifted behind the code that
        # depends on it.

        conn.execute("""
            CREATE TABLE IF NOT EXISTS youtube_skip (
                video_id TEXT PRIMARY KEY, seen_at TEXT
            )
        """)
        # Verdict cache for the Shorts probe: ids that were identified as
        # Shorts and deliberately not stored in youtube_cache. Without it,
        # every Short in a channel's feed would be re-probed every cycle
        # because it never appears in youtube_cache.

        # Courses (see app/courses.py). Everything is keyed by device_id, the cookie
        # that already scopes the history tab, so progress is visible only from the
        # device that made it.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                device_id TEXT, playlist_id TEXT, title TEXT, channel TEXT,
                thumb_video TEXT, total INTEGER, added_at TEXT,
                PRIMARY KEY (device_id, playlist_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS course_videos (
                device_id TEXT, playlist_id TEXT, position INTEGER, video_id TEXT, title TEXT,
                PRIMARY KEY (device_id, playlist_id, position)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS course_progress (
                device_id TEXT, playlist_id TEXT, video_id TEXT, completed_at TEXT,
                PRIMARY KEY (device_id, playlist_id, video_id)
            )
        """)

        # --- migration pass: reconciles tables that already existed before
        # these fixes, on your live Turso database, with a schema that had
        # already drifted out from under them.
        _ensure_columns(conn, "youtube_cache", [("thumbnail_url", "TEXT")])
        # summary: sanitised feed text shown on /item. NULL = stored before the
        # column existed (backfilled by generic_rss.store_records); '' = the feed
        # has no text for that item.
        _ensure_columns(conn, "rss_cache", [("summary", "TEXT")])
        _ensure_columns(conn, "substack_cache", [("content_html", "TEXT"), ("url", "TEXT")])
        _ensure_columns(conn, "user_history", [("device_id", "TEXT")])
        # Per-device history scoping. Existing rows predate this column and
        # get NULL — they won't match any real device_id filter, so they
        # won't appear in the per-device history tab. There's no way to
        # retroactively attribute old entries to a device that was never
        # recorded, so this is the honest outcome, not a bug.

        # --- indexes: every list page is "WHERE <key> ORDER BY published_date
        # DESC LIMIT n", which without these is a full scan plus a sort.
        conn.execute("CREATE INDEX IF NOT EXISTS ix_yt_cat_date ON youtube_cache(category, published_date DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_rss_mod_date ON rss_cache(module_type, published_date DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_sub_date ON substack_cache(published_date DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_hist_dev ON user_history(device_id, accessed_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_cv_video ON course_videos(device_id, playlist_id, video_id)")

        # Written last, inside the same session: a failure above leaves the
        # version unset, so the next start retries the whole pass.
        conn.execute(
            "INSERT INTO system_state (key, val) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET val = excluded.val",
            (str(SCHEMA_VERSION),),
        )
