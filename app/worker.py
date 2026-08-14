import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app import config
from app.database import db_session
from app.extractors import generic_rss, substack, youtube

logger = logging.getLogger("worker")

_scheduler: BackgroundScheduler | None = None


def run_cycle() -> dict:
    """Structurally optimized ingest cycle. Database sessions are scoped per-source 
    to prevent write locks from spanning across blocking network operations."""
    sources = config.load_sources()
    summary = {"youtube": 0, "substack": 0, "news": 0, "papers": 0, "errors": []}

    # Isolate transaction scope per YouTube channel
    for category, channel_ids in sources.get("youtube", {}).items():
        for cid in channel_ids:
            try:
                with db_session() as conn:
                    summary["youtube"] += youtube.ingest_channel(conn, cid, category)
            except Exception as e:
                summary["errors"].append(f"youtube:{cid}: {e}")

    # Isolate transaction scope per Substack author
    cookie = config.get_substack_cookie()
    for author in sources.get("substack", []):
        try:
            with db_session() as conn:
                summary["substack"] += substack.ingest_author(conn, author, cookie)
        except Exception as e:
            summary["errors"].append(f"substack:{author}: {e}")

    # Isolate transaction scope per News feed
    for feed in sources.get("news", []):
        try:
            with db_session() as conn:
                summary["news"] += generic_rss.ingest_feed(conn, feed["url"], feed["name"], "News")
        except Exception as e:
            summary["errors"].append(f"news:{feed.get('name')}: {e}")

    # Isolate transaction scope per Papers feed
    for feed in sources.get("papers", []):
        try:
            with db_session() as conn:
                summary["papers"] += generic_rss.ingest_feed(conn, feed["url"], feed["name"], "Paper")
        except Exception as e:
            summary["errors"].append(f"papers:{feed.get('name')}: {e}")

    # Isolate pruning and logging
    with db_session() as conn:
        summary["pruned"] = _prune(conn)
        conn.execute(
            "INSERT INTO worker_log (run_at, status, detail) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(),
             "ok" if not summary["errors"] else "partial", str(summary)),
        )

    logger.info("worker cycle complete: %s", summary)
    return summary


def _prune(conn) -> dict:
    """Applies segmented TTLs based on content velocity."""
    counts = {}
    
    # 1. Ephemeral Content (News/RSS) -> 7 Day TTL
    cur_rss = conn.execute(
        "DELETE FROM rss_cache WHERE published_date < date('now', '-1 days') AND is_saved = 0"
    )
    counts["rss_cache"] = cur_rss.rowcount
    
    # 2. Evergreen Archive -> 365 Day TTL
    cur_yt = conn.execute(
        "DELETE FROM youtube_cache WHERE published_date < date('now', '-365 days') AND is_saved = 0"
    )
    counts["youtube_cache"] = cur_yt.rowcount
    
    cur_sub = conn.execute(
        "DELETE FROM substack_cache WHERE published_date < date('now', '-365 days') AND is_saved = 0"
    )
    counts["substack_cache"] = cur_sub.rowcount
    
    return counts


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        run_cycle, "interval",
        hours=config.get_worker_interval_hours(),
        id="ingest_cycle",
        next_run_time=datetime.now(),  # run once immediately on startup
    )
    _scheduler.start()
    return _scheduler
