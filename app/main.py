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

from app import config, worker
from app.database import db_session, init_db
from app.extractors import youtube as youtube_extractor

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

DEVICE_ID_COOKIE = "device_id"


def _get_or_set_device_id(request: Request, response: Response) -> str:
    """History is scoped per-device via this cookie, not per-user-account —
    there's no login system here. httponly + 2-year expiry: it's an opaque
    identifier, not something the page's own JS needs to read."""
    device_id = request.cookies.get(DEVICE_ID_COOKIE)
    if not device_id:
        device_id = secrets.token_hex(16)
        response.set_cookie(
            DEVICE_ID_COOKIE, device_id,
            max_age=60 * 60 * 24 * 365 * 2,
            httponly=True, samesite="lax",
        )
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

# ---------------------------------------------------------------- session & cron

@app.post("/api/heartbeat")
def register_activity():
    """Registers an active session lock in the database."""
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, val) VALUES ('last_active', ?)", 
            (now,)
        )
    return {"status": "locked"}

@app.get("/cron/maintenance")
def run_maintenance(token: str, background_tasks: BackgroundTasks):
    """External cron trigger. Dispatches ingestion to a background task."""
    expected = config.get_admin_token()
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="invalid or missing admin token")
        
    with db_session() as conn:
        # 1. Check active session lock synchronously
        row = conn.execute("SELECT val FROM system_state WHERE key = 'last_active'").fetchone()
        if row:
            last_active = datetime.fromisoformat(row['val'])
            if datetime.now(timezone.utc) - last_active < timedelta(minutes=2):
                return {"status": "aborted", "reason": "Active session detected. Database locked."}
                
    # 2. Define the heavy background workload
    def background_ingestion():
        with db_session() as conn:
            expiry = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            conn.execute("DELETE FROM rss_cache WHERE published_date < ? AND is_saved = 0", (expiry,))
            conn.execute("DELETE FROM youtube_cache WHERE published_date < ? AND is_saved = 0", (expiry,))
            conn.execute("DELETE FROM substack_cache WHERE published_date < ? AND is_saved = 0", (expiry,))
        
        # Execute the ingestion worker
        worker.run_cycle()

    # 3. Dispatch and return immediately
    background_tasks.add_task(background_ingestion)
    
    return {"status": "Maintenance payload dispatched to background thread"}

# ---------------------------------------------------------------- hub

@app.get("/", response_class=HTMLResponse)
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
    results, error, degraded, backend = [], None, False, None
    if q:
        ttl = config.get_search_cache_ttl_hours()
        with db_session() as conn:
            cached = youtube_extractor.get_cached_search(conn, q, course_mode, ttl)
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
                    youtube_extractor.set_cached_search(conn, q, course_mode, results, backend)
                except Exception as e:
                    error = str(e)
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

@app.post("/youtube/click")
async def log_click(request: Request, response: Response):
    body = await request.json()
    video_id, title = body.get("video_id"), body.get("title", "")
    if not video_id:
        raise HTTPException(400, "video_id required")
    device_id = _get_or_set_device_id(request, response)
    with db_session() as conn:
        conn.execute(
            "INSERT INTO user_history (video_id, title, accessed_at, device_id) VALUES (?, ?, ?, ?)",
            (video_id, title, datetime.now(timezone.utc).isoformat(), device_id),
        )
    return {"ok": True}


# ---------------------------------------------------------------- youtube history
# Separate tab, not the "jump back in" strip that used to sit on /youtube —
# scoped per-device via the device_id cookie, not shared globally, and
# deletable (per-entry and clear-all), unlike the old embedded version.

@app.get("/youtube/history", response_class=HTMLResponse)
def youtube_history(request: Request, response: Response, page: int = 1):
    device_id = _get_or_set_device_id(request, response)
    limit, offset = 30, (page - 1) * 30
    with db_session() as conn:
        entries = conn.execute(
            "SELECT * FROM user_history WHERE device_id = ? "
            "ORDER BY accessed_at DESC LIMIT ? OFFSET ?",
            (device_id, limit, offset),
        ).fetchall()
    return templates.TemplateResponse(request, "youtube_history.html", {
        "entries": entries, "page": page,
    })


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
    "country": ["Daily_Star", "Prothom Alo","The Business Standard"],
    "international": ["Al Jazeera"]
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
def papers_feed(request: Request, page: int = 1):
    return _feed_page(request, "Paper", page)

# ---------------------------------------------------------------- admin

def _check_admin(x_admin_token: str) -> None:
    expected = config.get_admin_token()
    if not expected or x_admin_token != expected:
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

