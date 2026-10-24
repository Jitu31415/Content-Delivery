"""
Courses: up to MAX_COURSES YouTube playlists per device, with per-device progress.

A "course" is a YouTube playlist. Adding one snapshots its lesson list; progress
is the set of lessons marked complete, shown as a percentage. Everything is
keyed by the device_id cookie (the same one the history tab uses), so another
device has its own courses and its own progress and cannot see yours. There is
no login: the cookie is the identity, so clearing the app's data on the device
starts a fresh, empty course list.

Why snapshot the lessons instead of reading the playlist on every page view:
a page view must not spend YouTube API quota or depend on an Invidious
instance being up. The "Refresh" action re-reads the playlist and keeps the
completion marks for lessons that are still in it.
"""

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx

from app.extractors import youtube as yt
from app.utils import insert_or_ignore_many

MAX_COURSES = 10
MAX_LESSONS = 300

_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
# A bare id is only accepted with a known playlist prefix, so a pasted video id
# (11 characters) is not mistaken for a playlist.
_BARE_PLAYLIST_ID = re.compile(r"^(PL|UU|OL|FL|LL|RD)[A-Za-z0-9_-]{8,62}$")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
             "youtu.be", "www.youtube-nocookie.com"}


class CourseError(Exception):
    """code is one of: invalid, limit, dup, fetch, empty."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def valid_playlist_id(value) -> bool:
    return bool(value) and bool(_PLAYLIST_ID.match(value))


def valid_video_id(value) -> bool:
    return bool(value) and bool(_VIDEO_ID.match(value))


def parse_playlist_id(text):
    """Accepts a playlist link (watch?v=..&list=.., /playlist?list=..) or a bare
    playlist id. Returns the id, or None."""
    text = (text or "").strip()
    if not text or len(text) > 500:
        return None
    if _BARE_PLAYLIST_ID.match(text):
        return text
    try:
        parts = urlsplit(text if "://" in text else "https://" + text)
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in _YT_HOSTS:
        return None
    pid = (parse_qs(parts.query).get("list") or [""])[0]
    return pid if _PLAYLIST_ID.match(pid) else None


def percent(done: int, total: int) -> int:
    return int(done * 100 / total) if total else 0


# ---------------------------------------------------------------- fetching

def _official(pid: str, api_key: str):
    if yt.build_youtube_service is None:
        raise RuntimeError("google-api-python-client not installed")
    svc = yt.build_youtube_service("youtube", "v3", developerKey=api_key)
    found = svc.playlists().list(part="snippet", id=pid).execute().get("items", [])
    if not found:
        raise CourseError("fetch", "playlist not found or private")
    snippet = found[0]["snippet"]
    videos, token = [], None
    while len(videos) < MAX_LESSONS:
        kwargs = {"part": "snippet,contentDetails", "playlistId": pid, "maxResults": 50}
        if token:
            kwargs["pageToken"] = token
        resp = svc.playlistItems().list(**kwargs).execute()
        for item in resp.get("items", []):
            snip = item.get("snippet", {})
            title = snip.get("title", "")
            vid = (item.get("contentDetails") or {}).get("videoId")
            if title in ("Deleted video", "Private video") or not vid or not snip.get("videoOwnerChannelTitle"):
                continue
            videos.append({"video_id": vid, "title": title})
            if len(videos) >= MAX_LESSONS:
                break
        token = resp.get("nextPageToken")
        if not token:
            break
    meta = {"title": snippet.get("title") or "Untitled course", "channel": snippet.get("channelTitle", "")}
    return meta, videos


def _invidious(pid: str, instances, timeout: float = 6.0):
    for base in instances:
        try:
            videos, seen, title, channel = [], set(), "", ""
            for page in range(1, 7):
                r = httpx.get(f"{base}/api/v1/playlists/{pid}", params={"page": page}, timeout=timeout)
                if r.status_code != 200:
                    raise httpx.HTTPError(f"{base}: HTTP {r.status_code}")
                data = r.json()
                if page == 1:
                    title, channel = data.get("title", ""), data.get("author", "")
                added = 0
                for v in data.get("videos") or []:
                    vid = v.get("videoId")
                    if vid and v.get("title") and vid not in seen:
                        seen.add(vid)
                        videos.append({"video_id": vid, "title": v["title"]})
                        added += 1
                # An instance that ignores ?page= returns the same videos again.
                if added == 0 or len(videos) >= MAX_LESSONS:
                    break
            if videos:
                return {"title": title or "Untitled course", "channel": channel}, videos[:MAX_LESSONS]
        except (httpx.HTTPError, ValueError):
            continue
    raise CourseError("fetch", "all backends failed")


def fetch_playlist(pid: str, api_key: str, instances):
    """Returns (meta, videos). Official API first, Invidious as the fallback,
    mirroring how search and playlists already work in this app."""
    if api_key:
        try:
            meta, videos = _official(pid, api_key)
            if videos:
                return meta, videos
        except Exception:
            pass
    meta, videos = _invidious(pid, instances)
    if not videos:
        raise CourseError("empty", "no playable videos")
    return meta, videos


# ---------------------------------------------------------------- database

def _done_counts(conn, device_id: str) -> dict:
    """Completed lessons per course, counting only lessons still in the course."""
    rows = conn.execute(
        "SELECT p.playlist_id AS pid, COUNT(*) AS done FROM course_progress p "
        "JOIN course_videos v ON v.device_id = p.device_id AND v.playlist_id = p.playlist_id "
        "AND v.video_id = p.video_id WHERE p.device_id = ? GROUP BY p.playlist_id",
        (device_id,),
    ).fetchall()
    return {r["pid"]: r["done"] for r in rows}


def _shape(row: dict, done: int) -> dict:
    return {**row, "done": done, "percent": percent(done, row["total"])}


def list_courses(conn, device_id: str) -> list:
    rows = conn.execute(
        "SELECT playlist_id, title, channel, thumb_video, total FROM courses "
        "WHERE device_id = ? ORDER BY added_at, playlist_id", (device_id,),
    ).fetchall()
    counts = _done_counts(conn, device_id)
    return [_shape(r, counts.get(r["playlist_id"], 0)) for r in rows]


def get_course(conn, device_id: str, pid: str):
    row = conn.execute(
        "SELECT playlist_id, title, channel, thumb_video, total FROM courses "
        "WHERE device_id = ? AND playlist_id = ?", (device_id, pid),
    ).fetchone()
    if row is None:
        return None
    return _shape(row, _done_counts(conn, device_id).get(pid, 0))


def get_lessons(conn, device_id: str, pid: str) -> list:
    lessons = conn.execute(
        "SELECT position, video_id, title FROM course_videos "
        "WHERE device_id = ? AND playlist_id = ? ORDER BY position", (device_id, pid),
    ).fetchall()
    done = {r["video_id"] for r in conn.execute(
        "SELECT video_id FROM course_progress WHERE device_id = ? AND playlist_id = ?",
        (device_id, pid)).fetchall()}
    return [{"n": i + 1, "video_id": l["video_id"], "title": l["title"], "done": l["video_id"] in done}
            for i, l in enumerate(lessons)]


def _insert_lessons(conn, device_id: str, pid: str, videos: list) -> None:
    insert_or_ignore_many(
        conn, "course_videos", ("device_id", "playlist_id", "position", "video_id", "title"),
        [(device_id, pid, i, v["video_id"], v["title"]) for i, v in enumerate(videos)],
    )


def count_courses(conn, device_id: str) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM courses WHERE device_id = ?", (device_id,)).fetchone()["n"]


def has_course(conn, device_id: str, pid: str) -> bool:
    return conn.execute("SELECT 1 AS x FROM courses WHERE device_id = ? AND playlist_id = ?",
                        (device_id, pid)).fetchone() is not None


def add_course(conn, device_id: str, pid: str, meta: dict, videos: list) -> str:
    """Returns "ok", "dup" or "limit". The cap is enforced inside the INSERT, so
    two simultaneous adds cannot both slip past the check."""
    if has_course(conn, device_id, pid):
        return "dup"
    conn.execute(
        "INSERT INTO courses (device_id, playlist_id, title, channel, thumb_video, total, added_at) "
        "SELECT ?, ?, ?, ?, ?, ?, ? WHERE (SELECT COUNT(*) FROM courses WHERE device_id = ?) < ?",
        (device_id, pid, meta["title"], meta["channel"], videos[0]["video_id"], len(videos),
         datetime.now(timezone.utc).isoformat(), device_id, MAX_COURSES),
    )
    if not has_course(conn, device_id, pid):
        return "limit"
    _insert_lessons(conn, device_id, pid, videos)
    return "ok"


def refresh_course(conn, device_id: str, pid: str, meta: dict, videos: list) -> None:
    """Replace the lesson list. Completion marks are keyed by video id, so those
    for lessons still in the playlist are kept; marks for removed lessons stay
    stored but are no longer counted."""
    conn.execute("UPDATE courses SET title = ?, channel = ?, thumb_video = ?, total = ? "
                 "WHERE device_id = ? AND playlist_id = ?",
                 (meta["title"], meta["channel"], videos[0]["video_id"], len(videos), device_id, pid))
    conn.execute("DELETE FROM course_videos WHERE device_id = ? AND playlist_id = ?", (device_id, pid))
    _insert_lessons(conn, device_id, pid, videos)


def remove_course(conn, device_id: str, pid: str) -> None:
    for table in ("course_progress", "course_videos", "courses"):
        conn.execute(f"DELETE FROM {table} WHERE device_id = ? AND playlist_id = ?", (device_id, pid))


def set_done(conn, device_id: str, pid: str, video_id: str, done: bool) -> bool:
    """False if the video is not a lesson of this device's course."""
    if conn.execute(
        "SELECT 1 AS x FROM course_videos WHERE device_id = ? AND playlist_id = ? AND video_id = ? LIMIT 1",
        (device_id, pid, video_id),
    ).fetchone() is None:
        return False
    if done:
        conn.execute(
            "INSERT OR IGNORE INTO course_progress (device_id, playlist_id, video_id, completed_at) "
            "VALUES (?, ?, ?, ?)", (device_id, pid, video_id, datetime.now(timezone.utc).isoformat()))
    else:
        conn.execute("DELETE FROM course_progress WHERE device_id = ? AND playlist_id = ? AND video_id = ?",
                     (device_id, pid, video_id))
    return True
