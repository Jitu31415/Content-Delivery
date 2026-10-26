import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app import cache, config, reader
from app.database import db_session
from app.extractors import generic_rss, substack, youtube

logger = logging.getLogger("worker")

_scheduler = None

# Retention for tables not covered by generic_rss.RETENTION_DAYS.
YOUTUBE_RETENTION_DAYS = 365
SUBSTACK_RETENTION_DAYS = 365

# (sources.json key, rss_cache.module_type)
RSS_GROUPS = (("news", "News"), ("papers", "Paper"), ("blogs", "Blog"))
FETCH_WORKERS = 6

LOCK_KEY = "maintenance_lock"
LOCK_STALE_MINUTES = 30


# ---------------------------------------------------------------- run lock
# Stored in system_state as "<iso timestamp>|<token>". The timestamp prefix
# lets the upsert's WHERE clause compare lexicographically against a stale
# cutoff; an empty string (released) always compares as stale. Ownership is
# verified by reading the value back, not by rowcount, because rowcount is
# not reliable through the Turso driver.

def _acquire_lock() -> str | None:
    token = secrets.token_hex(8)
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(minutes=LOCK_STALE_MINUTES)).isoformat()
    value = f"{now.isoformat()}|{token}"
    with db_session() as conn:
        conn.execute(
            "INSERT INTO system_state (key, val) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET val = excluded.val "
            "WHERE system_state.val < ?",
            (LOCK_KEY, value, stale_before),
        )
        row = conn.execute(
            "SELECT val FROM system_state WHERE key = ?", (LOCK_KEY,)
        ).fetchone()
    return token if row and row["val"].endswith(f"|{token}") else None


def _release_lock(token: str) -> None:
    try:
        with db_session() as conn:
            conn.execute(
                "UPDATE system_state SET val = '' WHERE key = ? AND val LIKE ?",
                (LOCK_KEY, f"%|{token}"),
            )
    except Exception:
        # Not fatal: the lock expires after LOCK_STALE_MINUTES regardless.
        logger.exception("failed to release maintenance lock")


# ---------------------------------------------------------------- cycle

def run_cycle() -> dict:
    """Entry point for both the cron route and /admin/run-worker.
    Refuses to start if another cycle holds the lock."""
    token = _acquire_lock()
    if token is None:
        return {"status": "skipped", "reason": "another ingest cycle is running"}
    try:
        return _run_cycle_locked()
    finally:
        _release_lock(token)


def _load_known_ids() -> tuple[set, set, set]:
    """One short session, ids only. Passed to the fetchers so they skip what
    is already stored: no Shorts probe, no full-article fetch, no insert."""
    with db_session() as conn:
        yt = {r["video_id"] for r in conn.execute("SELECT video_id FROM youtube_cache").fetchall()}
        yt |= {r["video_id"] for r in conn.execute("SELECT video_id FROM youtube_skip").fetchall()}
        sub = {r["article_id"] for r in conn.execute("SELECT article_id FROM substack_cache").fetchall()}
        # Only rows that already hold a summary: legacy NULL rows must be fetched
        # again so they can be backfilled.
        rss = {r["item_id"] for r in conn.execute(
            "SELECT item_id FROM rss_cache WHERE summary IS NOT NULL").fetchall()}
    return yt, sub, rss


def _build_jobs(sources: dict) -> list[tuple]:
    """(kind, arg1, arg2). youtube: (channel_id, category); substack:
    (author, None); rss kinds: (module_type, feed dict)."""
    jobs = []
    for category, channel_ids in sources.get("youtube", {}).items():
        for cid in channel_ids:
            jobs.append(("youtube", cid, category))
    for author in sources.get("substack", []):
        jobs.append(("substack", author, None))
    for key, module_type in RSS_GROUPS:
        for feed in sources.get(key, []):
            jobs.append((key, module_type, feed))
    return jobs


def _label(job: tuple) -> str:
    kind, a, b = job
    if kind == "youtube":
        return f"youtube:{a}"
    if kind == "substack":
        return f"substack:{a}"
    return f"{kind}:{b.get('name')}"


def _fetch_all(jobs: list[tuple], yt_known: set, sub_known: set, rss_known: set,
               cookie: str) -> list[tuple]:
    """Network + parse for every source, concurrently, with NO database
    connection open. Returns [(job, payload | None, error | None), ...].
    The known-id sets are shared read-only across threads."""
    def fetch(job):
        kind, a, b = job
        try:
            if kind == "youtube":
                payload = youtube.fetch_channel_feed(a, yt_known)
            elif kind == "substack":
                meta = {}
                articles = substack.fetch_feed(a, cookie, sub_known, meta)
                payload = (articles, meta.get("host", ""))
            else:
                payload = generic_rss.fetch_feed_records(b["url"], b["name"], a, rss_known)
            return job, payload, None
        except Exception as e:
            return job, None, e

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        return list(pool.map(fetch, jobs))


def _has_data(job: tuple, payload) -> bool:
    kind = job[0]
    if not payload:
        return False
    if kind == "youtube":
        videos, shorts = payload
        return bool(videos or shorts)
    if kind == "substack":
        articles, host = payload
        # A changed feed host (custom domain) needs a write even with no new posts.
        return bool(articles) or reader.substack_host_changed(job[1], host)
    return True


def _run_cycle_locked() -> dict:
    sources = config.load_sources()
    summary = {"youtube": 0, "substack": 0, "news": 0, "papers": 0, "blogs": 0, "errors": []}

    yt_known, sub_known, rss_known = _load_known_ids()
    cookie = config.get_substack_cookie()
    jobs = _build_jobs(sources)
    fetched = _fetch_all(jobs, yt_known, sub_known, rss_known, cookie)

    for job, _payload, err in fetched:
        if err is not None:
            summary["errors"].append(f"{_label(job)}: {err}")

    to_store = [(job, payload) for job, payload, err in fetched
                if err is None and _has_data(job, payload)]
    if to_store:
        try:
            with db_session() as conn:
                for job, payload in to_store:
                    kind, a, b = job
                    try:
                        if kind == "youtube":
                            videos, shorts = payload
                            summary["youtube"] += youtube.store_videos(conn, videos, shorts, b)
                        elif kind == "substack":
                            articles, host = payload
                            summary["substack"] += substack.store_articles(conn, articles)
                            reader.record_substack_host(conn, a, host)
                        else:
                            summary[kind] += generic_rss.store_records(conn, payload, a)
                    except Exception as e:
                        summary["errors"].append(f"{_label(job)}: store failed: {e}")
        except Exception as e:
            summary["errors"].append(f"write session: {e}")

    with db_session() as conn:
        summary["pruned"] = _prune(conn)
        conn.execute(
            "INSERT INTO worker_log (run_at, status, detail) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(),
             "ok" if not summary["errors"] else "partial", str(summary)),
        )

    cache.clear()   # list pages must show what this cycle just stored
    logger.info("worker cycle complete: %s", summary)
    return summary


def _prune(conn) -> dict:
    """Retention comes from generic_rss.RETENTION_DAYS so that ingest and
    prune can never disagree about what is worth keeping."""
    def deleted(cur) -> int:
        return max(getattr(cur, "rowcount", 0) or 0, 0)  # driver may report -1

    counts = {}

    rss_total = 0
    for module_type, days in generic_rss.RETENTION_DAYS.items():
        cur = conn.execute(
            "DELETE FROM rss_cache WHERE module_type = ? "
            "AND published_date < date('now', ?) AND is_saved = 0",
            (module_type, f"-{days} days"),
        )
        rss_total += deleted(cur)
    counts["rss_cache"] = rss_total

    cur = conn.execute(
        "DELETE FROM youtube_cache WHERE published_date < date('now', ?) AND is_saved = 0",
        (f"-{YOUTUBE_RETENTION_DAYS} days",),
    )
    counts["youtube_cache"] = deleted(cur)

    # Shorts verdicts only need to outlive the feed window (a feed carries
    # its ~15 most recent entries); the same horizon as youtube_cache is ample.
    cur = conn.execute(
        "DELETE FROM youtube_skip WHERE seen_at < date('now', ?)",
        (f"-{YOUTUBE_RETENTION_DAYS} days",),
    )
    counts["youtube_skip"] = deleted(cur)

    cur = conn.execute(
        "DELETE FROM substack_cache WHERE published_date < date('now', ?) AND is_saved = 0",
        (f"-{SUBSTACK_RETENTION_DAYS} days",),
    )
    counts["substack_cache"] = deleted(cur)

    cur = conn.execute("DELETE FROM read_items WHERE read_at < date('now', ?)", (f"-{YOUTUBE_RETENTION_DAYS} days",))
    counts["read_items"] = deleted(cur)

    return counts


def start_scheduler():
    """Unused on Render (cron-triggered). Imported lazily so APScheduler is
    not a hard dependency of the web process."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        run_cycle, "interval",
        hours=config.get_worker_interval_hours(),
        id="ingest_cycle",
        next_run_time=datetime.now(),  # run once immediately on startup
    )
    _scheduler.start()
    return _scheduler
