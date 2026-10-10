import json
import os
from pathlib import Path

from app.database import db_session

BASE_DIR = Path(__file__).parent.parent
SOURCES_PATH = BASE_DIR / "sources.json"

# Public Invidious instances rotate and die frequently. This default list
# will go stale — before relying on search, check https://api.invidious.io/
# for currently healthy instances and override via INVIDIOUS_INSTANCES in .env.
# (vid.puffyan.us was removed: that instance is no longer operating, so every
# search without a working API key paid its full timeout before moving on.)
DEFAULT_INVIDIOUS_INSTANCES = [
    "https://invidious.privacyredirect.com",
    "https://yewtu.be",
    "https://inv.nadeko.net",
]

_COOKIE_KEY = "substack_cookie"


def load_sources() -> dict:
    with open(SOURCES_PATH) as f:
        return json.load(f)


def get_substack_cookie() -> str:
    """The database-backed value takes precedence — this is what the admin
    endpoint updates at runtime without a server restart. It lives in the
    database rather than a file because the container's disk is ephemeral:
    a file written by the admin endpoint is gone after every deploy or
    respawn, silently reverting to the (possibly expired) env value."""
    try:
        with db_session() as conn:
            row = conn.execute(
                "SELECT val FROM system_state WHERE key = ?", (_COOKIE_KEY,)
            ).fetchone()
        if row and row["val"]:
            return row["val"]
    except Exception:
        pass  # DB unreachable: fall back to the env value rather than fail ingestion
    return os.environ.get("SUBSTACK_COOKIE", "")


def set_substack_cookie(value: str) -> None:
    with db_session() as conn:
        conn.execute(
            "INSERT INTO system_state (key, val) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET val = excluded.val",
            (_COOKIE_KEY, value),
        )


def get_admin_token() -> str:
    return os.environ.get("ADMIN_TOKEN", "")


def get_invidious_instances() -> list[str]:
    env_val = os.environ.get("INVIDIOUS_INSTANCES")
    if env_val:
        return [i.strip() for i in env_val.split(",") if i.strip()]
    return DEFAULT_INVIDIOUS_INSTANCES


def get_youtube_api_key() -> str:
    """No hardcoded default, deliberately. If unset, search falls back to
    the Invidious + heuristic-filter path automatically — see
    extractors/youtube.py:search_educational()."""
    return os.environ.get("YOUTUBE_API_KEY", "")


def get_search_cache_ttl_hours() -> int:
    return int(os.environ.get("SEARCH_CACHE_TTL_HOURS", "6"))


def get_worker_interval_hours() -> int:
    return int(os.environ.get("WORKER_INTERVAL_HOURS", "6"))


def get_prune_days() -> int:
    return int(os.environ.get("PRUNE_DAYS", "30"))
