"""
Allowlisted reader view: lets the app open articles from the configured
sources without the WebView ever leaving this site's origin.

Why this exists: once a WebView is on a third-party page (aeon.co, a news
site), that page's own links can lead anywhere, and nothing served by this
app can intercept them. So articles are fetched server-side, stripped down to
text and images, and every link in them is either rewritten to /read?u=...
(if the target host is an allowed source) or made inert. The WebView only
ever loads same-origin pages.

Allowed hosts (see allowed_hosts()):
  * the host of every feed URL in sources.json (news / papers / blogs),
  * <author>.substack.com for each Substack author,
  * the optional top-level "reader_hosts" list in sources.json,
  * the optional READER_EXTRA_HOSTS env var (comma separated).
A "www." prefix is treated as equivalent. Hosts are NOT derived from the item
URLs stored in the database: a feed controls those URLs, so deriving from them
would let any feed allowlist youtube.com or a redirector by linking to it. Use
/admin/reader-hosts to review which hosts your items point at, then add the
ones you trust to "reader_hosts".

Trimming (READER_TRIM env var, default "light"):
  light   Generous. Removes only high-confidence junk: ad containers and ad
          labels, cookie/consent/modal overlays, tracking images, a few
          stand-alone labels ("Share", "Tweet"), and "The post X appeared first
          on Y"/copyright lines. Keeps everything else, including link lists,
          asides, footers and collapsed sections inside the article.
  strict  Aggressive. Also removes related/share/comment/newsletter blocks by
          class name, link-dense blocks, hidden and aria-hidden elements,
          call-to-action lines, and any <nav>/<aside>/<footer>.
The "Full page" view is a third, separate mode: the whole page, minus ads.

This is an unauthenticated endpoint, so it is hardened as a proxy:
  * only http(s), default ports, no credentials, no IP literals;
  * every hop of every redirect is re-checked against the allowlist;
  * the host must resolve to public addresses only (defence in depth; it does
    not defend against DNS rebinding by an allowlisted domain's owner);
  * HTML only, 3 MB cap, 10 s timeout, no cookies sent or kept.
"""

import html as _html
import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from app import config

READ_PATH = "/read"
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
MAX_URL_LEN = 2048
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "en",
}

# Same normalisation browsers apply to URLs (strip control chars; "\" == "/").
_CTRL = re.compile(r"[\x00-\x20\x7f]")


class ReaderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- allowlist

def derive_hosts(sources: dict, extra=()) -> frozenset:
    hosts = set()

    def add(h):
        h = (h or "").strip().lower().rstrip(".")
        if h:
            hosts.add(h)

    for key in ("news", "papers", "blogs"):
        for feed in sources.get(key) or []:
            add(urlsplit(feed.get("url", "")).hostname)
    for author in sources.get("substack") or []:
        add(f"{author}.substack.com")
    for h in sources.get("reader_hosts") or []:
        add(h)
    for h in extra:
        add(h)
    return frozenset(hosts)


_cache = {"key": None, "hosts": frozenset()}


def allowed_hosts() -> frozenset:
    """Cached on sources.json's mtime and the env var; no database access, so
    it is safe to call from middleware on the event loop."""
    try:
        mtime = os.stat(config.SOURCES_PATH).st_mtime
    except OSError:
        mtime = None
    extra = tuple(h for h in os.environ.get("READER_EXTRA_HOSTS", "").split(",") if h.strip())
    key = (mtime, extra)
    if _cache["key"] != key:
        try:
            sources = config.load_sources()
        except Exception:
            sources = {}
        _cache["hosts"] = derive_hosts(sources, extra)
        _cache["key"] = key
    return _cache["hosts"]


def host_allowed(host, allowed) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    if host in allowed:
        return True
    if host.startswith("www.") and host[4:] in allowed:
        return True
    return ("www." + host) in allowed


def reader_href(href, base_url, allowed):
    """/read?u=<absolute url> if `href` points at an allowed host over http(s),
    else None. Fragments are dropped (the reader re-renders the page)."""
    cleaned = _CTRL.sub("", (href or "").strip()).replace("\\", "/")
    if not cleaned:
        return None
    try:
        parts = urlsplit(urljoin(base_url, cleaned))
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return None
    if not host_allowed(parts.hostname, allowed):
        return None
    return f"{READ_PATH}?u={quote(parts._replace(fragment='').geturl(), safe='')}"


# ---------------------------------------------------------------- fetching

def _resolves_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {i[4][0].split("%")[0] for i in infos}
    return bool(addrs) and all(ipaddress.ip_address(a).is_global for a in addrs)


def _check_url(url: str, allowed) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ReaderError(400, "That address is not valid.")
    if parts.scheme not in ("http", "https"):
        raise ReaderError(400, "Only web addresses can be opened.")
    if "@" in parts.netloc or parts.username or parts.password:
        raise ReaderError(400, "That address is not valid.")
    host = (parts.hostname or "").lower()
    if not host or port not in (None, 80, 443):
        raise ReaderError(400, "That address is not valid.")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        raise ReaderError(403, "Direct IP addresses are not allowed.")
    if not host_allowed(host, allowed):
        raise ReaderError(403, f"{host} is not one of your sources.")
    if not _resolves_public(host):
        raise ReaderError(403, f"{host} did not resolve to a public address.")


def _new_client() -> httpx.Client:
    # No cookie persistence across requests: a new client per fetch.
    return httpx.Client(headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=False)


def fetch_page(url: str, allowed, client=None):
    """Returns (final_url, body_bytes). Every redirect hop is re-validated."""
    own = client is None
    client = client or _new_client()
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            _check_url(current, allowed)
            try:
                with client.stream("GET", current) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        location = r.headers.get("location")
                        if not location:
                            raise ReaderError(502, "The site sent a broken redirect.")
                        current = urljoin(current, location)
                        continue
                    if r.status_code >= 400:
                        raise ReaderError(502, f"The site answered with HTTP {r.status_code}.")
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in ("text/html", "application/xhtml+xml"):
                        raise ReaderError(415, "This link is not a web page (PDFs and downloads are not supported).")
                    data = bytearray()
                    for chunk in r.iter_bytes():
                        data += chunk
                        if len(data) > MAX_BYTES:
                            raise ReaderError(502, "That page is too large to open.")
                    return current, bytes(data)
            except httpx.HTTPError:
                raise ReaderError(502, "The page could not be fetched.")
        raise ReaderError(502, "Too many redirects.")
    finally:
        if own:
            client.close()


# ---------------------------------------------------------------- extraction

_DROP_ALWAYS = ["script", "style", "noscript", "svg", "iframe", "frame", "frameset", "object",
                "embed", "video", "audio", "canvas", "form", "button", "input", "select",
                "textarea", "dialog", "template", "source", "link", "meta", "base", "ins",
                "fencedframe"]
_DROP_READER = ["nav", "aside", "footer"]

# Class/id TOKENS (split on non-alphanumerics and camelCase), not substrings:
# "ad" must hit "article-ad-slot" and "adSlot" but not "header", "read" or "shadow".
_AD_TOKENS = {"ad", "ads", "adv", "advert", "advertisement", "advertising", "adslot", "adsbygoogle",
              "adunit", "sponsor", "sponsors", "sponsored", "promoted", "dfp", "taboola", "outbrain",
              "googlead"}
_NOISE_TOKENS = _AD_TOKENS | {
    "promo", "promotion", "newsletter", "signup", "subscribe", "subscription", "paywall", "share",
    "sharing", "social", "related", "recommended", "recommendation", "trending", "comments",
    "comment", "cookie", "cookies", "consent", "gdpr", "popup", "modal", "sidebar", "breadcrumb",
    "breadcrumbs", "widget", "widgets", "donate", "donation", "toolbar", "skip", "print",
}
# "light" trimming: only things that are junk on essentially every page.
_OVERLAY_TOKENS = {"cookie", "cookies", "consent", "gdpr", "popup", "modal"}
_LIGHT_TOKENS = _AD_TOKENS | _OVERLAY_TOKENS
_LIGHT_ROLES = {"dialog", "alertdialog"}
_AD_SRC_LIGHT = re.compile(r"doubleclick|googlesyndication|adservice|adsystem|scorecardresearch|"
                           r"facebook\.com/tr|outbrain|taboola", re.I)
_AD_ALT_LIGHT = {"advertisement", "advert", "ad", "sponsored"}
_LIGHT_LABEL = re.compile(
    r"^(sponsored by .{1,40}|share( this( article| story| post| page)?)?|tweet|email|print|save|"
    r"back to top|skip( to (main )?content)?)$")
_SAFE_BOILER = re.compile(
    r"^(the post .{1,200} appeared first on\b|all rights reserved|\u00a9|copyright (\u00a9|\(c\)|\d{4}))")
_SR_ONLY = re.compile(r"sr-only|screen-reader-text|visually-?hidden|assistive-text", re.I)
_NOISE_ROLES = {"complementary", "banner", "navigation", "contentinfo", "dialog", "alertdialog",
                "search", "toolbar"}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_AD_SRC = re.compile(r"doubleclick|googlesyndication|adservice|adsystem|/ads?/|pixel|scorecardresearch|"
                     r"facebook\.com/tr|outbrain|taboola|analytics|beacon|tracker", re.I)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Short elements that are only a label, or open with an unmistakable call to action.
_AD_LABEL = re.compile(
    r"^(advertisement|advertising|advertorial|sponsored( content| post)?|promoted( content)?|"
    r"paid (content|post)|scroll to continue( reading)?|story continues( below)?( advertisement)?|"
    r"advertisement\s*[-\u2013\u2014]\s*scroll to continue( reading)?|continue reading below)$")
_LABEL_ONLY = re.compile(
    r"^(sponsored by .{1,40}|ads?|related( (stories|articles|reading|posts|content|coverage|topics))?|"
    r"more (stories|from .{1,40}|on this topic|like this)|recommended( for you)?|trending( now| stories)?|"
    r"share( this( article| story| post| page)?)?|tweet|email|print|save|(\d+ )?comments?|read more|"
    r"read also|also read|continue reading|newsletter|subscribe|sign up|follow us|listen|"
    r"listen to this article|skip( to (main )?content)?|back to top|top|see also|you may also like)$")
_CTA_PREFIX = re.compile(
    r"^(sign ?up (for|to|now|today)\b|subscribe (to|now|today|for)\b|follow us\b|"
    r"get (our|the) (latest|newsletter|best|daily|weekly|free)\b|join (our|the) (newsletter|mailing list|community)\b|"
    r"download (our|the) (free )?app\b|click here (to|for)\b|scroll to continue\b|story continues\b|"
    r"continue reading\b|read more:|read also:|also read:|related:|related (stories|articles|reading)\b|"
    r"the post .{1,200} appeared first on\b|this (article|story) (was )?(originally )?(published|appeared) (in|on|at)\b|"
    r"all rights reserved|\u00a9|copyright (\u00a9|\(c\)|\d{4})|we use cookies|this (site|website) uses cookies|"
    r"(accept|manage) (all )?cookies|support (our|independent) (journalism|work|reporting)\b|"
    r"become a (member|subscriber|supporter)\b)")
_TEXT_BLOCKS = ["p", "div", "li", "h2", "h3", "h4", "h5", "h6", "figcaption", "dt", "dd", "small", "cite"]
_PRUNABLE = ["p", "div", "li", "ul", "ol", "figure", "blockquote", "h2", "h3", "h4", "h5", "h6",
             "figcaption", "pre", "dl"]

_ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd", "blockquote",
    "pre", "code", "em", "strong", "b", "i", "u", "sub", "sup", "br", "hr", "small", "mark",
    "abbr", "cite", "time", "figure", "figcaption", "table", "thead", "tbody", "tfoot", "tr",
    "th", "td", "a", "img", "div",
}
_IMG_SOURCES = ("src", "data-src", "data-lazy-src", "data-original")
_MIN_TEXT = 400
_SEP = re.compile(r"\s[|\u2013\u2014\u00b7\u2022]\s|\s-\s")


@dataclass
class Article:
    title: str
    body_html: str
    text_len: int
    words: int
    site: str
    author: str
    published: str
    lang: str


def _text_len(el) -> int:
    return len(el.get_text(" ", strip=True))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).strip(" :-\u2013\u2014.\u2026\u00bb\u203a\u2192|\u2022")


def _trim_level() -> str:
    return "strict" if os.environ.get("READER_TRIM", "light").strip().lower() == "strict" else "light"


def _pick_container(soup):
    best, best_len = None, 0
    for art in soup.find_all("article"):
        n = _text_len(art)
        if n > best_len:
            best, best_len = art, n
    if best is not None and best_len >= _MIN_TEXT:
        return best
    for sel in ("main", "[role=main]", "#content", "#main", ".entry-content", ".post-content", ".article-body"):
        el = soup.select_one(sel)
        if el is not None and _text_len(el) >= _MIN_TEXT:
            return el
    return soup.body or soup


# ---- page metadata

def _meta_content(soup, **attrs) -> str:
    el = soup.find("meta", attrs=attrs)
    return (el.get("content") or "").strip() if el is not None else ""


def _fmt_date(value: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    if not m:
        return ""
    try:
        d = date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return ""
    return f"{d.day} {d.strftime('%B %Y')}"


def _page_meta(soup, host: str) -> dict:
    title = _meta_content(soup, property="og:title")
    if not title and soup.title is not None:
        title = soup.title.get_text(strip=True)
    site = _meta_content(soup, property="og:site_name") or host.removeprefix("www.")
    author = _meta_content(soup, name="author")
    if not author or author.lower().startswith("http") or len(author) > 80:
        author = ""
    published = ""
    for attrs in ({"property": "article:published_time"}, {"itemprop": "datePublished"},
                  {"name": "date"}, {"name": "pubdate"}):
        published = _fmt_date(_meta_content(soup, **attrs))
        if published:
            break
    if not published:
        t = soup.find("time", attrs={"datetime": True})
        published = _fmt_date(t["datetime"]) if t is not None else ""
    lang = ""
    if soup.html is not None:
        lang = (soup.html.get("lang") or "").strip()
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", lang):
        lang = "en"
    return {"title": title, "site": site[:80], "author": author, "published": published, "lang": lang}


def _trim_site_suffix(title: str) -> str:
    parts = [p.strip() for p in _SEP.split(title or "") if p.strip()]
    return max(parts, key=len) if parts else (title or "").strip()


def _choose_title(meta_title: str, container, host: str) -> str:
    h1 = container.find("h1")
    if h1 is not None:
        t = " ".join(h1.get_text(" ", strip=True).split())
        if 8 <= len(t) <= 220:
            return t
    return _trim_site_suffix(meta_title) or host


# ---- noise removal

def _attr_tokens(tag):
    raw = " ".join([" ".join(tag.get("class") or []), tag.get("id") or ""])
    raw = _CAMEL.sub(" ", raw).lower()
    return set(re.findall(r"[a-z0-9]+", raw)), raw


def _is_hidden(tag) -> bool:
    if tag.has_attr("hidden") or (tag.get("aria-hidden") or "").lower() == "true":
        return True
    return bool(_HIDDEN_STYLE.search(tag.get("style") or ""))


def _remove_noise(container, mode: str) -> None:
    """mode "strict": class-name noise, roles, hidden elements (the aggressive
    rules). mode "light": ad containers and overlays only; hidden elements are
    kept because collapsed sections and "read more" text are real content.
    mode "full" (Full page view): ad containers and hidden elements only."""
    if mode == "strict":
        tokens, roles, drop_hidden, sr_only = _NOISE_TOKENS, _NOISE_ROLES, True, True
    elif mode == "full":
        tokens, roles, drop_hidden, sr_only = _AD_TOKENS, set(), True, False
    else:
        tokens, roles, drop_hidden, sr_only = _LIGHT_TOKENS, _LIGHT_ROLES, False, True
    for tag in list(container.find_all(True)):
        if tag is container or tag.attrs is None:
            continue
        if drop_hidden and _is_hidden(tag):
            tag.extract()
            continue
        toks, raw = _attr_tokens(tag)
        if toks & tokens:
            tag.extract()
        elif (sr_only and _SR_ONLY.search(raw)) or (roles and (tag.get("role") or "").lower() in roles):
            tag.extract()
        elif any(k.startswith(("data-ad", "data-google", "data-dfp")) for k in tag.attrs):
            tag.extract()


def _link_dense(tag) -> bool:
    """Blocks that are mostly links ("more stories", tag clouds, link bars).
    A list whose links are all in-page anchors is a table of contents: kept."""
    anchors = tag.find_all("a")
    if len(anchors) < 3:
        return False
    if all((a.get("href") or "").startswith("#") for a in anchors):
        return False
    total = _text_len(tag)
    if total == 0:
        return True
    link_text = sum(_text_len(a) for a in anchors)
    return link_text / total > 0.8 and link_text / len(anchors) < 80


def _remove_link_dense(container) -> None:
    for tag in list(container.find_all(["ul", "ol", "div", "section", "p", "table"])):
        if tag is not container and tag.parent is not None and _link_dense(tag):
            tag.extract()


# ---- cleaning

def _clean_img(tag, page_url: str, mode: str) -> None:
    src = ""
    for attr in _IMG_SOURCES:
        candidate = (tag.get(attr) or "").strip()
        if candidate and not candidate.lower().startswith("data:"):
            src = candidate
            break
    alt = tag.get("alt", "")
    tiny = any(str(tag.get(k, "")).strip().isdigit() and int(str(tag.get(k)).strip()) <= 3
               for k in ("width", "height"))
    tag.attrs = {}
    try:
        parts = urlsplit(urljoin(page_url, _CTRL.sub("", src).replace("\\", "/"))) if src else None
    except ValueError:
        parts = None
    if mode == "strict":
        ad_like = bool(_AD_SRC.search(src)) or "advert" in alt.lower()
    else:   # "pixel", "analytics" or "advert" in a name/alt is usually a real image
        ad_like = bool(_AD_SRC_LIGHT.search(src)) or alt.strip().lower() in _AD_ALT_LIGHT
    if not parts or parts.scheme not in ("http", "https") or "@" in parts.netloc or tiny or ad_like:
        tag.decompose()
        return
    tag.attrs = {"src": parts._replace(scheme="https").geturl(), "alt": alt, "loading": "lazy",
                 "decoding": "async"}


def _frag(value: str) -> str:
    """Element ids from the source page are kept (so tables of contents and
    footnotes still jump), but prefixed so they can never collide with this
    page's own ids (e.g. the progress bar)."""
    return "r-" + re.sub(r"[^\w.:-]", "-", unquote(value))


def _same_page_fragment(href: str, page_url: str) -> str:
    try:
        target = urlsplit(urljoin(page_url, _CTRL.sub("", href).replace("\\", "/")))
    except ValueError:
        return ""
    base = urlsplit(page_url)
    if target.fragment and target._replace(fragment="").geturl() == base._replace(fragment="").geturl():
        return target.fragment
    return ""


def _clean_anchor(tag, page_url: str, allowed) -> None:
    href = (tag.get("href") or "").strip()
    if href.startswith("#") and len(href) > 1:
        tag.attrs = {"href": "#" + _frag(href[1:])}
        return
    frag = _same_page_fragment(href, page_url)
    if frag:                      # absolute URL pointing back into this same article
        tag.attrs = {"href": "#" + _frag(frag)}
        return
    via = reader_href(href, page_url, allowed)
    if via:
        tag.attrs = {"href": via}
    else:
        tag.name = "span"
        tag.attrs = {"class": ["blocked-link"], "title": "Not one of your sources"}


def _strip_boilerplate(container, mode: str) -> None:
    """Runs after cleaning, when spans are unwrapped: elements that are
    nothing but 'Advertisement', 'Share', 'Sign up for ...', 'The post X
    appeared first on Y', copyright lines, and so on. Only short elements
    are considered, and call-to-action patterns must open the text."""
    for tag in list(container.find_all(_TEXT_BLOCKS)):
        if tag is container or tag.parent is None:
            continue
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 200:
            continue
        n = _norm(text)
        if _AD_LABEL.match(n):
            drop = True
        elif mode == "strict":
            drop = bool(_LABEL_ONLY.match(n) or _CTA_PREFIX.match(n))
        elif mode == "light":
            drop = bool(_LIGHT_LABEL.match(n) or _SAFE_BOILER.match(n))
        else:
            drop = False
        if drop:
            tag.extract()


def _drop_title_duplicates(container, title: str, mode: str) -> None:
    """The page header already shows the title; drop the body's own copy.
    Light mode needs a near-identical heading, so a real section heading that
    merely appears inside the title (e.g. "The River") is kept."""
    t = _norm(title)
    for h in container.find_all(["h1", "h2", "h3"], limit=3):
        h_norm = _norm(h.get_text(" ", strip=True))
        if not h_norm:
            continue
        if h_norm == t:
            h.extract()
        elif len(h_norm) >= 8 and (h_norm in t or t in h_norm):
            if mode == "strict" or min(len(h_norm), len(t)) / max(len(h_norm), len(t)) >= 0.85:
                h.extract()


def _prune_empty(container) -> None:
    for _ in range(3):
        for tag in list(container.find_all(_PRUNABLE)):
            if tag.parent is not None and not tag.get_text(strip=True) and tag.find("img") is None:
                tag.extract()
    # Collapse runs of <br>/<hr>, and trim them from both ends.
    prev = None
    for tag in list(container.find_all(["br", "hr"])):
        sib = tag.previous_sibling
        while sib is not None and isinstance(sib, str) and not sib.strip():
            sib = sib.previous_sibling
        if sib is not None and getattr(sib, "name", None) == tag.name:
            tag.extract()
    for end in ("first", "last"):
        while True:
            kids = [c for c in container.children if getattr(c, "name", None) or (isinstance(c, str) and c.strip())]
            if not kids:
                break
            node = kids[0] if end == "first" else kids[-1]
            if getattr(node, "name", None) in ("br", "hr"):
                node.extract()
            else:
                break


def _mark_lede(container) -> None:
    for p in container.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) >= 120:
            if text[0].isascii() and text[0].isalpha():
                p["class"] = ["lede"]
            return


def extract(data: bytes, page_url: str, allowed, full: bool = False, trim: str = None) -> Article:
    soup = BeautifulSoup(data, "html.parser")
    host = urlsplit(page_url).hostname or ""
    meta = _page_meta(soup, host)

    for tag in soup.find_all(_DROP_ALWAYS):
        tag.decompose()
    for tag in soup.find_all(lambda t: bool(t.name) and t.name.startswith("amp-")):
        tag.decompose()

    trim = trim or _trim_level()
    mode = "full" if full else trim
    if full:
        container = soup.body or soup
    elif trim == "strict":
        for tag in soup.find_all(_DROP_READER):
            tag.decompose()
        container = _pick_container(soup)
    else:
        container = _pick_container(soup)
        # Site chrome only needs dropping when we fell back to a wide container.
        # Inside an <article>, asides/footers/navs are pull quotes, author
        # notes and pagination: part of the piece.
        if container.name != "article":
            for tag in container.find_all(_DROP_READER):
                tag.decompose()

    _remove_noise(container, mode)
    if mode == "strict":
        _remove_link_dense(container)
    title = _choose_title(meta["title"], container, host)

    for tag in list(container.find_all(True)):
        if tag.parent is None and tag is not container:
            continue
        if tag.name == "h1":
            tag.name = "h2"          # the page header carries the only h1
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()
        elif tag.name == "a":
            _clean_anchor(tag, page_url, allowed)
        elif tag.name == "img":
            _clean_img(tag, page_url, mode)
        elif tag.name in ("td", "th"):
            tag.attrs = {k: v for k, v in tag.attrs.items() if k in ("colspan", "rowspan")}
        else:
            keep_id = tag.get("id")
            tag.attrs = {"id": _frag(keep_id)} if keep_id else {}

    _strip_boilerplate(container, mode)
    _drop_title_duplicates(container, title, mode)
    _prune_empty(container)
    _mark_lede(container)

    return Article(
        title=title, body_html=container.decode_contents(), text_len=_text_len(container),
        words=len(container.get_text(" ").split()), site=meta["site"], author=meta["author"],
        published=meta["published"], lang=meta["lang"],
    )


# ---------------------------------------------------------------- rendering

_CSS = """
:root{--fs:18px;--bg:#fbfaf6;--fg:#1f1d1a;--muted:#6f6a60;--rule:#e4dfd3;--card:#f1ede2;--accent:#a3411c}
html[data-theme=sepia]{--bg:#f3ead6;--fg:#3b2f20;--muted:#7a6a52;--rule:#dccdae;--card:#eadfc4;--accent:#8a4b12}
html[data-theme=dark]{--bg:#151412;--fg:#e9e5dc;--muted:#9b958a;--rule:#2c2a26;--card:#1f1d1a;--accent:#e7a35f}
@media(prefers-color-scheme:dark){html:not([data-theme]){--bg:#151412;--fg:#e9e5dc;--muted:#9b958a;--rule:#2c2a26;--card:#1f1d1a;--accent:#e7a35f}}
*{box-sizing:border-box}
html{font-size:var(--fs);-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);
font-family:Charter,"Iowan Old Style","Palatino Linotype","Book Antiqua",Georgia,"Noto Serif","Noto Serif Bengali",serif;
line-height:1.7;text-rendering:optimizeLegibility}
::selection{background:var(--accent);color:var(--bg)}
a{color:var(--accent);text-underline-offset:.18em;text-decoration-thickness:1px}
button,.bar a{font:600 .78rem system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
#rp{position:fixed;top:0;left:0;height:3px;width:0;background:var(--accent);z-index:20}
.bar{position:sticky;top:0;z-index:10;display:flex;gap:.55rem;align-items:center;
padding:.55rem max(.9rem,env(safe-area-inset-right)) .55rem max(.9rem,env(safe-area-inset-left));
background:var(--bg);background:color-mix(in srgb,var(--bg) 88%,transparent);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
border-bottom:1px solid var(--rule)}
.bar .host{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);
font:500 .78rem system-ui,sans-serif;letter-spacing:.02em}
.bar button{color:var(--fg);background:var(--card);border:1px solid var(--rule);border-radius:.55rem;
padding:.32rem .7rem;cursor:pointer}
.bar a{color:var(--fg);text-decoration:none;padding:.32rem .2rem}
main{max-width:38rem;margin:0 auto;padding:2.2rem max(1.25rem,env(safe-area-inset-left)) 1rem}
.head{margin-bottom:2rem;padding-bottom:1.4rem;border-bottom:1px solid var(--rule)}
.kicker{font:700 .72rem system-ui,sans-serif;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);margin-bottom:.8rem}
h1{font-size:2.2rem;font-size:clamp(1.9rem,6vw,2.6rem);line-height:1.12;letter-spacing:-.015em;margin:0 0 .9rem;
font-weight:800;text-wrap:balance}
.meta{font:500 .8rem system-ui,sans-serif;color:var(--muted);letter-spacing:.02em}
.prose{hyphens:auto;overflow-wrap:break-word}
.prose p{margin:0 0 1.15em;text-wrap:pretty}
.prose .lede{font-size:1.1em;line-height:1.65}
.prose .lede::first-line{font-variant:small-caps;letter-spacing:.04em;font-weight:600}
.prose h2,.prose h3,.prose h4,.prose h5,.prose h6{line-height:1.25;margin:2em 0 .6em;text-wrap:balance}
.prose h2{font-size:1.5rem;font-weight:800}.prose h3{font-size:1.2rem;font-weight:700}
.prose h4,.prose h5,.prose h6{font-size:1rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.prose blockquote{margin:1.6em 0;padding:.1em 0 .1em 1.1em;border-left:3px solid var(--accent);
font-style:italic;color:var(--muted);color:color-mix(in srgb,var(--fg) 82%,var(--muted))}
.prose blockquote p:last-child{margin-bottom:0}
.prose hr{border:0;text-align:center;margin:2.4em 0}
.prose hr::after{content:"\\00b7  \\00b7  \\00b7";letter-spacing:.5em;color:var(--muted)}
.prose figure{margin:1.8em 0}.prose figure img{display:block;margin:0 auto}
.prose img{max-width:100%;height:auto;border-radius:.4rem}
.prose figcaption{margin-top:.6em;text-align:center;font:.78rem/1.5 system-ui,sans-serif;color:var(--muted)}
.prose ul,.prose ol{padding-left:1.4em;margin:0 0 1.15em}.prose li{margin:.3em 0}
.prose code{font:.85em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--card);padding:.1em .35em;border-radius:.3rem}
.prose pre{background:var(--card);border:1px solid var(--rule);border-radius:.6rem;padding:.9rem 1rem;overflow:auto;line-height:1.5}
.prose pre code{background:none;padding:0}
.prose table{display:block;overflow-x:auto;border-collapse:collapse;margin:1.4em 0;font-size:.9em}
.prose th,.prose td{border:1px solid var(--rule);padding:.4em .7em;text-align:left}
.prose tr:nth-child(even) td{background:var(--card)}
.blocked-link{text-decoration:underline dotted;text-underline-offset:.18em}
.note{font:.85rem system-ui,sans-serif;color:var(--muted);border-left:3px solid var(--rule);padding-left:.8rem;margin:0 0 1.6rem}
.end{max-width:38rem;margin:1.5rem auto 4rem;padding:1.6rem max(1.25rem,env(safe-area-inset-left)) 0;text-align:center;
font:500 .78rem system-ui,sans-serif;color:var(--muted);letter-spacing:.04em}
.end .mark{display:block;margin-bottom:1rem;letter-spacing:.5em;color:var(--accent)}
.end button,.end a{margin:0 .3rem;color:var(--fg);background:var(--card);border:1px solid var(--rule);
border-radius:.55rem;padding:.4rem .9rem;cursor:pointer;text-decoration:none;font:600 .78rem system-ui,sans-serif}
.end .src{display:block;margin-top:1rem}
@media print{.bar,#rp,.end button,.end a{display:none}}
"""

# Runs in <head>: restores the saved theme and text size before first paint (no flash).
_JS_HEAD = """(function(){var d=document.documentElement,S;try{S=window.localStorage}catch(e){}
function g(k){try{return S.getItem(k)}catch(e){return null}}
var fs=parseInt(g('rr_fs'),10);if(fs>=14&&fs<=28)d.style.setProperty('--fs',fs+'px');
var th=g('rr_theme');if(th==='light'||th==='sepia'||th==='dark')d.setAttribute('data-theme',th);})();"""

_JS_BODY = """(function(){var d=document.documentElement,S;try{S=window.localStorage}catch(e){}
function s(k,v){try{S.setItem(k,v)}catch(e){}}
window.rrSize=function(n){var c=parseFloat(getComputedStyle(d).getPropertyValue('--fs'))||18;
c=Math.max(14,Math.min(28,c+n));d.style.setProperty('--fs',c+'px');s('rr_fs',String(c));};
window.rrTheme=function(){var o=['light','sepia','dark'];
var c=d.getAttribute('data-theme')||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
var n=o[(o.indexOf(c)+1)%3];d.setAttribute('data-theme',n);s('rr_theme',n);};
var b=document.getElementById('rp');
function p(){var h=d.scrollHeight-innerHeight;b.style.width=(h>0?Math.min(100,scrollY/h*100):0)+'%';}
addEventListener('scroll',p,{passive:true});addEventListener('resize',p);p();})();"""


def _page(title: str, bar: str, body: str, tail: str = "", lang: str = "en") -> str:
    return (
        '<!DOCTYPE html><html lang="%s"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="color-scheme" content="light dark">'
        "<title>%s</title><style>%s</style><script>%s</script></head><body>"
        '<div id="rp"></div><div class="bar">%s</div><main>%s</main>%s<script>%s</script></body></html>'
    ) % (lang, _html.escape(title), _CSS, _JS_HEAD, bar, body, tail, _JS_BODY)


def render_article(art: Article, final_url: str, full: bool) -> str:
    host = _html.escape(urlsplit(final_url).hostname or "")
    toggle_href = f"{READ_PATH}?u={quote(final_url, safe='')}" + ("" if full else "&amp;full=1")
    toggle_label = "Reader view" if full else "Full page"
    bar = (
        '<button onclick="history.back()" aria-label="Back">&larr;</button><a href="/">Home</a>'
        f'<span class="host">{host}</span>'
        '<button onclick="rrSize(-1)" aria-label="Smaller text">A&minus;</button>'
        '<button onclick="rrSize(1)" aria-label="Larger text">A+</button>'
        '<button onclick="rrTheme()" aria-label="Change theme">&#9680;</button>'
        f'<a href="{toggle_href}">{toggle_label}</a>'
    )
    meta_parts = [p for p in (art.author, art.published,
                              f"{max(1, round(art.words / 220))} min read" if art.words >= 150 else "") if p]
    meta = f'<div class="meta">{_html.escape(" · ".join(meta_parts))}</div>' if meta_parts else ""
    note = ""
    if not full and art.text_len < _MIN_TEXT:
        note = '<p class="note">Very little text was found. Try &ldquo;Full page&rdquo; above.</p>'
    body = (
        f'{note}<header class="head"><div class="kicker">{_html.escape(art.site)}</div>'
        f"<h1>{_html.escape(art.title)}</h1>{meta}</header>"
        f'<div class="prose">{art.body_html}</div>'
    )
    tail = (
        '<div class="end"><span class="mark">&middot; &middot; &middot;</span>'
        '<button onclick="history.back()">&larr; Back</button><a href="/">Home</a>'
        f'<span class="src">Source: {host}</span></div>'
    )
    return _page(art.title, bar, body, tail, art.lang)


def error_page(err: ReaderError) -> str:
    bar = ('<button onclick="history.back()" aria-label="Back">&larr;</button><a href="/">Home</a>'
           '<span class="host"></span>')
    body = ('<header class="head"><div class="kicker">Cannot open</div><h1>This page cannot be opened</h1></header>'
            f'<div class="prose"><p>{_html.escape(err.message)}</p></div>')
    return _page("Cannot open", bar, body)


def read(u: str, full: bool = False, allowed=None) -> str:
    allowed = allowed_hosts() if allowed is None else allowed
    u = (u or "").strip()
    if not u or len(u) > MAX_URL_LEN:
        raise ReaderError(400, "That address is not valid.")
    final_url, data = fetch_page(u, allowed)
    return render_article(extract(data, final_url, allowed, full), final_url, full)
