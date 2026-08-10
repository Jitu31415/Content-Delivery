# Content Aggregator

Self-hosted, chronological content aggregator. Implements the technical
specification in full, including the Phase 2 corrections (WAL mode,
`is_saved`-gated pruning, sanitized Substack HTML, admin cookie rotation,
Invidious instance fallback).

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit ADMIN_TOKEN at minimum
```

Edit `sources.json` — replace every `REPLACE_WITH_*` placeholder with real
channel IDs, author subdomains, and feed URLs. The `papers` entries ship
with two working arXiv/Nature RSS URLs as a working example; `youtube`,
`substack`, and `news` are placeholders that must be filled in — they were
not fabricated, since a fake channel ID or feed URL would silently fail
ingestion and look identical to a real misconfiguration.

Run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

On startup: the schema initializes, and the background worker runs one
ingest cycle immediately, then every `WORKER_INTERVAL_HOURS` (default 6).

## Admin endpoints

Unlisted in navigation by design. Require `x-admin-token` header matching
`ADMIN_TOKEN` in `.env`.

```bash
# Rotate the Substack session cookie without restarting the server
curl -X POST http://localhost:8000/admin/substack-cookie \
  -H "x-admin-token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cookie": "the-substack.sid-cookie-value"}'

# Trigger an ingest cycle on demand (useful after editing sources.json)
curl -X POST http://localhost:8000/admin/run-worker \
  -H "x-admin-token: $ADMIN_TOKEN"
```

Getting the cookie value: log into Substack in a browser, open dev tools →
Application/Storage → Cookies → `substack.sid`, copy the value only (not
the full `Cookie` header).

## Educational search (YouTube module)

`/youtube/search` filters results to educational content through three
independent layers, so a leak in one is still caught by another:

1. **Query augmentation** — the search text itself is expanded with
   include/exclude terms before either backend runs (`edu_filter.augment_query`).
2. **Category filter** — when an official YouTube Data API key is configured
   (`YOUTUBE_API_KEY` in `.env`), video search is restricted to
   `videoCategoryId=27` (Education) server-side. This category signal has no
   equivalent on Invidious — it's the one thing only the official API can do.
3. **Post-fetch heuristic filter** (`app/extractors/edu_filter.py`) — applied
   to *every* result regardless of backend. A hard-exclude keyword list
   (official music videos, gameplay, vlogs, pranks, etc.) disqualifies an
   item outright; a soft positive/negative keyword list plus a channel-name
   boost (university, academy, MIT, Khan Academy, ...) ranks survivors.
   Anything under 90 seconds is treated as a Short and excluded regardless
   of text content. This layer exists specifically because neither category
   tags nor query text alone were catching everything — unit-tested in
   `test_edu_filter.py`.

**Backend selection is automatic, not configured:** if `YOUTUBE_API_KEY` is
set, the official API is tried first; on missing key, failed call, or
exhausted quota, it falls back to Invidious search with the same heuristic
filter applied. The UI shows a "degraded mode" notice when running on the
fallback, since Invidious has no category signal — filtering there is
heuristic-only and more permissive.

**Quota:** `search.list` costs 100 units per call against a 10,000/day
default quota (~99 searches/day without caching). `search_cache`
(`SEARCH_CACHE_TTL_HOURS`, default 6h) keys on `(query, course_mode)` so
repeat searches don't re-spend quota. The one follow-up `videos.list` call
per search (batched, for real per-video durations) costs 1 additional unit
regardless of result count.

**Course mode vs video mode:** the switch on the search form maps directly
to `type=playlist` vs `type=video` on whichever backend is active. Playlists
can't be filtered by category (the API parameter only applies to
`type=video`), so course-mode results rely on the heuristic layer alone —
title and channel text only, no duration signal available for playlists.

**Playlist viewer:** `/youtube/playlist/{id}` lists a playlist's videos
(official `playlistItems.list`, falling back to Invidious's
`/api/v1/playlists/{id}` if no key or the call fails) and renders the same
queue-and-player UI as the uploaded `playlist.html`. Clicking a queue item
logs to `user_history` the same way clicking a search/tab result does —
course-mode and video-mode plays land in the same "jump back in" list.

## Deviations from the original spec (and why)

- **Dates normalized to ISO 8601 at ingestion** (`app/utils.py`). The spec's
  `published_date DESC` ordering and the `date('now', '-30 days')` prune
  query both assume sortable, comparable date strings. feedparser returns
  raw RFC-822 strings (`"Wed, 06 Aug 2025 12:00:00 GMT"`), which sort
  lexicographically wrong and don't compare against `date()` at all. Every
  extractor converts through `normalize_date()` before insert. This wasn't
  in the original schema and would have caused silent, hard-to-diagnose
  ordering/pruning bugs if built as specified.
- **`is_saved` column added to all three cache tables**, per the Phase 2
  correction, with matching indices.
- **Invidious search results are not cached.** Section 2.2 doesn't specify
  whether search results persist; caching them would pollute the
  categorized tabs with one-off query noise, so search is deliberately
  ephemeral. If you want a "save from search" action, the extension point
  is `POST /save/youtube/{video_id}` — it will 404 until a matching row
  exists in `youtube_cache`, so a save-from-search action would need to
  insert the row first, not just toggle a flag.
- **Pagination added** (`?page=N`, 30/20/40 rows depending on module) —
  the original schema's unlimited `ORDER BY ... DESC` queries don't scale
  past a few ingestion cycles.

## Security note on the uploaded reference files

`fetcher.py`, as uploaded, has a real-looking Google API key hardcoded in
source. **Rotate that key in Google Cloud Console** if it's ever been
committed to a repo or shared before this — treat it as compromised
regardless. This codebase never hardcodes it; `YOUTUBE_API_KEY` is read
from `.env` only. `app.py`'s hardcoded Flask `secret_key` ('1234jitu') has
the same problem — a static, guessable secret key defeats CSRF protection.
This aggregator generates no equivalent secret (FastAPI + Jinja2, no
Flask-WTF), so there's nothing to port there, but worth fixing if that
Flask app stays in use.

## Known limitations

- **Public Invidious instances rotate and die.** The defaults in
  `app/config.py` will go stale. Check https://api.invidious.io/ before
  relying on search, and override via `INVIDIOUS_INSTANCES` in `.env`.
- **Substack scraping (Section 2.3) uses your own paid-subscriber session
  cookie to fetch full article HTML.** This is generally against Substack's
  Terms of Service regardless of whether you're a legitimate paying
  subscriber to the content — an operational/legal consideration for you
  to weigh, not something this codebase resolves. It was flagged in the
  Phase 2 review and is restated here because it's a compliance question,
  not an engineering one.
- **This build environment has no general internet access** (package
  registries only — confirmed empirically: requests to youtube.com,
  googleapis.com, and Invidious instances all return the egress proxy's
  `host_not_allowed`, not a real upstream response), so live search/ingestion
  could not be exercised end-to-end here. What was verified: every route
  against an empty and a seeded database (`smoke_test.py`, 14/14 passing),
  the educational-content heuristic filter against 8 synthetic cases
  including a real-lecture-with-no-generic-keyword case and a soft-negative
  ranking case (`test_edu_filter.py`, 10/10 passing), the search-result
  cache's miss/write/hit/TTL-expiry/course-mode-key-isolation behavior
  against synthetic results, template rendering with titles containing
  quotes and apostrophes (checking for attribute-escaping bugs), WAL mode,
  schema/index creation, save-toggle round-trips, and admin-token gating.
  Verify live search and ingestion against real feeds/API in your own
  environment before relying on it — particularly whether the heuristic
  filter's keyword lists need tuning against real result sets, which
  can't be assessed without live data.

## Project layout

```
app/
  main.py          FastAPI routes
  database.py      schema, WAL mode, connection handling
  config.py        sources.json + env + cookie secret store
  worker.py        APScheduler cycle: ingest all sources, then prune
  utils.py         date normalization
  extractors/
    youtube.py     channel-RSS cache fill; educational search (official
                   API + Invidious fallback); playlist video listing
    edu_filter.py  post-fetch educational-content heuristic filter
    substack.py    RSS + cookie-authenticated full-text + sanitization
    generic_rss.py News/Papers via feedparser
templates/         Jinja2 — base/hub/substack/feed (reading-room theme),
                   youtube/youtube_playlist (dark/crimson theme, own <head>)
static/            style.css + app.js       — reading-room modules
                   youtube-theme.css + youtube-app.js — YouTube module only
sources.json       whitelisted channels/authors/feeds
smoke_test.py      route-level checks against a live TestClient
test_edu_filter.py unit tests for the heuristic filter (no network needed)
```
