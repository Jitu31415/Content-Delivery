"""
Two families of function here:

1. ingest_channel() — reads each whitelisted channel's native video RSS feed
   (youtube.com/feeds/videos.xml). Free, no API key, no rate limit exposure.
   This is the primary path that fills youtube_cache for the categorized tabs.
   Unaffected by anything below — the category tabs are whitelist-driven,
   not search-driven, so they don't need educational filtering.

2. search_educational() and its two backends — the on-demand search bar.
   Deliberately not written to youtube_cache: caching ad-hoc query results
   there would pollute the category tabs with one-off query noise. Instead
   they go through search_cache (see database.py), keyed on (query,
   course_mode), which exists purely to avoid re-spending API quota on
   repeat searches — not to feed the tabs.
"""

import json
import re
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from app.extractors import edu_filter
from app.utils import normalize_date

try:
    from googleapiclient.discovery import build as build_youtube_service
    from googleapiclient.errors import HttpError
except ImportError:  # optional dependency — only needed for the official-API path
    build_youtube_service = None
    HttpError = Exception

_ISO8601_DURATION_RE = re.compile(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _parse_iso8601_duration(duration: str) -> int | None:
    """YouTube durations are ISO 8601 (e.g. 'PT15M33S'). No external
    dependency needed for a format this constrained."""
    if not duration:
        return None
    match = _ISO8601_DURATION_RE.match(duration)
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def fetch_channel_feed(channel_id: str) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    parsed = feedparser.parse(url)
    videos = []
    
    # Use a persistent client session to pool connections per feed
    with httpx.Client(timeout=5.0, follow_redirects=False) as client:
        for entry in parsed.entries:
            video_id = entry.get("yt_videoid") or entry.get("id", "").split(":")[-1]
            
            try:
                # Probe the Shorts endpoint using a HEAD request to minimize bandwidth
                resp = client.head(f"https://www.youtube.com/shorts/{video_id}")
                if resp.status_code == 200:
                    continue  # Format is a Short; bypass insertion
            except httpx.HTTPError:
                pass  # Fail open: if network drops, assume standard video to prevent data loss
                
            thumbs = entry.get("media_thumbnail") or []
            videos.append({
                "video_id": video_id,
                "channel_name": parsed.feed.get("title", "Unknown channel"),
                "title": entry.get("title", "Untitled"),
                "published_date": normalize_date(entry.get("published_parsed")),
                "thumbnail_url": thumbs[0].get("url", "") if thumbs else "",
            })
            
    return videos


def ingest_channel(conn, channel_id: str, category: str) -> int:
    videos = fetch_channel_feed(channel_id)
    inserted = 0
    for v in videos:
        cur = conn.execute(
            "INSERT OR IGNORE INTO youtube_cache "
            "(video_id, channel_name, title, category, published_date, thumbnail_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (v["video_id"], v["channel_name"], v["title"], category,
             v["published_date"], v["thumbnail_url"]),
        )
        inserted += cur.rowcount
    return inserted


# ==========================================================================
# Educational search — two backends behind one orchestrator.
#
# Result shape is deliberately the StudyTube convention (Title/Channel/
# Thumbnail/Embed_URL/Type/ID), not this file's snake_case cache convention
# above. That's a one-time decision, not drift: it lets the templates for
# this feature be near-verbatim ports of the existing search.html/
# results.html/playlist.html, which was the point of matching "the same UI".
# ==========================================================================

def _official_search(query: str, course_mode: bool, api_key: str,
                      max_results: int) -> list[dict]:
    if build_youtube_service is None:
        raise RuntimeError("google-api-python-client not installed")

    youtube = build_youtube_service("youtube", "v3", developerKey=api_key)
    strict_query = edu_filter.augment_query(query)

    search_kwargs = {
        "q": strict_query,
        "part": "snippet",
        "maxResults": max_results,
        "order": "relevance",
        "safeSearch": "strict",
    }
    if course_mode:
        search_kwargs["type"] = "playlist"
    else:
        search_kwargs["type"] = "video"
        search_kwargs["videoCategoryId"] = "27"  # Education
        # No videoDuration restriction: the "medium" bucket (4-20 min) used
        # by the original implementation silently excludes most full-length
        # lecture recordings (45-90 min is common), while still letting
        # 4-20 min listicle junk through. Real duration-based filtering
        # happens below instead, via an actual seconds value.

    response = youtube.search().list(**search_kwargs).execute()
    items = response.get("items", [])

    raw = []
    video_ids = []
    for item in items:
        snippet = item["snippet"]
        item_id = item["id"]
        if "videoId" in item_id:
            link_id = item_id["videoId"]
            entry = {
                "Title": snippet["title"], "Channel": snippet["channelTitle"],
                "Embed_URL": f"https://www.youtube.com/embed/{link_id}?autoplay=1&rel=0&modestbranding=1",
                "Thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                "Type": "Video", "ID": link_id,
                "_description": snippet.get("description", ""),
                "_duration_seconds": None,  # filled in below
            }
            video_ids.append(link_id)
        elif "playlistId" in item_id:
            link_id = item_id["playlistId"]
            entry = {
                "Title": snippet["title"], "Channel": snippet["channelTitle"],
                "Embed_URL": f"https://www.youtube.com/embed/videoseries?list={link_id}&autoplay=1&rel=0&modestbranding=1",
                "Thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                "Type": "Playlist", "ID": link_id,
                "_description": snippet.get("description", ""),
                "_duration_seconds": None,
            }
        else:
            continue
        raw.append(entry)

    # One batched follow-up call (1 quota unit total, not per-video) to get
    # real durations — needed for the sub-90s Shorts cutoff in edu_filter.
    if video_ids:
        durations = {}
        for i in range(0, len(video_ids), 50):  # videos.list caps at 50 ids/call
            batch = video_ids[i:i + 50]
            details = youtube.videos().list(part="contentDetails", id=",".join(batch)).execute()
            for v in details.get("items", []):
                durations[v["id"]] = _parse_iso8601_duration(v["contentDetails"].get("duration", ""))
        for entry in raw:
            if entry["Type"] == "Video":
                entry["_duration_seconds"] = durations.get(entry["ID"])

    return _apply_edu_filter(raw)


def _invidious_search(query: str, course_mode: bool, instances: list[str],
                       timeout: float = 6.0) -> list[dict]:
    strict_query = edu_filter.augment_query(query)
    item_type = "playlist" if course_mode else "video"
    last_error = None

    for base in instances:
        try:
            resp = httpx.get(
                f"{base}/api/v1/search",
                params={"q": strict_query, "type": item_type},
                timeout=timeout,
            )
            if resp.status_code == 429:
                last_error = f"{base}: rate limited (429)"
                continue
            resp.raise_for_status()
            data = resp.json()
            raw = []
            for item in data:
                if course_mode and item.get("type") != "playlist":
                    continue
                if not course_mode and item.get("type") != "video":
                    continue
                if course_mode:
                    link_id = item.get("playlistId")
                    raw.append({
                        "Title": item.get("title", ""), "Channel": item.get("author", ""),
                        "Embed_URL": f"https://www.youtube.com/embed/videoseries?list={link_id}&autoplay=1&rel=0&modestbranding=1",
                        "Thumbnail": _best_thumbnail(item), "Type": "Playlist", "ID": link_id,
                        "_description": "", "_duration_seconds": None,
                    })
                else:
                    link_id = item.get("videoId")
                    raw.append({
                        "Title": item.get("title", ""), "Channel": item.get("author", ""),
                        "Embed_URL": f"https://www.youtube.com/embed/{link_id}?autoplay=1&rel=0&modestbranding=1",
                        "Thumbnail": _best_thumbnail(item), "Type": "Video", "ID": link_id,
                        "_description": item.get("description", ""),
                        "_duration_seconds": item.get("lengthSeconds"),
                    })
            return _apply_edu_filter(raw)
        except httpx.HTTPError as e:
            last_error = f"{base}: {e}"
            continue
    raise RuntimeError(f"all Invidious instances failed — last error: {last_error}")


def _apply_edu_filter(raw: list[dict]) -> list[dict]:
    scored = []
    for entry in raw:
        keep, score = edu_filter.evaluate(
            title=entry["Title"], description=entry.pop("_description", ""),
            channel=entry["Channel"], duration_seconds=entry.pop("_duration_seconds", None),
        )
        if keep:
            scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored]


def search_educational(query: str, course_mode: bool, api_key: str,
                        invidious_instances: list[str],
                        max_results: int = 25) -> tuple[list[dict], str, bool]:
    """Returns (results, backend, degraded). backend is 'official' or
    'invidious'; degraded=True means the official API was unavailable
    (no key, or the call failed — e.g. quota exceeded) and results came
    from the heuristic-only Invidious fallback, which has no true
    category-level signal."""
    if api_key:
        try:
            results = _official_search(query, course_mode, api_key, max_results)
            return results, "official", False
        except Exception:
            pass  # missing dep, quota exceeded, bad key, network — all fall through
    results = _invidious_search(query, course_mode, invidious_instances)
    return results, "invidious", True


def _best_thumbnail(item: dict) -> str:
    thumbs = item.get("videoThumbnails") or []
    for t in thumbs:
        if t.get("quality") == "medium":
            return t.get("url", "")
    return thumbs[0].get("url", "") if thumbs else item.get("playlistThumbnail", "")


# ---------------------------------------------------------------- search result cache

def _cache_key(query: str, course_mode: bool) -> str:
    return f"{query.strip().lower()}::course={int(course_mode)}"


def get_cached_search(conn, query: str, course_mode: bool, ttl_hours: int):
    row = conn.execute(
        "SELECT results_json, backend, cached_at FROM search_cache WHERE cache_key = ?",
        (_cache_key(query, course_mode),),
    ).fetchone()
    if not row:
        return None
    cached_at = datetime.fromisoformat(row["cached_at"])
    if datetime.now(timezone.utc) - cached_at > timedelta(hours=ttl_hours):
        return None
    return json.loads(row["results_json"]), row["backend"]


def set_cached_search(conn, query: str, course_mode: bool, results: list[dict], backend: str):
    conn.execute(
        "INSERT INTO search_cache (cache_key, query, course_mode, results_json, backend, cached_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(cache_key) DO UPDATE SET "
        "results_json = excluded.results_json, backend = excluded.backend, cached_at = excluded.cached_at",
        (_cache_key(query, course_mode), query, int(course_mode),
         json.dumps(results), backend, datetime.now(timezone.utc).isoformat()),
    )


# ---------------------------------------------------------------- playlist video listing

def _official_playlist_videos(playlist_id: str, api_key: str) -> list[dict]:
    youtube = build_youtube_service("youtube", "v3", developerKey=api_key)
    request = youtube.playlistItems().list(
        part="snippet,contentDetails", playlistId=playlist_id, maxResults=50,
    )
    try:
        response = request.execute()
    except HttpError:
        return []

    videos = []
    for item in response.get("items", []):
        snippet = item.get("snippet", {})
        title = snippet.get("title", "Deleted video")
        channel_title = snippet.get("videoOwnerChannelTitle")
        if title in ("Deleted video", "Private video") or not channel_title:
            continue
        video_id = item.get("contentDetails", {}).get("videoId")
        if not video_id:
            continue
        thumb_url = snippet.get("thumbnails", {}).get("high", {}).get("url", "")
        videos.append({
            "Title": title, "Channel": channel_title,
            "Embed_URL": f"https://www.youtube.com/embed/{video_id}?autoplay=1&rel=0&modestbranding=1",
            "Thumbnail": thumb_url, "Video_ID": video_id,
        })
    return videos


def _invidious_playlist_videos(playlist_id: str, instances: list[str],
                                timeout: float = 6.0) -> list[dict]:
    for base in instances:
        try:
            resp = httpx.get(f"{base}/api/v1/playlists/{playlist_id}", timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            videos = []
            for v in data.get("videos", []):
                video_id = v.get("videoId")
                if not video_id:
                    continue
                videos.append({
                    "Title": v.get("title", "Untitled"), "Channel": v.get("author", ""),
                    "Embed_URL": f"https://www.youtube.com/embed/{video_id}?autoplay=1&rel=0&modestbranding=1",
                    "Thumbnail": _best_thumbnail(v), "Video_ID": video_id,
                })
            return videos
        except httpx.HTTPError:
            continue
    return []


def get_playlist_videos(playlist_id: str, api_key: str, invidious_instances: list[str]) -> list[dict]:
    if api_key:
        try:
            videos = _official_playlist_videos(playlist_id, api_key)
            if videos:
                return videos
        except Exception:
            pass
    return _invidious_playlist_videos(playlist_id, invidious_instances)
