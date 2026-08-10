import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
SOURCES_PATH = BASE_DIR / "sources.json"
SECRETS_PATH = BASE_DIR / "data" / "secrets.json"

# Public Invidious instances rotate and die frequently. This default list
# will go stale — before relying on search, check https://api.invidious.io/
# for currently healthy instances and override via INVIDIOUS_INSTANCES in .env.
DEFAULT_INVIDIOUS_INSTANCES = [
    "https://vid.puffyan.us",
    "https://invidious.privacyredirect.com",
    "https://yewtu.be",
    "https://inv.nadeko.net",
]


def load_sources() -> dict:
    with open(SOURCES_PATH) as f:
        return json.load(f)


def _load_secrets() -> dict:
    if SECRETS_PATH.exists():
        with open(SECRETS_PATH) as f:
            return json.load(f)
    return {}


def _save_secrets(data: dict) -> None:
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SECRETS_PATH, "w") as f:
        json.dump(data, f)


def get_substack_cookie() -> str:
    """File-backed value takes precedence — this is what the admin endpoint
    updates at runtime without a server restart (Phase 2 correction)."""
    secrets = _load_secrets()
    return secrets.get("substack_cookie") or os.environ.get("SUBSTACK_COOKIE", "")


def set_substack_cookie(value: str) -> None:
    secrets = _load_secrets()
    secrets["substack_cookie"] = value
    _save_secrets(secrets)


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
