"""practice -> domain -> candidate emails -> validated, scored leads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from . import patterns
from .domains import SearchFn, discover_domain, normalize_domain
from .models import Lead, Practice
from .scrape import FetchText, http_text, scrape_domain
from .validate import FREE_PROVIDERS, Validator

# status -> (scraped score, pattern score)
_SCORES = {"valid": (95, 85), "risky": (60, 35), "unknown": (55, 30), "invalid": (0, 0)}
MAX_PATTERN_GUESSES = 6


def process_practice(
    p: Practice,
    validator: Validator,
    *,
    search: SearchFn | None = None,
    fetch: FetchText = http_text,
    keep_invalid: bool = False,
) -> tuple[Practice, list[Lead]]:
    notes: list[str] = []
    p.domain = normalize_domain(p.domain)
    if not p.domain and search:
        try:
            p.domain = discover_domain(p, search)
        except Exception as e:  # search API quota / network: don't kill the batch
            notes.append(f"domain search failed: {e}")
    if not p.domain:
        return p, []

    leads: list[Lead] = []
    seen: set[str] = set()

    def add(email: str, source: str) -> Lead | None:
        if email in seen:
            return None
        seen.add(email)
        v = validator.validate(email)
        base = _SCORES[v.status][0 if source == "scraped" else 1]
        score = max(0, base - (15 if v.role else 0))
        lead = Lead(p, email, source, v, score, list(notes))
        if v.status != "invalid" or keep_invalid:
            leads.append(lead)
        return lead

    for email in sorted(scrape_domain(p.domain, fetch)):
        add(email, "scraped")

    # Guess the decision-maker's address only when they aren't already on the site.
    contact_found = any(
        p.contact_last and p.contact_last.lower().split()[0] in l.email.split("@")[0] for l in leads
    )
    if p.contact_first and p.contact_last and not contact_found and p.domain not in FREE_PROVIDERS:
        guesses = patterns.candidates(p.contact_first, p.contact_last, p.domain, MAX_PATTERN_GUESSES)
        if validator.use_smtp:
            for g in guesses:
                lead = add(g, "pattern")
                if lead and lead.validation.status in ("valid", "risky"):
                    break  # first accepted pattern wins; on catch-all domains the rest prove nothing
                if lead and lead.validation.smtp == "unknown":
                    break  # server isn't answering; more probes won't help
        else:
            add(guesses[0], "pattern") if guesses else None  # top guess only, flagged unverified
    leads.sort(key=lambda l: -l.score)
    return p, leads


def run(
    practices: Iterable[Practice],
    validator: Validator,
    *,
    search: SearchFn | None = None,
    fetch: FetchText = http_text,
    workers: int = 8,
    keep_invalid: bool = False,
    on_done: Callable[[Practice, list[Lead]], None] | None = None,
) -> list[tuple[Practice, list[Lead]]]:
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(process_practice, p, validator, search=search, fetch=fetch, keep_invalid=keep_invalid)
                for p in practices]
        for f in futs:
            r = f.result()
            results.append(r)
            if on_done:
                on_done(*r)
    return results
