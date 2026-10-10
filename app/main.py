import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before app.config reads any env vars below

from fastapi import FastAPI, Header, HTTPException, Request, Response, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import config, worker
from app.database import db_session, init_db
from app.extractors import youtube as youtube_extractor

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

DEVICE_ID_COOKIE = "device_id"
DEVICE_ID_MAX_AGE = 60 * 60 * 24 * 365 * 2


def _set_device_cookie(response: Response, device_id: str) -> None:
    response.set_cookie(
        DEVICE_ID_COOKIE, device_id,
        max_age=DEVICE_ID_MAX_AGE,
        httponly=True, samesite="lax",
    )


def _get_or_set_device_id(request: Request, response: Response) -> str:
    """History is scoped per-device via this cookie, not per-user-account —
    there's no login system here. httponly + 2-year expiry: it's an opaque
    identifier, not something the page's own JS needs to read.

    Only valid for routes that return a dict/model. FastAPI discards the
    injected `response` (and its cookies) when a route returns a Response
    object such as a TemplateResponse; those routes must call
    _set_device_cookie() on the response they actually return."""
    device_id = request.cookies.get(DEVICE_ID_COOKIE)
    if not device_id:
        device_id = secrets.token_hex(16)
        _set_device_cookie(response, device_id)
    return device_id


TABS = ["scientific", "music_poetry", "podcast", "religious"]

# module -> (table, primary key column). Used by the generic save-toggle route.
VALID_MODULES = {
    "youtube": ("youtube_cache", "video_id"),
    "substack": ("substack_cache", "article_id"),
    "rss": ("rss_cache", "item_id"),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # worker.start_scheduler() removed for Render serverless compatibility
    yield


app = FastAPI(title="Content Aggregator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ---------------------------------------------------------------- auth helper


def _token_ok(candidate: str) -> bool:
    expected = config.get_admin_token()
    if not expected or not candidate:
        return False
    # bytes: compare_digest raises TypeError on non-ASCII str
    return secrets.compare_digest(candidate.encode(), expected.encode())


# ---------------------------------------------------------------- session & cron

@app.post("/api/heartbeat")
def register_activity():
    """Registers an active session lock in the database."""
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, val) VALUES ('last_active', ?)",
            (now,),
        )
    return {"status": "locked"}


@app.get("/cron/maintenance")
def run_maintenance(
    background_tasks: BackgroundTasks,
    token: str = "",
    x_admin_token: str = Header(default=""),
):
    """External cron trigger. Dispatches ingestion to a background task.

    Prefer sending the token in an `X-Admin-Token` header: a `?token=` query
    string is written to the access log in clear text. The query form is
    still accepted so the existing cron job keeps working until it is
    reconfigured.

    Retention/pruning is NOT done here. worker.run_cycle() prunes using the
    per-module TTLs in generic_rss.RETENTION_DAYS; a second, shorter delete
    here would remove rows that ingestion then re-inserts every cycle.
    Overlapping runs are rejected inside run_cycle() by a DB-backed lock."""
    if not _token_ok(x_admin_token or token):
        raise HTTPException(status_code=403, detail="invalid or missing admin token")

    with db_session() as conn:
        # Check active session lock synchronously
        row = conn.execute("SELECT val FROM system_state WHERE key = 'last_active'").fetchone()
    if row and row["val"]:
        try:
            last_active = datetime.fromisoformat(row["val"])
        except ValueError:
            last_active = None
        if last_active and datetime.now(timezone.utc) - last_active < timedelta(minutes=2):
            return {"status": "aborted", "reason": "Active session detected. Database locked."}

    background_tasks.add_task(worker.run_cycle)
    return {"status": "Maintenance payload dispatched to background thread"}

# ---------------------------------------------------------------- hub

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def hub(request: Request):
    return templates.TemplateResponse(request, "hub.html", {})

# ---------------------------------------------------------------- youtube

@app.get("/youtube", response_class=HTMLResponse)
def youtube_home(request: Request, tab: str = "scientific", page: int = 1):
    if tab not in TABS:
        tab = TABS[0]
    limit, offset = 30, (page - 1) * 30
    with db_session() as conn:
        videos = conn.execute(
            "SELECT * FROM youtube_cache WHERE category = ? "
            "ORDER BY published_date DESC LIMIT ? OFFSET ?",
            (tab, limit, offset),
        ).fetchall()
    return templates.TemplateResponse(request, "youtube.html", {
        "videos": videos,
        "tabs": TABS, "active_tab": tab, "page": page,
        "search_results": None, "query": "", "course_mode": False,
        "error": None, "degraded": False,
    })

@app.get("/youtube/search", response_class=HTMLResponse)
def youtube_search(request: Request, q: str = "", course_mode: bool = False):
    """No DB connection is held across the (potentially slow) search call:
    one short session for the cache read, the search with none open, then a
    second short session for the cache write. A failed cache write is logged
    to the page as nothing — it must not discard results that were fetched."""
    results, error, degraded, backend = [], None, False, None
    if q:
        ttl = config.get_search_cache_ttl_hours()
        cached = None
        try:
            with db_session() as conn:
                cached = youtube_extractor.get_cached_search(conn, q, course_mode, ttl)
        except Exception:
            cached = None  # treat an unreadable cache as a miss

        if cached is not None:
            results, backend = cached
            degraded = backend == "invidious"
        else:
            try:
                results, backend, degraded = youtube_extractor.search_educational(
                    q, course_mode,
                    api_key=config.get_youtube_api_key(),
                    invidious_instances=config.get_invidious_instances(),
                )
            except Exception as e:
                error = str(e)
            else:
                try:
                    with db_session() as conn:
                        youtube_extractor.set_cached_search(conn, q, course_mode, results, backend)
                except Exception:
                    pass  # cache write is best-effort
    return templates.TemplateResponse(request, "youtube.html", {
        "videos": [], "tabs": TABS,
        "active_tab": None, "page": 1,
        "search_results": results, "query": q, "course_mode": course_mode,
        "error": error, "degraded": degraded,
    })

@app.get("/youtube/playlist/{playlist_id}", response_class=HTMLResponse)
def youtube_playlist(request: Request, playlist_id: str):
    videos = youtube_extractor.get_playlist_videos(
        playlist_id,
        api_key=config.get_youtube_api_key(),
        invidious_instances=config.get_invidious_instances(),
    )
    if not videos:
        raise HTTPException(404, "playlist is empty or private")
    return templates.TemplateResponse(request, "youtube_playlist.html", {
        "videos": videos, "playlist_id": playlist_id,
    })


class ClickIn(BaseModel):
    video_id: str | None = ""
    title: str | None = ""


@app.post("/youtube/click")
def log_click(payload: ClickIn, request: Request, response: Response):
    # Declared `def`, not `async def`: the DB driver is synchronous, and a
    # blocking call on the event loop stalls every other request.
    if not payload.video_id:
        raise HTTPException(400, "video_id required")
    device_id = _get_or_set_device_id(request, response)
    with db_session() as conn:
        conn.execute(
            "INSERT INTO user_history (video_id, title, accessed_at, device_id) VALUES (?, ?, ?, ?)",
            (payload.video_id, payload.title or "",
             datetime.now(timezone.utc).isoformat(), device_id),
        )
    return {"ok": True}


# ---------------------------------------------------------------- youtube history
# Separate tab, not the "jump back in" strip that used to sit on /youtube —
# scoped per-device via the device_id cookie, not shared globally, and
# deletable (per-entry and clear-all), unlike the old embedded version.

@app.get("/youtube/history", response_class=HTMLResponse)
def youtube_history(request: Request, page: int = 1):
    device_id = request.cookies.get(DEVICE_ID_COOKIE)
    is_new = not device_id
    if is_new:
        device_id = secrets.token_hex(16)
        entries = []  # a device with no cookie has no history; skip the query
    else:
        limit, offset = 30, (page - 1) * 30
        with db_session() as conn:
            entries = conn.execute(
                "SELECT * FROM user_history WHERE device_id = ? "
                "ORDER BY accessed_at DESC LIMIT ? OFFSET ?",
                (device_id, limit, offset),
            ).fetchall()
    resp = templates.TemplateResponse(request, "youtube_history.html", {
        "entries": entries, "page": page,
    })
    if is_new:
        _set_device_cookie(resp, device_id)  # set on the response actually returned
    return resp


@app.post("/youtube/history/{entry_id}/delete")
def delete_history_entry(entry_id: int, request: Request, response: Response):
    device_id = _get_or_set_device_id(request, response)
    with db_session() as conn:
        # Scoped to device_id, not just id — a device can only delete its
        # own entries, not anyone else's by guessing a row id.
        cur = conn.execute(
            "DELETE FROM user_history WHERE id = ? AND device_id = ?",
            (entry_id, device_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(404, "entry not found for this device")
    return {"ok": True}


@app.post("/youtube/history/clear")
def clear_history(request: Request, response: Response):
    device_id = _get_or_set_device_id(request, response)
    with db_session() as conn:
        cur = conn.execute("DELETE FROM user_history WHERE device_id = ?", (device_id,))
    return {"ok": True, "deleted": cur.rowcount}

# ---------------------------------------------------------------- save toggle (shared)

@app.post("/save/{module}/{item_id}")
def toggle_save(module: str, item_id: str):
    if module not in VALID_MODULES:
        raise HTTPException(404, "unknown module")
    table, pk = VALID_MODULES[module]
    with db_session() as conn:
        row = conn.execute(f"SELECT is_saved FROM {table} WHERE {pk} = ?", (item_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "item not found")
        new_val = 0 if row["is_saved"] else 1
        conn.execute(f"UPDATE {table} SET is_saved = ? WHERE {pk} = ?", (new_val, item_id))
    return {"ok": True, "is_saved": bool(new_val)}

# ---------------------------------------------------------------- substack

@app.get("/substack", response_class=HTMLResponse)
def substack_feed(request: Request, page: int = 1):
    limit, offset = 20, (page - 1) * 20
    with db_session() as conn:
        articles = conn.execute(
            "SELECT * FROM substack_cache ORDER BY published_date DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return templates.TemplateResponse(request, "substack.html", {
        "articles": articles, "page": page,
    })

# ---------------------------------------------------------------- news / papers

NEWS_TABS = {
    "country": ["Daily_Star", "Prothom Alo", "The Business Standard"],
    "international": ["Al Jazeera"],
}

def _feed_page(request: Request, module_type: str, page: int):
    # Retained exclusively for the /papers route
    limit, offset = 40, (page - 1) * 40
    with db_session() as conn:
        items = conn.execute(
            "SELECT * FROM rss_cache WHERE module_type = ? "
            "ORDER BY published_date DESC LIMIT ? OFFSET ?",
            (module_type, limit, offset),
        ).fetchall()
    return templates.TemplateResponse(request, "feed.html", {
        "items": items, "page": page,
        "module_name": module_type,
        "module_slug": "papers",
        "tabs": None,
    })

@app.get("/news", response_class=HTMLResponse)
def news_feed(request: Request, tab: str = "country", page: int = 1):
    if tab not in NEWS_TABS:
        tab = "country"

    allowed_sources = NEWS_TABS[tab]
    limit, offset = 40, (page - 1) * 40

    # Dynamically construct the IN clause placeholders based on the array length
    placeholders = ",".join("?" * len(allowed_sources))
    query = f"""
        SELECT * FROM rss_cache
        WHERE module_type = 'News' AND source_name IN ({placeholders})
        ORDER BY published_date DESC LIMIT ? OFFSET ?
    """
    params = tuple(allowed_sources) + (limit, offset)

    with db_session() as conn:
        items = conn.execute(query, params).fetchall()

    return templates.TemplateResponse(request, "feed.html", {
        "items": items, "page": page,
        "module_name": "News",
        "module_slug": "news",
        "tabs": list(NEWS_TABS.keys()),
        "active_tab": tab,
    })

@app.get("/papers", response_class=HTMLResponse)
def library_feed(request: Request, tab: str = "Journals", page: int = 1):
    # Map UI tabs directly to database module types
    VALID_TABS = {"Journals": "Paper", "Blogs": "Blog"}

    if tab not in VALID_TABS:
        tab = "Journals"

    limit, offset = 40, (page - 1) * 40
    target_module = VALID_TABS[tab]

    with db_session() as conn:
        items = conn.execute(
            "SELECT * FROM rss_cache WHERE module_type = ? ORDER BY published_date DESC LIMIT ? OFFSET ?",
            (target_module, limit, offset),
        ).fetchall()

    return templates.TemplateResponse(request, "feed.html", {
        "items": items, "page": page,
        "module_name": "Library",
        "module_slug": "papers",
        "tabs": list(VALID_TABS.keys()),
        "active_tab": tab,
    })

# ---------------------------------------------------------------- admin

def _check_admin(x_admin_token: str) -> None:
    if not _token_ok(x_admin_token):
        raise HTTPException(403, "invalid or missing admin token")

@app.post("/admin/substack-cookie")
async def update_substack_cookie(request: Request, x_admin_token: str = Header(default="")):
    _check_admin(x_admin_token)
    body = await request.json()
    cookie = (body.get("cookie") or "").strip()
    if not cookie:
        raise HTTPException(400, "cookie required")
    config.set_substack_cookie(cookie)
    return {"ok": True}

@app.post("/admin/run-worker")
def trigger_worker(x_admin_token: str = Header(default="")):
    _check_admin(x_admin_token)
    return worker.run_cycle()
