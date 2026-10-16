# findmyclient

Self-hosted lead toolkit for finding and verifying decision-maker emails at US medical practices
(for RCM / back-office outsourcing). No search engines, directories, enrichment APIs or SaaS: everything
runs on your machine against a local SQLite file.

The only outside systems it talks to are the ones inherent to email itself: DNS, the **practice's own**
website (to confirm a domain is really theirs; optionally to read published addresses), and the
practice's mail server (SMTP mailbox check).

```
import-nppes / import-csv  ->  domains  ->  find  ->  export / serve
        (your files)        (in-house)   (verify)     (CSV / dashboard)
                                             ^                |
                                             +--- feedback ---+   bounces suppress, replies teach patterns
```

## Install

```bash
pip install -e '.[dev]'
```

## Workflow

```bash
# 1. Load practices from files you hold. NPPES bulk file (npidata_pfile_*.csv) is read as a stream, so the
#    multi-GB file is fine. Filter on the way in. Authorized official (owner/CEO/administrator) becomes the contact.
findmyclient import-nppes npidata_pfile_2026.csv --state TX --state FL --taxonomy 207R --taxonomy 261Q
findmyclient import-csv our_crm_export.csv        # any CSV with `name` (+ optional domain, phone, city, zip, contact_first, contact_last ...)
findmyclient import-csv domains_we_know.csv       # a sparse `npi,domain` file attaches domains to existing records

# 2. Domains. In-house engine: derive candidate domains from the name, keep those in DNS, then require the
#    practice's own homepage to show its phone / ZIP / city / name. Resolving alone is never enough.
findmyclient domains --state TX

# 3. Emails. Guess the contact's address from name patterns and verify against the mail server:
findmyclient find --smtp --helo mail.yourco.in --mail-from verify@yourco.in
#    add --crawl to also read addresses published on the practice's own site

# 4. Use them
findmyclient export -o leads.csv --min-score 60
findmyclient serve                                # dashboard on http://127.0.0.1:8765

# 5. Close the loop (from your sending tool's reports): this is what makes the tool better than a static list
findmyclient feedback bounced a@x.com b@y.com
findmyclient feedback-file outcomes.csv           # columns: email,result
```

`result` is one of `delivered replied bounced unsubscribed complained`.
- `bounced`, `unsubscribed`, `complained`: address is suppressed permanently and excluded from every export.
- `replied`: lead becomes score 100, and the tool learns that domain's address convention (e.g. `first.last`)
  and applies it first for other contacts at the same domain.

## Scoring

`status`: `valid` (mailbox accepted, not a catch-all), `risky` (catch-all, so acceptance proves nothing),
`unknown` (couldn't tell), `invalid` (dropped unless `--keep-invalid`). `score` is lower for guessed patterns than
for published or learned addresses, lower for role addresses (`info@`, `billing@`), and scaled down when the
domain match itself was only moderately confident. Verification results are cached 14 days (`--recheck` to bypass).

## Limits you should know about

- **Domain discovery has finite recall.** It finds practices with a branded domain matching their legal name
  (`acmeprimarycare.com`). Practices on a website builder subdomain, a hospital-system domain, or a name that
  differs from the legal entity will be missed; the rest come from `import-csv` (your CRM, call sheets, replies).
  I have not measured the hit rate on real registry data; run `domains` on a few hundred and read the output.
- **Without `--smtp` nothing is mailbox-verified**, only MX-checked. SMTP checks need **outbound port 25**, which
  most cloud hosts and many ISPs block (the tool warns and reports `unknown`, never a false `valid`). Use a VPS
  that allows it, with reverse DNS matching `--helo`. Google Workspace / Microsoft 365 often accept everything or
  throttle probes, so those come back `risky`/`unknown`. Keep `--delay` >= 1s.
- `import-nppes` needs the bulk file on disk; get it once from CMS (public data) and keep it, nothing in this
  tool fetches it. Individual providers (`--entity individual`) rarely have their own domain; target orgs.
- The dashboard binds to 127.0.0.1, rejects foreign `Host` headers, and requires a per-run token for writes.
  Don't put it behind a public proxy without adding real authentication.

## Sending (outside this tool, but decides whether the list pays off)

- US **CAN-SPAM** applies even when you send from India: truthful headers, physical address, working unsubscribe,
  honour opt-outs within 10 business days. Use `feedback unsubscribed` so they are never exported again.
- Send from a **separate domain** with SPF/DKIM/DMARC and warm it up. Keep bounces under ~2%.
- Offshore billing touches PHI, so US practices will ask for a **HIPAA BAA** and your security controls.

## Tests

```bash
pytest   # offline: DNS, SMTP and HTTP are faked
```
