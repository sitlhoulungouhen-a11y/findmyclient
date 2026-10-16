"""Client for the CMS NPPES NPI Registry API (public, free, no key).

Organisation records (NPI-2) carry an "authorized official" with name and title,
which is usually the owner / administrator / CEO: the person to contact about
outsourcing billing.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterator

from .models import Practice

API_URL = "https://npiregistry.cms.hhs.gov/api/"
PAGE_SIZE = 200  # API maximum
MAX_SKIP = 1000  # API maximum; results beyond this need narrower filters

Fetch = Callable[[str], dict]


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "findmyclient/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _title(s: str) -> str:
    return " ".join(w.capitalize() for w in (s or "").split())


def parse_result(r: dict) -> Practice | None:
    basic = r.get("basic", {})
    org = r.get("enumeration_type") == "NPI-2"
    loc = next((a for a in r.get("addresses", []) if a.get("address_purpose") == "LOCATION"), None)
    if loc is None:
        loc = (r.get("addresses") or [{}])[0]
    tax = next((t for t in r.get("taxonomies", []) if t.get("primary")), None)
    tax = tax or (r.get("taxonomies") or [{}])[0]

    if org:
        name = basic.get("organization_name", "")
        first = basic.get("authorized_official_first_name", "")
        last = basic.get("authorized_official_last_name", "")
        title = basic.get("authorized_official_title_or_position", "")
    else:
        name = " ".join(p for p in (basic.get("first_name"), basic.get("last_name")) if p)
        if basic.get("credential"):
            name += f", {basic['credential']}"
        first, last, title = basic.get("first_name", ""), basic.get("last_name", ""), ""
    if not name:
        return None
    return Practice(
        npi=str(r.get("number", "")),
        kind="org" if org else "individual",
        name=_title(name) if org else name,
        specialty=tax.get("desc", ""),
        address=_title(loc.get("address_1", "")),
        city=_title(loc.get("city", "")),
        state=loc.get("state", ""),
        zip=(loc.get("postal_code") or "")[:5],
        phone=loc.get("telephone_number", ""),
        contact_first=_title(first),
        contact_last=_title(last),
        contact_title=_title(title),
    )


def search(
    *,
    state: str = "",
    city: str = "",
    postal_code: str = "",
    taxonomy: str = "",
    name: str = "",
    kind: str = "org",
    limit: int = 200,
    fetch: Fetch = _http_json,
) -> Iterator[Practice]:
    """Yield practices matching the filters, paging through the registry."""
    if not any((state, city, postal_code, taxonomy, name)):
        raise ValueError("NPI registry needs at least one filter (state, city, postal code, specialty or name)")
    params = {"version": "2.1", "enumeration_type": "NPI-2" if kind == "org" else "NPI-1", "address_purpose": "LOCATION"}
    for key, val in (("state", state), ("city", city), ("postal_code", postal_code),
                     ("taxonomy_description", taxonomy), ("organization_name" if kind == "org" else "last_name", name)):
        if val:
            params[key] = val

    yielded, skip = 0, 0
    while yielded < limit and skip <= MAX_SKIP:
        page = min(PAGE_SIZE, limit - yielded)
        data = fetch(API_URL + "?" + urllib.parse.urlencode({**params, "limit": page, "skip": skip}))
        if data.get("Errors"):
            raise RuntimeError("NPI registry: " + "; ".join(e.get("description", "?") for e in data["Errors"]))
        results = data.get("results") or []
        for r in results:
            p = parse_result(r)
            if p:
                yield p
                yielded += 1
                if yielded >= limit:
                    return
        if len(results) < page:
            return
        skip += page
