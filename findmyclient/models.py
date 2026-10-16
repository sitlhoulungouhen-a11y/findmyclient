from __future__ import annotations

from dataclasses import dataclass, field, fields


@dataclass
class Practice:
    """A US practice (NPI-2) or individual provider (NPI-1) from the NPI Registry."""

    npi: str
    kind: str  # "org" | "individual"
    name: str
    specialty: str = ""
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

    @classmethod
    def csv_columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "Practice":
        known = {f.name for f in fields(cls)}
        return cls(**{k: (v or "").strip() for k, v in row.items() if k in known})


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
    source: str  # scraped | pattern | role
    validation: Validation
    score: int = 0
    notes: list[str] = field(default_factory=list)
