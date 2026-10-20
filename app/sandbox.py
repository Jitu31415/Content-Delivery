"""
Closed-world sandbox for a web-view wrapper.

Threat model: a person using the app inside a WebView has no address bar, so
the only ways out are things the *page* offers or does:
  1. <a href> / <area href> to another site, or to a non-http scheme
     (mailto:, tel:, intent:, market:, javascript:, data: ...)
  2. <form action> posting elsewhere
  3. window.open() / target=_blank
  4. <iframe> content (e.g. the YouTube player: logo, title, "Watch on
     YouTube", share, privacy-policy links) opening popups or navigating away
  5. <meta http-equiv=refresh>, <base href>, <object>/<embed>
  6. script assignments to location.* (cannot be intercepted from the page)

This middleware rewrites every text/html response so that 1, 2, 4 and 5 cannot
reach the network, and sends a Content-Security-Policy whose `sandbox`
directive is inherited by every nested frame (including iframes that
JavaScript creates later, which HTML rewriting cannot see).

Links to hosts allowed by app.reader (the configured sources) are rewritten
to /read?u=..., so articles open in the stripped reader view served from this
origin instead of navigating the WebView to a third-party page.

What it does NOT do (see the notes delivered with this file):
  * It cannot stop a script from assigning location.href. There is no working
    `navigate-to` CSP. The templates' own scripts do not need to, but this is
    not enforced here.
  * It cannot stop the user leaving the app (Home button) or the wrapper's own
    native menus/footers. Those are controlled in the wrapper / device.

Configuration (env, read when the middleware is created):
  SANDBOX_MODE                 "on" (default) | "off"
  SANDBOX_ALLOWED_LINK_HOSTS   comma list of extra hosts links may point to
                               (default: none; same-host links always allowed)
  SANDBOX_ALLOWED_FRAME_HOSTS  comma list of hosts iframes may load
                               (default: www.youtube-nocookie.com,www.youtube.com)
"""

import json
import os
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from starlette.datastructures import MutableHeaders

from app import reader

ENV_MODE = "SANDBOX_MODE"
ENV_LINK_HOSTS = "SANDBOX_ALLOWED_LINK_HOSTS"
ENV_FRAME_HOSTS = "SANDBOX_ALLOWED_FRAME_HOSTS"
DEFAULT_FRAME_HOSTS = ("www.youtube-nocookie.com", "www.youtube.com")

# Browsers strip ASCII whitespace/control characters inside a URL and treat
# backslashes as slashes in http(s) URLs, so the check must do the same or
# "java\nscript:" and "/\evil.com" slip through.
_CTRL = re.compile(r"[\x00-\x20\x7f]")
_JS_NOOP = {"javascript:void(0)", "javascript:void(0);", "javascript:;", "javascript:"}

_FRAME_SANDBOX = "allow-scripts allow-same-origin allow-presentation"


def _csv(value, default=()):
    if value is None:
        return tuple(default)
    return tuple(x.strip().lower() for x in value.split(",") if x.strip())


def url_allowed(url, base_url, host, extra_hosts=frozenset()) -> bool:
    """True if navigating to `url` (resolved against base_url) stays on this
    host (or an explicitly allowed one) over http(s). Fragment-only links and
    the conventional javascript no-ops are allowed."""
    raw = (url or "").strip()
    if not raw or raw.startswith("#"):
        return True
    cleaned = _CTRL.sub("", raw).replace("\\", "/")
    if cleaned.lower() in _JS_NOOP:
        return True
    try:
        parts = urlsplit(urljoin(base_url, cleaned))
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    netloc = parts.netloc.lower()
    return netloc == host or netloc in extra_hosts


def _frame_allowed(src, base_url, host, frame_hosts) -> bool:
    cleaned = _CTRL.sub("", (src or "").strip()).replace("\\", "/")
    if not cleaned:
        return True  # empty src: JS fills it in later; the CSP sandbox/frame-src still apply
    try:
        parts = urlsplit(urljoin(base_url, cleaned))
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    netloc = parts.netloc.lower()
    return netloc == host or netloc in frame_hosts


def _neutralize_anchor(tag) -> None:
    """Keep the visible content, remove everything that could navigate."""
    for attr in ("href", "target", "download", "ping", "rel"):
        tag.attrs.pop(attr, None)
    if tag.name == "a":
        tag.name = "span"
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    tag["class"] = classes + ["blocked-link"]
    tag["aria-disabled"] = "true"
    tag["title"] = "External link blocked"


def rewrite_html(html: str, *, host: str, base_url: str,
                 link_hosts=frozenset(), frame_hosts=DEFAULT_FRAME_HOSTS,
                 reader_hosts=frozenset()) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["base", "object", "embed", "applet", "frame", "frameset"]):
        tag.decompose()
    for tag in soup.find_all("meta"):
        if (tag.get("http-equiv") or "").strip().lower() == "refresh":
            tag.decompose()

    for tag in soup.find_all(["a", "area"]):
        href = tag.get("href")
        if href is not None and not url_allowed(href, base_url, host, link_hosts):
            # A link to an allowed source opens in the reader view (served from
            # this origin); anything else becomes inert text.
            via = reader.reader_href(href, base_url, reader_hosts) if reader_hosts else None
            if via:
                tag["href"] = via
                tag.attrs.pop("target", None)
            else:
                _neutralize_anchor(tag)
        else:
            # Popups are blocked by the sandbox, so a same-site target=_blank
            # link would otherwise do nothing at all.
            tag.attrs.pop("target", None)

    for form in soup.find_all("form"):
        action = form.get("action")
        if action is not None and not url_allowed(action, base_url, host, link_hosts):
            del form.attrs["action"]
    for el in soup.find_all(attrs={"formaction": True}):
        if not url_allowed(el.get("formaction"), base_url, host, link_hosts):
            del el.attrs["formaction"]

    for frame in soup.find_all("iframe"):
        if frame.get("srcdoc") is not None or not _frame_allowed(
                frame.get("src"), base_url, host, frame_hosts):
            frame.decompose()
            continue
        frame["sandbox"] = _FRAME_SANDBOX
        frame["referrerpolicy"] = "strict-origin-when-cross-origin"

    return str(soup)


def build_csp(frame_hosts) -> str:
    frames = " ".join(f"https://{h}" for h in frame_hosts) or "'none'"
    return "; ".join([
        # Inherited by every nested frame, including ones created by script:
        # no popups, no top-level navigation from inside a frame.
        "sandbox allow-scripts allow-same-origin allow-forms allow-modals allow-presentation",
        f"frame-src {frames}",
        "form-action 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "connect-src 'self'",
    ])


def build_guard(link_hosts) -> str:
    """Last line of defence for links/forms added by script after load.
    Not a security boundary on its own (script can bypass it)."""
    allowed = json.dumps(sorted(link_hosts))
    return (
        "<script>(function(){"
        "var A=" + allowed + ";"
        "function ok(u){try{u=String(u||'').replace(/[\\u0000-\\u0020\\u007f]/g,'').replace(/\\\\/g,'/');"
        "if(!u||u.charAt(0)==='#')return true;"
        "if(/^javascript:(void\\(0\\);?|;)?$/i.test(u))return true;"
        "var x=new URL(u,location.href);"
        "return (x.protocol==='http:'||x.protocol==='https:')&&(x.origin===location.origin||A.indexOf(x.host)>=0);"
        "}catch(e){return false;}}"
        "function onClick(e){var t=e.target;"
        "var a=t&&t.closest?t.closest('a[href],area[href]'):null;"
        "if(a&&!ok(a.getAttribute('href')))e.preventDefault();}"
        "document.addEventListener('click',onClick,true);"
        "document.addEventListener('auxclick',onClick,true);"
        "document.addEventListener('submit',function(e){var f=e.target,a=f&&f.getAttribute&&f.getAttribute('action');"
        "if(a&&!ok(a))e.preventDefault();},true);"
        "window.open=function(){return null;};"
        "})();</script>"
    )


class SandboxMiddleware:
    """Pure ASGI middleware (not BaseHTTPMiddleware, so BackgroundTasks used by
    the cron route are unaffected). Non-HTML responses pass through untouched
    apart from two harmless headers."""

    def __init__(self, app, enabled=None, link_hosts=None, frame_hosts=None, reader_hosts=None):
        self.app = app
        # Callable returning the allowed reader hosts; looked up per request.
        self.reader_hosts = reader_hosts or (lambda: reader.allowed_hosts())
        self.enabled = (os.environ.get(ENV_MODE, "on").strip().lower() != "off"
                        if enabled is None else enabled)
        self.link_hosts = frozenset(
            _csv(os.environ.get(ENV_LINK_HOSTS)) if link_hosts is None
            else (h.lower() for h in link_hosts))
        self.frame_hosts = (
            _csv(os.environ.get(ENV_FRAME_HOSTS), DEFAULT_FRAME_HOSTS) if frame_hosts is None
            else tuple(h.lower() for h in frame_hosts))
        self.csp = build_csp(self.frame_hosts)
        self.guard = build_guard(self.link_hosts)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        host = ""
        for k, v in scope.get("headers", []):
            if k == b"host":
                host = v.decode("latin-1").lower()
                break
        base_url = f"{scope.get('scheme', 'http')}://{host}{scope.get('path', '/')}"
        state = {"html": False, "start": None, "chunks": []}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                if headers.get("content-type", "").lower().startswith("text/html"):
                    headers["Content-Security-Policy"] = self.csp
                    state["html"] = True
                    state["start"] = message
                    return  # hold the headers until the body has been rewritten
                await send(message)
                return

            if message["type"] == "http.response.body" and state["html"]:
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body", False):
                    return
                body = b"".join(state["chunks"])
                start = state["start"]
                if body:
                    try:
                        text = rewrite_html(
                            body.decode("utf-8", "replace"), host=host, base_url=base_url,
                            link_hosts=self.link_hosts, frame_hosts=self.frame_hosts,
                            reader_hosts=self.reader_hosts())
                        idx = text.lower().rfind("</body>")
                        text = (text[:idx] + self.guard + text[idx:]) if idx != -1 else text + self.guard
                        body = text.encode("utf-8")
                    except Exception:
                        # Fail closed: never serve a page that could not be sanitised.
                        start["status"] = 500
                        hdrs = MutableHeaders(scope=start)
                        hdrs["content-type"] = "text/plain; charset=utf-8"
                        body = b"Sandbox error: page could not be sanitised."
                    MutableHeaders(scope=start)["content-length"] = str(len(body))
                await send(start)
                await send({"type": "http.response.body", "body": body})
                return

            await send(message)

        await self.app(scope, receive, send_wrapper)
