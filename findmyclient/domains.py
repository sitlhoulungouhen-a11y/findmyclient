"""Find a practice's own website domain."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Callable
from urllib.parse import urlparse

from .models import Practice

# Aggregators / directories / social: never a practice's own site.
DIRECTORY_DOMAINS = {
    "healthgrades.com", "npidb.org", "npino.com", "doximity.com", "zocdoc.com", "yelp.com", "facebook.com",
    "linkedin.com", "instagram.com", "twitter.com", "x.com", "yellowpages.com", "mapquest.com", "webmd.com",
    "vitals.com", "usnews.com", "bbb.org", "manta.com", "buzzfile.com", "zoominfo.com", "dnb.com", "opencorporates.com",
    "wikipedia.org", "youtube.com", "indeed.com", "glassdoor.com", "ratemds.com", "sharecare.com", "chamberofcommerce.com",
    "findatopdoc.com", "wellness.com", "birdeye.com", "mapquest.com", "cms.gov", "medicare.gov", "hhs.gov", "nih.gov",
    "google.com", "bing.com", "apple.com", "bizapedia.com", "cylex.us.com", "usa.com", "hipaaspace.com",
}

# search(query) -> list of result URLs
SearchFn = Callable[[str], "list[str]"]


def normalize_domain(value: str) -> str:
    value = value.strip().lower()
    if not value:
        return ""
    host = urlparse(value if "//" in value else "//" + value).netloc or value
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def is_directory(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in DIRECTORY_DOMAINS)


def brave_search(api_key: str) -> SearchFn:
    def run(query: str) -> list[str]:
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": 10, "country": "us"})
        req = urllib.request.Request(url, headers={"X-Subscription-Token": api_key, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return [x["url"] for x in json.load(r).get("web", {}).get("results", [])]
    return run


def default_search() -> SearchFn | None:
    key = os.environ.get("BRAVE_API_KEY")
    return brave_search(key) if key else None


def discover_domain(p: Practice, search: SearchFn) -> str:
    """Best guess at the practice's own site: first non-directory hit for name + location."""
    for url in search(f'"{p.name}" {p.city} {p.state}'):
        host = normalize_domain(url)
        if host and not is_directory(host):
            return host
    return ""
