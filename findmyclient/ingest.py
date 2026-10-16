"""Load practices from local files: the NPPES bulk dissemination CSV, or any CSV you hold (CRM export, purchased list)."""
from __future__ import annotations

import csv
import hashlib
import re
from typing import Iterable, Iterator

from .models import Practice
from .taxonomy import label

csv.field_size_limit(10_000_000)

# Header names in the NPPES "npidata_pfile" CSV.
_NPPES = {
    "npi": "NPI",
    "etype": "Entity Type Code",
    "org": "Provider Organization Name (Legal Business Name)",
    "last": "Provider Last Name (Legal Name)",
    "first": "Provider First Name",
    "cred": "Provider Credential Text",
    "addr": "Provider First Line Business Practice Location Address",
    "city": "Provider Business Practice Location Address City Name",
    "state": "Provider Business Practice Location Address State Name",
    "zip": "Provider Business Practice Location Address Postal Code",
    "phone": "Provider Business Practice Location Address Telephone Number",
    "ao_last": "Authorized Official Last Name",
    "ao_first": "Authorized Official First Name",
    "ao_title": "Authorized Official Title or Position",
    "deact": "NPI Deactivation Date",
    "react": "NPI Reactivation Date",
}
_REQUIRED = ("npi", "etype", "org", "last", "first", "state", "zip")


def _title(s: str) -> str:
    return " ".join(w.capitalize() for w in (s or "").split())


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def _taxonomy(row: dict[str, str]) -> str:
    for i in range(1, 16):
        if row.get(f"Healthcare Provider Primary Taxonomy Switch_{i}") == "Y":
            return row.get(f"Healthcare Provider Taxonomy Code_{i}", "")
    return row.get("Healthcare Provider Taxonomy Code_1", "")


def read_nppes(
    path: str,
    *,
    states: Iterable[str] = (),
    cities: Iterable[str] = (),
    entity: str = "org",
    taxonomy_prefixes: Iterable[str] = (),
    limit: int | None = None,
) -> Iterator[Practice]:
    """Stream the multi-GB NPPES file, yielding only records that match the filters."""
    states = {s.upper() for s in states}
    cities = {c.upper() for c in cities}
    prefixes = tuple(p.upper() for p in taxonomy_prefixes)
    wanted = {"org": {"2"}, "individual": {"1"}, "both": {"1", "2"}}[entity]
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        missing = [_NPPES[k] for k in _REQUIRED if _NPPES[k] not in (rd.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} doesn't look like an NPPES npidata file; missing columns: {missing}")
        for row in rd:
            g = {k: (row.get(col) or "").strip() for k, col in _NPPES.items()}
            if g["etype"] not in wanted:
                continue
            if g["deact"] and not g["react"]:
                continue
            if states and g["state"].upper() not in states:
                continue
            if cities and g["city"].upper() not in cities:
                continue
            tax = _taxonomy(row)
            if prefixes and not tax.upper().startswith(prefixes):
                continue
            org = g["etype"] == "2"
            if org:
                name, cf, cl, ct = _title(g["org"]), _title(g["ao_first"]), _title(g["ao_last"]), _title(g["ao_title"])
            else:
                name = " ".join(x for x in (_title(g["first"]), _title(g["last"])) if x)
                if g["cred"]:
                    name += f", {g['cred']}"
                cf, cl, ct = _title(g["first"]), _title(g["last"]), ""
            if not name:
                continue
            yield Practice(
                npi=g["npi"], kind="org" if org else "individual", name=name, specialty=label(tax), taxonomy=tax,
                address=_title(g["addr"]), city=_title(g["city"]), state=g["state"].upper(), zip=g["zip"][:5],
                phone=_digits(g["phone"]), contact_first=cf, contact_last=cl, contact_title=ct,
            )
            n += 1
            if limit and n >= limit:
                return


def read_csv(path: str) -> Iterator[Practice]:
    """Generic import. Needs `name` or a known `npi`; everything else optional, unknown columns ignored.

    A sparse file such as `npi,domain` just attaches domains to practices already loaded.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): v for k, v in row.items() if k}
            row.setdefault("kind", "org")
            if not row.get("npi") and not row.get("name"):
                continue
            if not row.get("npi"):  # stable id so re-importing the same list updates rather than duplicates
                key = "|".join(row.get(k, "").lower() for k in ("name", "zip", "phone", "address"))
                row["npi"] = "local-" + hashlib.sha1(key.encode()).hexdigest()[:10]
            p = Practice.from_row(row)
            p.phone = _digits(p.phone)
            yield p
