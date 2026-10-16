"""Generate likely email local-parts from a person's name."""
from __future__ import annotations

import re
import unicodedata

# Most common corporate / small-practice conventions first.
PATTERNS = [
    "{first}.{last}",
    "{first}",
    "{f}{last}",
    "{first}{last}",
    "{first}{l}",
    "{last}",
    "{f}.{last}",
    "{first}_{last}",
    "{last}.{first}",
    "{last}{f}",
]

_SUFFIXES = {"md", "do", "dds", "dmd", "np", "pa", "rn", "phd", "jr", "sr", "ii", "iii", "dpm", "od", "dc"}


def _clean(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def candidates(first: str, last: str, domain: str, limit: int | None = None) -> list[str]:
    first_parts = [p for p in re.split(r"\s+", first.strip()) if p]
    last_parts = [p for p in re.split(r"[\s,]+", last.strip()) if p and _clean(p) not in _SUFFIXES]
    f, l = _clean(first_parts[0]) if first_parts else "", _clean("".join(last_parts))
    if not f or not l or not domain:
        return []
    out: list[str] = []
    for pat in PATTERNS:
        local = pat.format(first=f, last=l, f=f[0], l=l[0])
        addr = f"{local}@{domain.lower()}"
        if addr not in out:
            out.append(addr)
    return out[:limit] if limit else out


def infer(local: str, first: str, last: str) -> str | None:
    """Which template produced `local` for this person? (inverse of candidates())"""
    f, l = _names(first, last)
    if not f or not l:
        return None
    local = local.lower()
    for pat in PATTERNS:
        if pat.format(first=f, last=l, f=f[0], l=l[0]) == local:
            return pat
    return None


def apply(template: str, first: str, last: str, domain: str) -> str | None:
    """Build an address for this person from a learned template."""
    f, l = _names(first, last)
    if not f or not l or not domain:
        return None
    return f"{template.format(first=f, last=l, f=f[0], l=l[0])}@{domain.lower()}"


def _names(first: str, last: str) -> tuple[str, str]:
    fp = first.split()
    lp = [p for p in re.split(r"[\s,]+", last.strip()) if p and _clean(p) not in _SUFFIXES]
    return (_clean(fp[0]) if fp else ""), _clean("".join(lp))
