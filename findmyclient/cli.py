from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import domainprobe, ingest
from .models import Lead
from .pipeline import Hooks, run
from .store import RESULTS, Store
from .validate import DnsResolver, Validator, port25_open

EXPORT_COLUMNS = [
    "practice", "npi", "specialty", "city", "state", "phone", "domain", "domain_confidence", "contact", "contact_title",
    "email", "source", "status", "score", "reason", "role",
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


def cmd_import_nppes(args, store: Store) -> None:
    it = ingest.read_nppes(args.file, states=args.state, cities=args.city, entity=args.entity,
                           taxonomy_prefixes=args.taxonomy, limit=args.limit)
    _eprint(f"{store.upsert_practices(it)} practices imported (filters: state={args.state or 'any'} entity={args.entity} "
            f"taxonomy={args.taxonomy or 'any'})")


def cmd_import_csv(args, store: Store) -> None:
    _eprint(f"{store.upsert_practices(ingest.read_csv(args.file))} practices imported/updated")


def cmd_domains(args, store: Store) -> None:
    todo = store.practices(state=args.state, needs_domain=True, unchecked_domains=not args.retry, limit=args.limit)
    todo = [p for p in todo if p.kind == "org" or args.individuals]
    if not todo:
        return _eprint("nothing to do (no un-probed practices without a domain; use --retry to re-probe)")
    resolver, found = DnsResolver(timeout=4.0), 0

    def work(p):
        return p, domainprobe.probe(p, resolver)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for p, (dom, conf, why) in ex.map(work, todo):
            if dom:
                store.set_domain(p.npi, dom, "probed", conf); found += 1
                _eprint(f"  {p.name[:42]:42} -> {dom:34} {conf:3} ({','.join(why)})")
            else:
                store.mark_domain_checked(p.npi)
    _eprint(f"{found}/{len(todo)} domains confirmed. The rest need a manual `import-csv` with a domain column "
            f"(your CRM, a call sheet, past replies).")


def cmd_find(args, store: Store) -> None:
    practices = store.practices(state=args.state, min_domain_confidence=args.min_domain_confidence, limit=args.limit)
    if not practices:
        return _eprint("no practices with a confident domain; run `domains` or import a CSV with a domain column")
    val = _validator(args)
    ttl = 0 if args.recheck else 14
    hooks = Hooks(suppressed=store.is_suppressed, cached=lambda e: store.cached_validation(e, ttl) if ttl else None,
                  learned_template=store.learned_template)
    n = 0

    def done(p, leads: list[Lead]):
        nonlocal n
        store.save_leads(p.npi, leads); n += len(leads)
        _eprint(f"  {p.name[:42]:42} {p.domain:30} {len(leads)} email(s)")

    run(practices, val, hooks=hooks, crawl=args.crawl, workers=args.workers, keep_invalid=args.keep_invalid, on_done=done)
    _eprint(f"{n} leads from {len(practices)} practices. `export` or `serve` to use them.")
    if not args.smtp:
        _eprint("Note: without --smtp nothing is mailbox-verified; leads are unverified pattern guesses (score <= 30).")


def cmd_export(args, store: Store) -> None:
    rows = store.lead_rows(state=args.state, min_score=args.min_score, status=args.status)
    pr = {p.npi: p for p in store.practices()}
    with open(args.output, "w", newline="") as f:
        w = csv.DictWriter(f, EXPORT_COLUMNS)
        w.writeheader()
        for r in rows:
            p = pr[r["npi"]]
            w.writerow({"practice": p.name, "npi": p.npi, "specialty": p.specialty, "city": p.city, "state": p.state,
                        "phone": p.phone, "domain": p.domain, "domain_confidence": p.domain_confidence,
                        "contact": f"{p.contact_first} {p.contact_last}".strip(), "contact_title": p.contact_title,
                        "email": r["email"], "source": r["source"], "status": r["status"], "score": r["score"],
                        "reason": r["reason"], "role": bool(r["role"])})
    _eprint(f"{len(rows)} leads -> {args.output} (suppressed addresses excluded)")


def cmd_feedback(args, store: Store) -> None:
    for email in args.emails:
        learned = store.record_feedback(email, args.result)
        _eprint(f"{email}: {args.result}" + (f" (learned pattern {learned} for @{email.split('@')[1]})" if learned else ""))


def cmd_feedback_file(args, store: Store) -> None:
    """CSV with columns email,result: e.g. exported from your sending tool's bounce/reply report."""
    n = 0
    with open(args.file, newline="") as f:
        for r in csv.DictReader(f):
            try:
                store.record_feedback(r["email"], r["result"].strip().lower()); n += 1
            except ValueError as e:
                _eprint(f"skipped {r.get('email')}: {e}")
    _eprint(f"{n} outcomes recorded")


def cmd_verify(args, store: Store) -> None:
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
        if store.is_suppressed(e):
            v.status, v.reason = "invalid", "on suppression list"
        w.writerow([v.email, v.status, v.reason, v.role, v.free_provider, "" if v.catch_all is None else v.catch_all, v.smtp])


def cmd_stats(args, store: Store) -> None:
    for k, v in store.stats().items():
        print(f"{k:28}{v}")


def cmd_serve(args, store: Store) -> None:
    from .webui import serve
    serve(store, args.port)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="findmyclient", description="Internal lead-finding toolkit for US medical practices.")
    ap.add_argument("--db", default=os.environ.get("FMC_DB", "findmyclient.db"), help="SQLite file (default ./findmyclient.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    def smtp_opts(p):
        p.add_argument("--smtp", action="store_true", help="probe mail servers directly (needs outbound port 25)")
        p.add_argument("--helo", help="hostname you control, used in EHLO")
        p.add_argument("--mail-from", help="a mailbox you own, used as MAIL FROM")
        p.add_argument("--delay", type=float, default=1.0, help="seconds between probes to one domain")

    p = add("import-nppes", cmd_import_nppes, "load practices from the NPPES bulk file (npidata_pfile_*.csv) you have on disk")
    p.add_argument("file")
    p.add_argument("--state", action="append", default=[], help="repeatable, e.g. --state TX --state FL")
    p.add_argument("--city", action="append", default=[])
    p.add_argument("--entity", choices=["org", "individual", "both"], default="org")
    p.add_argument("--taxonomy", action="append", default=[], help="taxonomy code prefix, e.g. 207R (internal medicine), 261Q (clinics)")
    p.add_argument("--limit", type=int)

    p = add("import-csv", cmd_import_csv, "load practices from your own CSV (CRM export, purchased list); add a `domain` column if you have it")
    p.add_argument("file")

    p = add("domains", cmd_domains, "find practice websites by probing name-derived domains and confirming identity")
    p.add_argument("--state", default="")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--retry", action="store_true", help="re-probe practices that already failed")
    p.add_argument("--individuals", action="store_true", help="also probe individual providers (low hit rate)")

    p = add("find", cmd_find, "generate + verify emails for practices with a confident domain")
    p.add_argument("--state", default="")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--min-domain-confidence", type=int, default=50)
    p.add_argument("--crawl", action="store_true", help="also read emails published on the practice's own site")
    p.add_argument("--recheck", action="store_true", help="ignore the 14-day verification cache")
    p.add_argument("--keep-invalid", action="store_true")
    smtp_opts(p)

    p = add("export", cmd_export, "write leads to CSV (suppressed addresses excluded)")
    p.add_argument("-o", "--output", default="leads.csv")
    p.add_argument("--state", default="")
    p.add_argument("--min-score", type=int, default=50)
    p.add_argument("--status", default="")

    p = add("feedback", cmd_feedback, "record an outcome so the tool learns and suppresses")
    p.add_argument("result", choices=sorted(RESULTS))
    p.add_argument("emails", nargs="+")

    p = add("feedback-file", cmd_feedback_file, "bulk outcomes from a CSV with columns email,result")
    p.add_argument("file")

    p = add("verify", cmd_verify, "validate arbitrary email addresses")
    p.add_argument("emails", nargs="*")
    p.add_argument("--file")
    smtp_opts(p)

    add("stats", cmd_stats, "database summary")
    p = add("serve", cmd_serve, "local dashboard (127.0.0.1 only)")
    p.add_argument("--port", type=int, default=8765)

    args = ap.parse_args(argv)
    store = Store(args.db)
    try:
        args.fn(args, store)
    finally:
        store.close()


if __name__ == "__main__":
    main()
