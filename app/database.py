import os
import sqlite3
import libsql_experimental as libsql
from contextlib import contextmanager

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

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
        """Bypasses remote bulk-insert driver bugs by forcing iterative atomic executions."""
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

def init_db():
    """Forces schema creation on the stateless container."""
    with db_session() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS system_state (key TEXT UNIQUE, val TEXT)")
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rss_cache (
                item_id TEXT PRIMARY KEY, module_type TEXT, 
                source_name TEXT, published_date TEXT, 
                url TEXT, title TEXT, is_saved INTEGER DEFAULT 0
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS youtube_cache (
                video_id TEXT PRIMARY KEY, title TEXT, 
                channel_name TEXT, category TEXT, published_date TEXT, 
                is_saved INTEGER DEFAULT 0
            )
        """)
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS substack_cache (
                article_id TEXT PRIMARY KEY, title TEXT, 
                author TEXT, published_date TEXT, is_saved INTEGER DEFAULT 0
            )
        """)
        
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