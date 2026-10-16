# findmyclient

Find and verify decision-maker emails at US medical practices, for Indian RCM / back-office
outsourcing companies selling offshore teams to US practices.

```
search  ->  practices.csv   (CMS NPI Registry: name, specialty, address, phone, authorized official + title)
find    ->  leads.csv       (domain -> site scrape + name-pattern guesses -> validate -> score)
verify  ->  any email list  (syntax, MX, disposable, role, SMTP mailbox probe, catch-all)
```

## Install

```bash
pip install -e '.[dev]'
```

## Use

```bash
# 1. Practices from the NPI Registry (free, no key). Organisations carry an "authorized
#    official" (owner/administrator/CEO), the person you want to reach.
findmyclient search --state TX --city Dallas --specialty "Internal Medicine" --limit 200 -o practices.csv

# 2. Domains. Either fill the `domain` column yourself, or set a Brave Search API key:
export BRAVE_API_KEY=...

# 3. Find + verify. DNS/MX checks only:
findmyclient find practices.csv -o leads.csv
#    ...plus mailbox-level SMTP checks (see "SMTP" below):
findmyclient find practices.csv -o leads.csv --smtp --helo mail.yourco.in --mail-from verify@yourco.in

# Verify an existing list
findmyclient verify --file emails.txt --smtp --helo mail.yourco.in --mail-from verify@yourco.in
```

`leads.csv` is ranked by `score` (0-100). `status`: `valid` (server accepted the mailbox, not a catch-all),
`risky` (catch-all domain, so acceptance proves nothing), `unknown` (couldn't tell), `invalid` (dropped
unless `--keep-invalid`). `source`: `scraped` (published on the practice's site) or `pattern` (guessed from the authorized official's name).

## Limits you should know about

- **SMTP verification needs outbound port 25.** Most cloud hosts and many Indian ISPs block it; the tool
  warns and reports `unknown` rather than guessing. Run it from a VPS that allows port 25, with a real
  hostname and reverse DNS. Probing never sends a message (no DATA), but hammering servers from a bare IP
  gets you blocklisted, so keep `--delay` >= 1s.
- **Catch-all and big providers.** Google Workspace and Microsoft 365 often accept every RCPT or throttle
  probes; those come back `risky`/`unknown`. Treat `valid` as strong evidence and `unknown` as "send a
  small test batch and watch bounces."
- **NPI Registry has no websites or emails.** Domains come from your CSV or Brave Search (directories such as
  Healthgrades/Yelp are filtered out). Wrong-domain matches happen; spot-check.
- Registry results cap at ~1200 per query. Slice by city / ZIP / specialty to go deeper.
- Site scraping honours `robots.txt`, fetches at most six pages per site.

## Sending (not part of this tool, but it decides whether the list is worth anything)

- US **CAN-SPAM**: truthful headers, a physical postal address, a working unsubscribe, honour opt-outs
  within 10 business days. Applies to you even though you're sending from India.
- Use a **separate sending domain** with SPF/DKIM/DMARC and warm it up; never send cold volume from your main domain.
- Stay under ~2% bounce rate; remove `invalid`/`unknown` hard bounces after each wave.
- If you'll touch patient data (claims, eligibility), US practices will require a **HIPAA BAA** and
  ask about your security controls. Lead with that in the pitch; it's the main objection for offshore RCM.

## Tests

```bash
pytest   # offline: DNS, SMTP, HTTP and the NPI API are all faked
```
