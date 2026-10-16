from __future__ import annotations

import argparse
import csv
import os
import sys

from . import npi
from .domains import default_search
from .models import Lead, Practice
from .pipeline import run
from .validate import DnsResolver, Validator, port25_open

LEAD_COLUMNS = [
    "practice", "npi", "specialty", "city", "state", "phone", "domain", "contact", "contact_title",
    "email", "source", "status", "score", "reason", "role", "catch_all", "smtp",
]


def _eprint(*a):
    print(*a, file=sys.stderr)


def _validator(args) -> Validator:
    helo = args.helo or os.environ.get("FMC_HELO", "")
    mail_from = args.mail_from or os.environ.get("FMC_MAIL_FROM", "")
    if args.smtp:
        if not helo or not mail_from:
            sys.exit("--smtp needs a real sending identity: --helo <your server's hostname> --mail-from <a mailbox you own> "
                     "(or FMC_HELO / FMC_MAIL_FROM). Anonymous probes get blocked.")
        if not port25_open():
            _eprint("WARNING: outbound port 25 looks blocked on this network (common on cloud hosts and home ISPs).\n"
                    "         SMTP checks will come back 'unknown'. Run from a VPS that allows port 25, or omit --smtp.")
    return Validator(resolver=DnsResolver(), use_smtp=args.smtp, helo=helo or "localhost",
                     mail_from=mail_from or "verify@localhost", delay=args.delay)


def cmd_search(args) -> None:
    rows = list(npi.search(state=args.state, city=args.city, postal_code=args.zip, taxonomy=args.specialty,
                           name=args.name, kind=args.type, limit=args.limit))
    with open(args.output, "w", newline="") as f:
        w = csv.DictWriter(f, Practice.csv_columns())
        w.writeheader()
        for p in rows:
            w.writerow(p.__dict__)
    _eprint(f"{len(rows)} records -> {args.output}")
    _eprint("Next: add a 'domain' column (or set BRAVE_API_KEY and let `find` discover it), then run `findmyclient find`.")


def _lead_row(l: Lead) -> dict:
    p, v = l.practice, l.validation
    return {
        "practice": p.name, "npi": p.npi, "specialty": p.specialty, "city": p.city, "state": p.state, "phone": p.phone,
        "domain": p.domain, "contact": f"{p.contact_first} {p.contact_last}".strip(), "contact_title": p.contact_title,
        "email": l.email, "source": l.source, "status": v.status, "score": l.score, "reason": v.reason,
        "role": v.role, "catch_all": "" if v.catch_all is None else v.catch_all, "smtp": v.smtp,
    }


def cmd_find(args) -> None:
    with open(args.input, newline="") as f:
        practices = [Practice.from_row(r) for r in csv.DictReader(f)]
    search = default_search()
    if not search and not any(p.domain for p in practices):
        sys.exit("No domains in the input and BRAVE_API_KEY isn't set. Fill the 'domain' column or export BRAVE_API_KEY.")
    val = _validator(args)
    nodomain = 0

    def progress(p, leads):
        nonlocal nodomain
        nodomain += not p.domain
        _eprint(f"  {p.name[:45]:45} {p.domain or '-':30} {len(leads)} email(s)")

    results = run(practices, val, search=search, workers=args.workers, keep_invalid=args.keep_invalid, on_done=progress)
    n = 0
    with open(args.output, "w", newline="") as f:
        w = csv.DictWriter(f, LEAD_COLUMNS)
        w.writeheader()
        for _, leads in results:
            for l in leads:
                w.writerow(_lead_row(l))
                n += 1
    _eprint(f"{n} emails from {len(results)} practices ({nodomain} without a domain) -> {args.output}")


def cmd_verify(args) -> None:
    emails = list(args.emails)
    if args.file:
        emails += [ln.strip() for ln in open(args.file) if ln.strip()]
    if not emails:
        sys.exit("give emails or --file")
    val = _validator(args)
    w = csv.writer(sys.stdout)
    w.writerow(["email", "status", "reason", "role", "free_provider", "catch_all", "smtp"])
    for e in emails:
        v = val.validate(e)
        w.writerow([v.email, v.status, v.reason, v.role, v.free_provider, "" if v.catch_all is None else v.catch_all, v.smtp])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="findmyclient", description="Find and verify emails for US medical practices and providers.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def smtp_opts(p):
        p.add_argument("--smtp", action="store_true", help="probe mail servers (needs outbound port 25)")
        p.add_argument("--helo", help="hostname you control, used in EHLO")
        p.add_argument("--mail-from", help="a mailbox you own, used as MAIL FROM")
        p.add_argument("--delay", type=float, default=1.0, help="seconds between probes to one domain")

    s = sub.add_parser("search", help="list US practices/providers from the NPI Registry")
    s.add_argument("--state", default="", help="2-letter state, e.g. TX")
    s.add_argument("--city", default="")
    s.add_argument("--zip", default="")
    s.add_argument("--specialty", default="", help='NPI taxonomy description, e.g. "Internal Medicine", "Clinic/Center"')
    s.add_argument("--name", default="", help="organization name (or provider last name with --type individual); trailing * wildcard")
    s.add_argument("--type", choices=["org", "individual"], default="org")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("-o", "--output", default="practices.csv")
    s.set_defaults(fn=cmd_search)

    f = sub.add_parser("find", help="find and verify emails for the practices in a CSV")
    f.add_argument("input")
    f.add_argument("-o", "--output", default="leads.csv")
    f.add_argument("--workers", type=int, default=8)
    f.add_argument("--keep-invalid", action="store_true")
    smtp_opts(f)
    f.set_defaults(fn=cmd_find)

    v = sub.add_parser("verify", help="validate email addresses")
    v.add_argument("emails", nargs="*")
    v.add_argument("--file", help="text file, one email per line")
    smtp_opts(v)
    v.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
