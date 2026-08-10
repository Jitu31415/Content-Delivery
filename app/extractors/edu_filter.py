"""
Post-fetch educational-content heuristic filter.

Neither the official YouTube Data API's videoCategoryId=27 (Education)
category nor a plain text search can be trusted alone: category
assignment is uploader-controlled and loosely enforced by YouTube, and
text search ranks by engagement, not academic rigor. This module is a
second, independent filtering layer applied AFTER either search backend
returns results, so a leak in one layer (e.g. a "life hack" video
mis-tagged as Education, or a channel gaming the query text) is still
caught by the other.

Two-tier design:
- HARD_EXCLUDE: near-certain non-educational signals. A single match in
  title or description disqualifies the item outright, regardless of
  category tag or query relevance.
- Positive/soft-negative keyword lists: adjust a ranking score used to
  sort survivors, not to disqualify them. An item with no positive
  keyword match is not excluded on that basis alone — the search query
  itself (via query augmentation, see augment_query()) is already the
  primary relevance signal, and a real lecture titled just "Navier-Stokes
  Equations, Lecture 12" should not be penalized for lacking the word
  "tutorial".

Duration is a hard signal where available: anything under 90 seconds is
excluded outright as a near-certain Shorts/clip, regardless of text
content — no legitimate lecture runs 90 seconds.
"""

HARD_EXCLUDE_KEYWORDS = [
    "official music video", "official video", "music video", "lyric video",
    " lyrics", "full album", "official trailer", "movie trailer",
    "teaser trailer", "asmr", "mukbang", "prank", "gameplay", "let's play",
    "speedrun", "playthrough", "unboxing", "haul video", "reaction video",
    "try not to laugh", "fail compilation", "tiktok compilation", "vlog",
    "day in my life", "get ready with me", "grwm", "storytime",
]

SOFT_NEGATIVE_KEYWORDS = [
    "funny", "meme", "compilation", "clip", "highlights", "best moments",
    "you won't believe", "shorts",
]

POSITIVE_KEYWORDS = [
    "tutorial", "lecture", "course", "class", "lesson", "explained",
    "explanation", "introduction to", "fundamentals", "crash course",
    "guide", "how to", "chapter", "walkthrough", "derivation", "proof",
    "theorem", "seminar", "workshop", "masterclass", "syllabus",
    "study", "exam prep", "revision", "textbook",
]

POSITIVE_CHANNEL_MARKERS = [
    "university", "academy", "institute", "mit", "khan academy",
    "coursera", "edx", "opencourseware", "lecture series",
]

SHORTS_DURATION_CUTOFF_SECONDS = 90


def evaluate(title: str, description: str = "", channel: str = "",
             duration_seconds: int | None = None) -> tuple[bool, int]:
    """Returns (keep, score). keep=False means hard-excluded — drop the
    item entirely. Otherwise score ranks survivors for display order
    (higher = stronger educational signal); it never excludes on its own.
    """
    if duration_seconds is not None and duration_seconds < SHORTS_DURATION_CUTOFF_SECONDS:
        return False, 0

    text = f"{title} {description}".lower()
    for kw in HARD_EXCLUDE_KEYWORDS:
        if kw in text:
            return False, 0

    score = 0
    for kw in POSITIVE_KEYWORDS:
        if kw in text:
            score += 1
    for kw in SOFT_NEGATIVE_KEYWORDS:
        if kw in text:
            score -= 1

    channel_lower = (channel or "").lower()
    for marker in POSITIVE_CHANNEL_MARKERS:
        if marker in channel_lower:
            score += 2
            break  # one channel-level boost is enough signal; don't stack

    return True, score


def augment_query(query: str) -> str:
    """First filtering layer, applied before either search backend runs.
    Broader synonym set and a tighter exclusion list than a bare keyword
    OR-chain, but still just query text — this alone is not sufficient,
    hence evaluate() above as an independent second pass."""
    include = "(tutorial OR lecture OR course OR explanation OR lesson OR guide)"
    exclude = (
        '-vlog -reaction -prank -gameplay -"let\'s play" -asmr -mukbang '
        '-"official music video"'
    )
    return f"{query} {include} {exclude}"
