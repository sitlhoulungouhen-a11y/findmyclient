"""Collect email addresses a practice publishes on its own website."""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html import unescape
from typing import Callable

UA = "findmyclient/0.1 (+business-contact research; honours robots.txt)"
PATHS = ["/", "/contact", "/contact-us", "/about", "/about-us", "/team", "/our-team", "/providers", "/staff", "/billing"]
MAX_BYTES = 1_000_000

# fetch(url) -> html text, or None when unavailable.
FetchText = Callable[[str], "str | None"]

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
_OBFUSCATED = re.compile(r"([A-Za-z0-9._%+-]+)\s*[\[\(\{]\s*at\s*[\]\)\}]\s*([A-Za-z0-9-]+(?:\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*[A-Za-z0-9-]+|\.[A-Za-z0-9-]+)+)", re.I)
_JUNK_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "woff", "woff2", "ico"}
_JUNK_DOMAINS = {"example.com", "domain.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com", "email.com", "yourdomain.com"}


def http_text(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=12) as r:
            if "html" not in (r.headers.get("Content-Type") or "text/html"):
                return None
            return r.read(MAX_BYTES).decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def extract_emails(html: str) -> set[str]:
    text = unescape(urllib.parse.unquote(html))
    found = set(_EMAIL.findall(text))
    for local, dom in _OBFUSCATED.findall(text):
        found.add(f"{local}@" + re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", dom, flags=re.I))
    out = set()
    for e in found:
        e = e.strip(".").lower()
        dom = e.rsplit("@", 1)[1]
        if dom.rsplit(".", 1)[-1] in _JUNK_TLDS or dom in _JUNK_DOMAINS or re.search(r"@\d+x", e):
            continue
        out.add(e)
    return out


def allowed_by_robots(base: str, fetch: FetchText) -> Callable[[str], bool]:
    rp = urllib.robotparser.RobotFileParser()
    body = fetch(base + "/robots.txt")
    if body is None:
        return lambda url: True
    rp.parse(body.splitlines())
    return lambda url: rp.can_fetch(UA, url)


def scrape_domain(domain: str, fetch: FetchText = http_text, max_pages: int = 6) -> dict[str, str]:
    """Return {email: page_url} for addresses on the practice's site."""
    base = None
    for scheme in ("https", "http"):
        if fetch(f"{scheme}://{domain}/") is not None or fetch(f"{scheme}://www.{domain}/") is not None:
            base = f"{scheme}://{domain}"
            break
    if base is None:
        return {}
    ok = allowed_by_robots(base, fetch)
    found: dict[str, str] = {}
    for path in PATHS[:max_pages]:
        url = base + path
        if not ok(url):
            continue
        html = fetch(url) or (fetch(f"{base.replace('//', '//www.', 1)}{path}") if path == "/" else None)
        for e in extract_emails(html or ""):
            found.setdefault(e, url)
    return found
