"""In-house domain discovery: no search engine, no directories.

1. Derive likely domains from the practice name (acmeprimarycare.com, acme-primary-care.net, ...).
2. Keep those that exist in DNS.
3. Confirm identity on the candidate's own homepage: the phone number, ZIP, city and name words
   from the registry record must appear there. A domain that merely resolves is never accepted.
"""
from __future__ import annotations

import re
from typing import Callable

from .models import Practice
from .scrape import FetchText, allowed_by_robots, http_text
from .validate import Resolver

TLDS = ["com", "net", "org", "health", "md", "care", "us", "clinic"]
ACCEPT_AT = 50

_LEGAL = {"llc", "inc", "pllc", "pc", "pa", "ltd", "corp", "corporation", "co", "llp", "lp", "plc", "incorporated", "company"}
_STOP = {"the", "of", "and", "a", "an", "for", "in", "at"}
# Words that carry little identity; used to build shorter variants and to ignore in identity matching.
_GENERIC = {"medical", "medicine", "health", "healthcare", "clinic", "center", "centre", "associates", "group", "practice",
            "services", "service", "care", "physicians", "physician", "specialists", "partners", "professional", "family",
            "primary", "internal", "pediatrics", "dental", "wellness"}

_STATES = {"AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
           "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
           "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
           "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
           "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
           "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
           "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
           "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee", "TX": "texas",
           "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
           "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia"}


def name_tokens(name: str) -> list[str]:
    name = name.lower().replace("&", " and ")
    name = re.sub(r"\b([a-z])\.(?=[a-z]\.)", r"\1", name)  # p.c. -> pc
    words = re.findall(r"[a-z0-9]+", re.sub(r"[.'’]", "", name))
    return [w for w in words if w not in _LEGAL]


def candidate_domains(p: Practice, tlds: list[str] = TLDS, max_candidates: int = 48) -> list[str]:
    toks = [t for t in name_tokens(p.name) if t not in _STOP]
    if p.kind == "individual":
        last = re.sub(r"[^a-z]", "", p.contact_last.lower())
        first = re.sub(r"[^a-z]", "", p.contact_first.lower())
        stems = [f"dr{last}", f"{last}md", f"{first}{last}", f"{last}{first[:1]}", f"{last}family"] if last else []
    else:
        core = [t for t in toks if t not in _GENERIC] or toks
        # drop trailing generic words progressively: "acme primary care associates" -> "acme primary care", "acme"
        stems = []
        for end in range(len(toks), 0, -1):
            stems += ["".join(toks[:end]), "-".join(toks[:end])]
        stems += ["".join(core), "-".join(core)]
        if len(core) > 1:
            stems.append("".join(w[0] for w in toks) if len(toks) <= 5 else "")
        if p.city:
            city = re.sub(r"[^a-z]", "", p.city.lower())
            stems += [f"{''.join(core)}{city}", f"{city}{''.join(core)}"]
    stems = list(dict.fromkeys(s for s in stems if len(s.replace("-", "")) >= 4))  # 2-3 letter stems are mostly someone else's
    # .com for every stem first, so the cap trims exotic TLDs rather than the likeliest names
    return [f"{s}.{tld}" for tld in tlds for s in stems][:max_candidates]


def _page_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).lower()


def identity_score(p: Practice, html: str) -> tuple[int, list[str]]:
    """How strongly does this page say it belongs to this registry record?"""
    text = _page_text(html)
    digits = re.sub(r"\D", "", text)
    score, why = 0, []
    if len(p.phone) == 10 and p.phone in digits:
        score += 60; why.append("phone")
    if p.zip and re.search(rf"\b{re.escape(p.zip)}\b", text):
        score += 15; why.append("zip")
    if p.city and re.search(rf"\b{re.escape(p.city.lower())}\b", text):
        score += 15; why.append("city")
    st = _STATES.get(p.state.upper(), "")
    if p.state and (re.search(rf"\b{p.state.lower()}\b", text) or (st and st in text)):
        score += 5; why.append("state")
    toks = [t for t in name_tokens(p.name) if t not in _STOP and t not in _GENERIC] or name_tokens(p.name)
    if toks:
        hit = sum(1 for t in toks if re.search(rf"\b{re.escape(t)}", text)) / len(toks)
        if hit >= 0.99:
            score += 25; why.append("name")
        elif hit >= 0.5:
            score += 12; why.append("name~")
    return min(score, 100), why


def probe(
    p: Practice,
    resolver: Resolver,
    fetch: FetchText = http_text,
    tlds: list[str] = TLDS,
) -> tuple[str, int, list[str]]:
    """Return (domain, confidence, evidence) for the best-matching candidate, or ('', 0, [])."""
    best: tuple[str, int, list[str]] = ("", 0, [])
    for dom in candidate_domains(p, tlds):
        if resolver.mx_hosts(dom) is None:  # not in DNS at all
            continue
        for scheme in ("https", "http"):
            base = f"{scheme}://{dom}"
            html = fetch(base + "/") if allowed_by_robots(base, fetch)(base + "/") else None
            if html is None:
                continue
            score, why = identity_score(p, html)
            if score < ACCEPT_AT:  # the phone or ZIP often lives on the contact page, not the homepage
                contact = fetch(base + "/contact")
                if contact:
                    score, why = identity_score(p, html + " " + contact)
            if score > best[1]:
                best = (dom, score, why)
            break
        if best[1] >= 80:
            break
    return best if best[1] >= ACCEPT_AT else ("", 0, [])
