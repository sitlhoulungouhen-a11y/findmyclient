"""practice (+ domain) -> candidate emails -> validated, scored leads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Iterable

from . import patterns
from .models import Lead, Practice, Validation
from .scrape import FetchText, http_text, scrape_domain
from .validate import FREE_PROVIDERS, Validator

# status -> (scored from the practice's own site, scored from a name pattern)
_SCORES = {"valid": (95, 85), "risky": (60, 35), "unknown": (55, 30), "invalid": (0, 0)}
MAX_PATTERN_GUESSES = 6
LEARNED_BONUS = 10


@dataclass
class Hooks:
    """Where the pipeline touches shared state. Defaults are no-ops so it stays testable without a DB."""
    suppressed: Callable[[str], bool] = lambda e: False
    cached: Callable[[str], Validation | None] = lambda e: None
    learned_template: Callable[[str], str | None] = lambda d: None


def process_practice(
    p: Practice,
    validator: Validator,
    *,
    hooks: Hooks = Hooks(),
    crawl: bool = False,
    fetch: FetchText = http_text,
    keep_invalid: bool = False,
) -> tuple[Practice, list[Lead]]:
    if not p.domain:
        return p, []
    leads: list[Lead] = []
    seen: set[str] = set()

    def add(email: str, source: str) -> Lead | None:
        email = email.lower()
        if email in seen or hooks.suppressed(email):
            return None
        seen.add(email)
        v = hooks.cached(email) or validator.validate(email)
        base = _SCORES[v.status][0 if source == "scraped" else 1]
        if source == "learned" and v.status != "invalid":
            base += LEARNED_BONUS
        # An unconfirmed domain match shouldn't produce confident leads.
        base = base * min(100, max(p.domain_confidence, 1)) // 100 if p.domain_confidence < 80 else base
        lead = Lead(p, email, source, v, max(0, base - (15 if v.role else 0)))
        if v.status != "invalid" or keep_invalid:
            leads.append(lead)
        return lead

    if crawl:  # opt-in: fetches up to six pages of the practice's own site
        for email in sorted(scrape_domain(p.domain, fetch)):
            add(email, "scraped")

    have_contact = p.contact_first and p.contact_last and p.domain not in FREE_PROVIDERS
    contact_found = have_contact and any(p.contact_last.lower().split()[0] in l.email.split("@")[0] for l in leads)
    if have_contact and not contact_found:
        template = hooks.learned_template(p.domain)
        learned = patterns.apply(template, p.contact_first, p.contact_last, p.domain) if template else None
        if learned:
            add(learned, "learned")  # another contact at this domain replied with this convention
        else:
            guesses = patterns.candidates(p.contact_first, p.contact_last, p.domain, MAX_PATTERN_GUESSES)
            if validator.use_smtp:
                for g in guesses:
                    lead = add(g, "pattern")
                    if lead and (lead.validation.status in ("valid", "risky") or lead.validation.smtp == "unknown"):
                        break  # first accepted wins; on catch-all domains the rest prove nothing; dead servers answer nothing
            elif guesses:
                add(guesses[0], "pattern")  # top guess only, flagged unverified
    leads.sort(key=lambda l: -l.score)
    return p, leads


def run(
    practices: Iterable[Practice],
    validator: Validator,
    *,
    hooks: Hooks = Hooks(),
    crawl: bool = False,
    fetch: FetchText = http_text,
    workers: int = 8,
    keep_invalid: bool = False,
    on_done: Callable[[Practice, list[Lead]], None] | None = None,
) -> None:
    """Results are handed to on_done as they finish (so a long batch can persist incrementally)."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(process_practice, p, validator, hooks=hooks, crawl=crawl, fetch=fetch, keep_invalid=keep_invalid)
                for p in practices]
        for f in futs:
            r = f.result()
            if on_done:
                on_done(*r)
