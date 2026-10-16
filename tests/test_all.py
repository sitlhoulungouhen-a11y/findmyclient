import pytest

from findmyclient import npi, patterns, scrape
from findmyclient.domains import discover_domain, is_directory, normalize_domain
from findmyclient.models import Practice
from findmyclient.pipeline import process_practice
from findmyclient.validate import Validator


class FakeDns:
    def __init__(self, table):
        self.table = table

    def mx_hosts(self, d):
        return self.table.get(d)


def make_validator(mx, mailboxes=None, catch_all=(), smtp=True, code_override=None):
    """mailboxes: set of addresses the fake server accepts."""
    mailboxes = mailboxes or set()
    calls = []

    def probe(host, addr, helo, mail_from, timeout):
        calls.append(addr)
        if code_override:
            return code_override
        dom = addr.split("@")[1]
        if dom in catch_all or addr in mailboxes:
            return 250, "ok"
        return 550, "5.1.1 user unknown"

    v = Validator(resolver=FakeDns(mx), use_smtp=smtp, probe=probe, delay=0)
    v.calls = calls
    return v


MX = {"clinic.com": ["mx.clinic.com"], "nullmail.com": [], "catchall.com": ["mx.catchall.com"]}


# ---- validation ----
@pytest.mark.parametrize("e", ["nope", "a@b", "a@@b.com", "a..b@x.com", "@x.com", "a b@x.com"])
def test_bad_syntax(e):
    assert make_validator(MX).validate(e).status == "invalid"


def test_no_dns_and_null_mx():
    v = make_validator(MX)
    assert v.validate("a@ghost.com").reason.startswith("domain does not exist")
    assert "null MX" in v.validate("a@nullmail.com").reason


def test_disposable_and_flags():
    v = make_validator({"gmail.com": ["g"]}, smtp=False)
    assert v.validate("x@mailinator.com").status == "invalid"
    r = v.validate("info@gmail.com")
    assert r.role and r.free_provider


def test_smtp_valid_rejected_catchall():
    v = make_validator(MX, mailboxes={"dr@clinic.com"}, catch_all={"catchall.com"})
    assert v.validate("dr@clinic.com").status == "valid"
    assert v.validate("zz@clinic.com").status == "invalid"
    r = v.validate("anyone@catchall.com")
    assert r.status == "risky" and r.catch_all is True


def test_catchall_probe_cached_per_domain():
    v = make_validator(MX, mailboxes={"a@clinic.com", "b@clinic.com"})
    v.validate("a@clinic.com"); v.validate("b@clinic.com")
    random_probes = [c for c in v.calls if c.startswith("fmc-")]
    assert len(random_probes) == 1


def test_smtp_unreachable_is_unknown_not_invalid():
    v = make_validator(MX, code_override=(None, "timeout"))
    r = v.validate("a@clinic.com")
    assert r.status == "unknown" and r.smtp == "unknown"


def test_greylist_and_policy_block_not_invalid():
    assert make_validator(MX, code_override=(451, "try later")).validate("a@clinic.com").status == "unknown"
    r = make_validator(MX, code_override=(554, "5.7.1 blocked by spamhaus")).validate("a@clinic.com")
    assert r.status == "unknown"


def test_mx_only_mode_never_claims_valid():
    r = make_validator(MX, smtp=False).validate("a@clinic.com")
    assert r.status == "unknown" and r.smtp == "skipped"


# ---- patterns ----
def test_patterns():
    c = patterns.candidates("John", "O'Neil, MD", "clinic.com")
    assert c[0] == "john.oneil@clinic.com" and "joneil@clinic.com" in c
    assert patterns.candidates("", "Smith", "x.com") == []
    assert patterns.candidates("José", "Núñez", "x.com")[0] == "jose.nunez@x.com"


# ---- scraping ----
def test_extract_emails():
    html = ('<a href="mailto:Billing@Clinic.com?subject=hi">x</a> office [at] clinic [dot] com '
            'bob(at)clinic.com logo@2x.png a@example.com &#105;nfo@clinic.com')
    got = scrape.extract_emails(html)
    assert {"billing@clinic.com", "bob@clinic.com", "info@clinic.com"} <= got
    assert not any(e.endswith((".png", "example.com")) for e in got)


def test_scrape_honours_robots():
    pages = {
        "https://clinic.com/": "<a href='mailto:a@clinic.com'>",
        "https://clinic.com/robots.txt": "User-agent: *\nDisallow: /contact",
        "https://clinic.com/contact": "secret@clinic.com",
        "https://clinic.com/about": "boss@clinic.com",
    }
    got = scrape.scrape_domain("clinic.com", fetch=pages.get)
    assert set(got) == {"a@clinic.com", "boss@clinic.com"}


def test_scrape_dead_site():
    assert scrape.scrape_domain("dead.com", fetch=lambda u: None) == {}


# ---- domains ----
def test_domains():
    assert normalize_domain("https://www.Clinic.com/about") == "clinic.com"
    assert normalize_domain("") == ""
    assert is_directory("www.healthgrades.com") and not is_directory("clinic.com")
    p = Practice("1", "org", "Acme Clinic", city="Austin", state="TX")
    assert discover_domain(p, lambda q: ["https://www.yelp.com/x", "https://acmeclinic.com/"]) == "acmeclinic.com"


# ---- npi ----
ORG = {"number": 1234567890, "enumeration_type": "NPI-2",
       "basic": {"organization_name": "ACME PRIMARY CARE LLC", "authorized_official_first_name": "JANE",
                 "authorized_official_last_name": "DOE", "authorized_official_title_or_position": "CEO"},
       "addresses": [{"address_purpose": "MAILING", "city": "X"},
                     {"address_purpose": "LOCATION", "address_1": "1 MAIN ST", "city": "DALLAS", "state": "TX",
                      "postal_code": "752010000", "telephone_number": "214-555-0100"}],
       "taxonomies": [{"desc": "Internal Medicine", "primary": True}]}


def test_npi_parse_and_paging():
    pages = []

    def fetch(url):
        pages.append(url)
        return {"results": [ORG] * 200} if len(pages) == 1 else {"results": [ORG] * 5}

    out = list(npi.search(state="TX", limit=300, fetch=fetch))
    assert len(out) == 205 and len(pages) == 2 and "skip=200" in pages[1]
    p = out[0]
    assert (p.name, p.contact_first, p.contact_title, p.city, p.zip, p.specialty) == (
        "Acme Primary Care Llc", "Jane", "Cto".replace("Cto", "Ceo"), "Dallas", "75201", "Internal Medicine")


def test_npi_errors_and_filters():
    with pytest.raises(ValueError):
        list(npi.search())
    with pytest.raises(RuntimeError, match="bad"):
        list(npi.search(state="TX", fetch=lambda u: {"Errors": [{"description": "bad"}]}))


# ---- pipeline ----
SITE = {"https://clinic.com/": "Contact: <a href='mailto:billing@clinic.com'>"}


def practice(**kw):
    return Practice("1", "org", "Acme", contact_first="Jane", contact_last="Doe", domain="https://www.clinic.com", **kw)


def test_pipeline_scraped_plus_verified_pattern():
    v = make_validator(MX, mailboxes={"billing@clinic.com", "jane.doe@clinic.com"})
    p, leads = process_practice(practice(), v, fetch=SITE.get)
    assert [(l.email, l.source, l.validation.status) for l in leads] == [
        ("jane.doe@clinic.com", "pattern", "valid"), ("billing@clinic.com", "scraped", "valid")]
    assert leads[0].score == 85 and leads[1].score == 80  # role address penalised (-15) from 95


def test_pipeline_stops_at_first_accepted_pattern_and_drops_invalid():
    v = make_validator(MX, mailboxes={"jdoe@clinic.com"})
    _, leads = process_practice(practice(), v, fetch=lambda u: None if "robots" in u or u != "https://clinic.com/" else "")
    assert [l.email for l in leads] == ["jdoe@clinic.com"]
    # guessed jane.doe, jane, jdoe in order; stopped after jdoe
    assert v.calls[:3] == ["jane.doe@clinic.com", "jane@clinic.com", "jdoe@clinic.com"]


def test_pipeline_catchall_domain_gets_single_low_confidence_guess():
    v = make_validator({"clinic.com": ["mx"]}, catch_all={"clinic.com"})
    _, leads = process_practice(practice(), v, fetch=lambda u: "" if u == "https://clinic.com/" else None)
    assert len(leads) == 1 and leads[0].validation.status == "risky" and leads[0].score == 35


def test_pipeline_no_smtp_gives_one_unverified_guess():
    v = make_validator(MX, smtp=False)
    _, leads = process_practice(practice(), v, fetch=lambda u: "" if u == "https://clinic.com/" else None)
    assert len(leads) == 1 and leads[0].validation.status == "unknown" and leads[0].score == 30


def test_pipeline_discovers_domain_and_survives_search_failure():
    v = make_validator(MX, smtp=False)
    p = Practice("1", "org", "Acme", city="A", state="TX")

    def boom(q):
        raise RuntimeError("quota")
    p2, leads = process_practice(p, v, search=boom, fetch=lambda u: None)
    assert leads == [] and p2.domain == ""
    p3, _ = process_practice(Practice("1", "org", "Acme"), v, search=lambda q: ["https://acme.com"], fetch=lambda u: None)
    assert p3.domain == "acme.com"
