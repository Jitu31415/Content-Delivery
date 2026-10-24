import os
import secrets
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

load_dotenv()  # must run before app.config reads any env vars below

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import config, worker
from app import courses as courses_db
from app import reader
from app.sandbox import SandboxMiddleware
from app.database import db_session, init_db
from app.extractors import generic_rss
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


# "courses" is not a cached category: /youtube?tab=courses redirects to the Courses page.
TABS = ["scientific", "music_poetry", "podcast", "religious", "courses"]

# Columns the list pages need. Deliberately excludes `summary`: it can be tens of
# KB per row, and 40 rows of it would be pulled over the network on every page view.
RSS_LIST_COLS = "item_id, module_type, source_name, published_date, url, title, is_saved"

# module_type -> (list page, label) for the back link on /item.
ITEM_BACK = {
    "News": ("/news", "News"),
    "Paper": ("/papers?tab=Journals", "Journals"),
    "Blog": ("/papers?tab=Blogs", "Blogs"),
}

# module -> (table, primary key column). Used by the generic save-toggle route.
VALID_MODULES = {
    "youtube": ("youtube_cache", "video_id"),
    "substack": ("substack_cache", "article_id"),
    "rss": ("rss_cache", "item_id"),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        reader.load_dynamic_hosts()   # Substack custom-domain hosts recorded by earlier cycles
    except Exception:
        pass                          # first run, or database unreachable: fall back to the static allowlist
    # worker.start_scheduler() removed for Render serverless compatibility
    yield


app = FastAPI(title="Content Aggregator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Closed-world sandbox: neutralises external links/forms/iframes in every HTML
# response and sends a sandboxing CSP. See app/sandbox.py. SANDBOX_MODE=off
# disables it (e.g. for debugging in a desktop browser).
app.add_middleware(SandboxMiddleware)

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
    if tab == "courses":
        return RedirectResponse("/youtube/courses", status_code=303)
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
    if not courses_db.valid_playlist_id(playlist_id):
        raise HTTPException(404, "playlist not found")   # also keeps the id out of outbound URL paths
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


# ---------------------------------------------------------------- courses (per device)

COURSE_MESSAGES = {
    "invalid": "That does not look like a YouTube playlist link or ID.",
    "limit": f"You already have {courses_db.MAX_COURSES} courses. Remove one to add another.",
    "dup": "That course is already in your list.",
    "fetch": "The playlist could not be read. It may be private, or YouTube may be unavailable. Try again later.",
    "empty": "That playlist has no playable videos.",
}


async def _form_fields(request: Request) -> dict:
    """urlencoded form body, parsed by hand: Form() would need python-multipart,
    a dependency this app does not otherwise have."""
    raw = await request.body()
    if len(raw) > 20_000:
        raise HTTPException(413, "request too large")
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items() if v}


def _device(request: Request):
    device_id = request.cookies.get(DEVICE_ID_COOKIE)
    return (device_id, False) if device_id else (secrets.token_hex(16), True)


def _redirect(url: str, device_id: str, is_new: bool) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if is_new:
        _set_device_cookie(resp, device_id)   # on the response actually returned
    return resp


def _owned_course(request: Request, playlist_id: str):
    """(device_id, course) for this device, or 404. A device without the cookie
    has no courses; a course id from another device is simply not found."""
    device_id = request.cookies.get(DEVICE_ID_COOKIE)
    if not courses_db.valid_playlist_id(playlist_id) or not device_id:
        raise HTTPException(404, "course not found")
    with db_session() as conn:
        course = courses_db.get_course(conn, device_id, playlist_id)
    if course is None:
        raise HTTPException(404, "course not found")
    return device_id, course


@app.get("/youtube/courses", response_class=HTMLResponse)
def courses_home(request: Request, e: str = ""):
    device_id = request.cookies.get(DEVICE_ID_COOKIE)
    courses = []
    if device_id:
        with db_session() as conn:
            courses = courses_db.list_courses(conn, device_id)
    return templates.TemplateResponse(request, "youtube_courses.html", {
        "courses": courses, "tabs": TABS, "max_courses": courses_db.MAX_COURSES,
        "slots_left": courses_db.MAX_COURSES - len(courses),
        "error": COURSE_MESSAGES.get(e),
    })


@app.post("/youtube/courses/add")
def courses_add(request: Request, fields: dict = Depends(_form_fields)):
    device_id, is_new = _device(request)
    pid = courses_db.parse_playlist_id(fields.get("playlist", ""))
    if pid is None:
        return _redirect("/youtube/courses?e=invalid", device_id, is_new)
    with db_session() as conn:   # checked before fetching, so a refused add spends no API quota
        if courses_db.has_course(conn, device_id, pid):
            return _redirect("/youtube/courses?e=dup", device_id, is_new)
        if courses_db.count_courses(conn, device_id) >= courses_db.MAX_COURSES:
            return _redirect("/youtube/courses?e=limit", device_id, is_new)
    try:
        meta, videos = courses_db.fetch_playlist(
            pid, config.get_youtube_api_key(), config.get_invidious_instances())
    except courses_db.CourseError as err:
        return _redirect(f"/youtube/courses?e={err.code}", device_id, is_new)
    with db_session() as conn:
        outcome = courses_db.add_course(conn, device_id, pid, meta, videos)
    if outcome != "ok":
        return _redirect(f"/youtube/courses?e={outcome}", device_id, is_new)
    return _redirect(f"/youtube/courses/{pid}", device_id, is_new)


@app.get("/youtube/courses/{playlist_id}", response_class=HTMLResponse)
def course_page(request: Request, playlist_id: str, v: str = ""):
    device_id, course = _owned_course(request, playlist_id)
    with db_session() as conn:
        lessons = courses_db.get_lessons(conn, device_id, playlist_id)
    index = {l["video_id"]: i for i, l in enumerate(lessons)}
    chosen = index.get(v) if courses_db.valid_video_id(v) else None
    if chosen is None:   # no lesson picked: continue with the first unfinished one
        chosen = next((i for i, l in enumerate(lessons) if not l["done"]), 0)
    current = lessons[chosen] if lessons else None
    return templates.TemplateResponse(request, "youtube_course.html", {
        "course": course, "lessons": lessons, "current": current,
        "next_lesson": lessons[chosen + 1] if lessons and chosen + 1 < len(lessons) else None,
        "autoplay": chosen is not None and v in index, "tabs": TABS,
        "error": COURSE_MESSAGES.get(request.query_params.get("e", "")),
    })


@app.post("/youtube/courses/{playlist_id}/toggle")
def course_toggle(request: Request, playlist_id: str, fields: dict = Depends(_form_fields)):
    device_id, _course = _owned_course(request, playlist_id)
    video_id = fields.get("video_id", "")
    if not courses_db.valid_video_id(video_id):
        raise HTTPException(400, "bad video id")
    with db_session() as conn:
        if not courses_db.set_done(conn, device_id, playlist_id, video_id, fields.get("done") == "1"):
            raise HTTPException(404, "lesson not found")
    return RedirectResponse(f"/youtube/courses/{playlist_id}#l-{video_id}", status_code=303)


class ProgressIn(BaseModel):
    video_id: str = ""
    done: bool = True


@app.post("/youtube/courses/{playlist_id}/progress")
def course_progress(request: Request, playlist_id: str, payload: ProgressIn):
    """Called by the player page when a lesson ends or passes 90%."""
    device_id, _course = _owned_course(request, playlist_id)
    if not courses_db.valid_video_id(payload.video_id):
        raise HTTPException(400, "bad video id")
    with db_session() as conn:
        if not courses_db.set_done(conn, device_id, playlist_id, payload.video_id, payload.done):
            raise HTTPException(404, "lesson not found")
        course = courses_db.get_course(conn, device_id, playlist_id)
    return {"ok": True, "done": course["done"], "total": course["total"], "percent": course["percent"]}


@app.post("/youtube/courses/{playlist_id}/remove")
def course_remove(request: Request, playlist_id: str):
    device_id, _course = _owned_course(request, playlist_id)
    with db_session() as conn:
        courses_db.remove_course(conn, device_id, playlist_id)
    return RedirectResponse("/youtube/courses", status_code=303)


@app.post("/youtube/courses/{playlist_id}/refresh")
def course_refresh(request: Request, playlist_id: str):
    device_id, _course = _owned_course(request, playlist_id)
    try:
        meta, videos = courses_db.fetch_playlist(
            playlist_id, config.get_youtube_api_key(), config.get_invidious_instances())
    except courses_db.CourseError as err:
        return RedirectResponse(f"/youtube/courses/{playlist_id}?e={err.code}", status_code=303)
    with db_session() as conn:
        courses_db.refresh_course(conn, device_id, playlist_id, meta, videos)
    return RedirectResponse(f"/youtube/courses/{playlist_id}", status_code=303)

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

# {item_id:path}: RSS and Substack ids are usually URLs. A plain {item_id} matches a
# single path segment, and "%2F" is decoded to "/" before routing, so those ids 404.
@app.post("/save/{module}/{item_id:path}")
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
            f"SELECT {RSS_LIST_COLS} FROM rss_cache WHERE module_type = ? "
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
        SELECT {RSS_LIST_COLS} FROM rss_cache
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
            f"SELECT {RSS_LIST_COLS} FROM rss_cache WHERE module_type = ? ORDER BY published_date DESC LIMIT ? OFFSET ?",
            (target_module, limit, offset),
        ).fetchall()

    return templates.TemplateResponse(request, "feed.html", {
        "items": items, "page": page,
        "module_name": "Library",
        "module_slug": "papers",
        "tabs": list(VALID_TABS.keys()),
        "active_tab": tab,
    })

# ---------------------------------------------------------------- item detail

@app.get("/item", response_class=HTMLResponse)
def item_detail(request: Request, item_id: str = Query("", alias="id")):
    """One news / paper / blog item, read inside the app from the feed's own
    stored text. The id travels as a query parameter because ids are URLs.
    Sync `def`: blocking DB call stays off the event loop."""
    if not item_id:
        raise HTTPException(404, "item not found")
    with db_session() as conn:
        row = conn.execute(
            f"SELECT {RSS_LIST_COLS}, summary FROM rss_cache WHERE item_id = ?", (item_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "item not found")
    back_href, back_label = ITEM_BACK.get(row["module_type"], ("/", "Home"))
    return templates.TemplateResponse(request, "item.html", {
        "item": row,
        # Sanitised at ingest and again here, so a bad row cannot inject markup.
        "summary": generic_rss.sanitize_summary(row.get("summary") or ""),
        # Only offered when the item's host is an allowed source.
        "reader_link": reader.reader_href(row.get("url") or "", "https://invalid.invalid/", reader.allowed_hosts()),
        "back_href": back_href, "back_label": back_label,
    })

# ---------------------------------------------------------------- reader (allowlisted)

@app.get("/read", response_class=HTMLResponse)
def read_article(u: str = "", full: bool = False):
    """Stripped-down view of an article from one of the configured sources.
    Sync `def`: the fetch is blocking network I/O and must not run on the
    event loop. See app/reader.py for the allowlist and proxy hardening."""
    try:
        return HTMLResponse(reader.read(u, full))
    except reader.ReaderError as e:
        return HTMLResponse(reader.error_page(e), status_code=e.status)

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

@app.get("/admin/reader-hosts")
def reader_hosts_report(x_admin_token: str = Header(default="")):
    """Which hosts the reader may open, and which hosts your stored items
    point at that it may not. Review before adding anything to "reader_hosts"
    in sources.json: item URLs come from feeds, so a host appearing here is
    not evidence that it is trustworthy (feed redirectors and video sites show
    up too)."""
    _check_admin(x_admin_token)
    allowed = reader.allowed_hosts()
    seen, example = Counter(), {}
    with db_session() as conn:
        rows = conn.execute("SELECT url FROM rss_cache").fetchall()
        rows += conn.execute("SELECT url FROM substack_cache").fetchall()
    for row in rows:
        url = row.get("url") or ""
        host = (urlsplit(url).hostname or "").lower()
        if host and not reader.host_allowed(host, allowed):
            seen[host] += 1
            example.setdefault(host, url)
    return {
        "allowed": sorted(allowed),
        "seen_but_not_allowed": [
            {"host": h, "items": n, "example": example[h]} for h, n in seen.most_common()
        ],
    }
