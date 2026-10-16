from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from urllib.parse import urlparse


def normalize_domain(value: str) -> str:
    """'https://www.Acme.com/about' -> 'acme.com'. Accepts emails too ('x@acme.com' -> 'acme.com')."""
    value = value.strip().lower()
    if not value:
        return ""
    host = urlparse(value if "//" in value else "//" + value).netloc or value
    host = re.split(r"[/?#]", host.split("@")[-1])[0].split(":")[0]
    return host[4:] if host.startswith("www.") else host


@dataclass
class Practice:
    """A US practice (NPI-2) or individual provider (NPI-1)."""

    npi: str
    kind: str = "org"  # "org" | "individual"
    name: str = ""
    specialty: str = ""
    taxonomy: str = ""  # NUCC taxonomy code
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    # Decision-maker. For orgs this is the NPPES "authorized official".
    contact_first: str = ""
    contact_last: str = ""
    contact_title: str = ""
    domain: str = ""
    domain_source: str = ""  # imported | probed | manual
    domain_confidence: int = 0  # 0-100

    @classmethod
    def csv_columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Practice":
        known = {f.name for f in fields(cls)}
        data = {k: (v or "").strip() for k, v in row.items() if k in known}
        data["domain_confidence"] = int(data.get("domain_confidence") or 0)
        data["domain"] = normalize_domain(data.get("domain", ""))
        if data.get("domain") and not data.get("domain_source"):
            data["domain_source"], data["domain_confidence"] = "imported", data["domain_confidence"] or 100
        return cls(**data)


@dataclass
class Validation:
    email: str
    status: str = "unknown"  # valid | risky | invalid | unknown
    reason: str = ""
    syntax_ok: bool = True
    has_mx: bool | None = None
    disposable: bool = False
    role: bool = False
    free_provider: bool = False
    smtp: str = "skipped"  # accepted | rejected | unknown | skipped
    catch_all: bool | None = None


@dataclass
class Lead:
    practice: Practice
    email: str
    source: str  # scraped | pattern | learned
    validation: Validation
    score: int = 0
    notes: list[str] = field(default_factory=list)
