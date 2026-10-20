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
from urllib.parse import quote, urljoin, urlsplit

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
        clean = author.strip("/")
        add(clean if "." in clean else f"{clean}.substack.com")
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
                "textarea", "dialog", "template", "source", "link", "meta", "base"]
_DROP_READER = ["nav", "aside", "footer"]
_NOISE = re.compile(
    r"comment|share|social|newsletter|subscribe|related|promo|advert|cookie|consent|sidebar|popup|modal|donat",
    re.I)
_ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd", "blockquote",
    "pre", "code", "em", "strong", "b", "i", "u", "sub", "sup", "br", "hr", "small", "mark",
    "abbr", "cite", "time", "figure", "figcaption", "table", "thead", "tbody", "tfoot", "tr",
    "th", "td", "a", "img", "div",
}
_IMG_SOURCES = ("src", "data-src", "data-lazy-src", "data-original")
_MIN_TEXT = 400


def _text_len(el) -> int:
    return len(el.get_text(" ", strip=True))


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


def _title(soup, fallback: str) -> str:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return fallback


def _clean_img(tag, page_url: str) -> None:
    src = ""
    for attr in _IMG_SOURCES:
        candidate = (tag.get(attr) or "").strip()
        if candidate and not candidate.lower().startswith("data:"):
            src = candidate
            break
    alt = tag.get("alt", "")
    tag.attrs = {}
    try:
        parts = urlsplit(urljoin(page_url, _CTRL.sub("", src).replace("\\", "/"))) if src else None
    except ValueError:
        parts = None
    if not parts or parts.scheme not in ("http", "https") or "@" in parts.netloc:
        tag.decompose()
        return
    tag.attrs = {"src": parts._replace(scheme="https").geturl(), "alt": alt, "loading": "lazy"}


def _clean_anchor(tag, page_url: str, allowed) -> None:
    href = (tag.get("href") or "").strip()
    if href.startswith("#") and len(href) > 1:
        tag.attrs = {"href": href}
        return
    via = reader_href(href, page_url, allowed)
    if via:
        tag.attrs = {"href": via}
    else:
        tag.name = "span"
        tag.attrs = {"class": ["blocked-link"], "title": "Not one of your sources"}


def extract(data: bytes, page_url: str, allowed, full: bool = False):
    """Returns (title, body_html, text_length)."""
    soup = BeautifulSoup(data, "html.parser")
    host = urlsplit(page_url).hostname or ""
    title = _title(soup, host)

    for tag in soup.find_all(_DROP_ALWAYS):
        tag.decompose()

    if full:
        container = soup.body or soup
    else:
        for tag in soup.find_all(_DROP_READER):
            tag.decompose()
        container = _pick_container(soup)
        for tag in container.find_all(True):
            if tag.attrs is None:
                continue
            ident = " ".join(filter(None, [" ".join(tag.get("class") or []), tag.get("id") or ""]))
            if ident and _NOISE.search(ident):
                tag.extract()

    for tag in list(container.find_all(True)):
        if tag.parent is None and tag is not container:
            continue
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()
        elif tag.name == "a":
            _clean_anchor(tag, page_url, allowed)
        elif tag.name == "img":
            _clean_img(tag, page_url)
        elif tag.name in ("td", "th"):
            tag.attrs = {k: v for k, v in tag.attrs.items() if k in ("colspan", "rowspan")}
        else:
            tag.attrs = {}

    return title, container.decode_contents(), _text_len(container)


# ---------------------------------------------------------------- rendering

_CSS = """
body{font:18px/1.6 Georgia,serif;margin:0;color:#1d1d1f;background:#fbfaf7}
@media(prefers-color-scheme:dark){body{color:#e6e6e6;background:#151515}a{color:#8ab4f8}}
.bar{position:sticky;top:0;display:flex;gap:12px;align-items:center;padding:8px 14px;
background:inherit;border-bottom:1px solid #8884;font:14px system-ui,sans-serif}
.bar .host{opacity:.65;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar button{font:inherit;padding:4px 10px}
main{max-width:42rem;margin:0 auto;padding:1rem 1.2rem 4rem}
img{max-width:100%;height:auto}pre{overflow:auto}table{border-collapse:collapse;display:block;overflow:auto}
td,th{border:1px solid #8886;padding:4px 8px}
.blocked-link{text-decoration:underline dotted;opacity:.8}
.note{font:14px system-ui,sans-serif;opacity:.8;border-left:3px solid #8886;padding-left:10px}
"""


def _page(title: str, bar: str, body: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_html.escape(title)}</title><style>{_CSS}</style></head><body>"
        f'<div class="bar">{bar}</div><main>{body}</main></body></html>'
    )


def render_article(title, final_url, body_html, text_len, full) -> str:
    host = _html.escape(urlsplit(final_url).hostname or "")
    toggle_href = f"{READ_PATH}?u={quote(final_url, safe='')}" + ("" if full else "&full=1")
    toggle_label = "Reader view" if full else "Full page"
    bar = (
        '<button onclick="history.back()">&larr;</button><a href="/">Home</a>'
        f'<span class="host">{host}</span><a href="{toggle_href}">{toggle_label}</a>'
    )
    note = ""
    if not full and text_len < _MIN_TEXT:
        note = '<p class="note">Very little text was found. Try &ldquo;Full page&rdquo; above.</p>'
    return _page(title, bar, f"{note}<h1>{_html.escape(title)}</h1>{body_html}")


def error_page(err: ReaderError) -> str:
    bar = '<button onclick="history.back()">&larr;</button><a href="/">Home</a>'
    return _page("Cannot open", bar, f"<h1>Cannot open this page</h1><p>{_html.escape(err.message)}</p>")


def read(u: str, full: bool = False, allowed=None) -> str:
    allowed = allowed_hosts() if allowed is None else allowed
    u = (u or "").strip()
    if not u or len(u) > MAX_URL_LEN:
        raise ReaderError(400, "That address is not valid.")
    final_url, data = fetch_page(u, allowed)
    title, body_html, text_len = extract(data, final_url, allowed, full)
    return render_article(title, final_url, body_html, text_len, full)
