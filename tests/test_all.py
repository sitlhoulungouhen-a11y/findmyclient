import pytest

from findmyclient.models import Validation

from findmyclient import patterns, scrape
from findmyclient.models import Practice
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


# ---- ingest ----
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

from findmyclient import domainprobe, ingest
from findmyclient.pipeline import Hooks, process_practice
from findmyclient.store import Store
from findmyclient.webui import make_handler

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "nppes_sample.csv")


def test_nppes_filters():
    allrows = list(ingest.read_nppes(FIX))
    assert [p.npi for p in allrows] == ["1111111111", "2222222222", "4444444444"]  # orgs only; deactivated dropped
    tx = list(ingest.read_nppes(FIX, states=["tx"]))
    assert [p.npi for p in tx] == ["1111111111", "2222222222"]
    im = list(ingest.read_nppes(FIX, taxonomy_prefixes=["207R"]))  # primary-switch taxonomy, not code_1
    assert [p.npi for p in im] == ["1111111111"] and im[0].specialty == "Internal Medicine"
    ind = list(ingest.read_nppes(FIX, entity="individual"))
    assert ind[0].name == "Tam Nguyen, MD" and ind[0].contact_last == "Nguyen"
    a = allrows[0]
    assert (a.name, a.contact_first, a.contact_title, a.zip, a.phone, a.city) == (
        "Acme Primary Care Associates Pc", "Jane", "Ceo", "75201", "2145550100", "Dallas")


def test_nppes_wrong_file():
    with pytest.raises(ValueError, match="NPPES"):
        list(ingest.read_nppes(os.path.join(os.path.dirname(__file__), "fixtures", "bad.csv")))


def test_csv_import_stable_ids(tmp_path):
    f = tmp_path / "crm.csv"
    f.write_text("name,zip,phone,domain,contact_first,contact_last\nAcme Clinic,75201,(214) 555-0100,acme.com,Jane,Doe\n")
    a, b = list(ingest.read_csv(str(f))), list(ingest.read_csv(str(f)))
    assert a[0].npi == b[0].npi and a[0].npi.startswith("local-")
    assert (a[0].phone, a[0].domain, a[0].domain_source, a[0].domain_confidence) == ("2145550100", "acme.com", "imported", 100)


# ---- store ----
def test_store_roundtrip_and_domain_not_clobbered(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    st.upsert_practices(ingest.read_nppes(FIX))
    assert st.stats()["practices"] == 3
    st.set_domain("1111111111", "acmepc.com", "probed", 75)
    st.upsert_practices(ingest.read_nppes(FIX))  # re-import must keep the found domain
    p = [x for x in st.practices() if x.npi == "1111111111"][0]
    assert (p.domain, p.domain_confidence) == ("acmepc.com", 75)
    assert [x.npi for x in st.practices(needs_domain=True, state="tx")] == ["2222222222"]
    assert [x.npi for x in st.practices(min_domain_confidence=50)] == ["1111111111"]


def test_csv_domain_overrides_and_sticks(tmp_path):
    st = Store(str(tmp_path / "t.db"))
    st.upsert_practices(ingest.read_nppes(FIX))
    f = tmp_path / "x.csv"
    f.write_text("npi,name,domain\n2222222222,Bayou Clinic,https://www.bayou.com/\n")
    st.upsert_practices(ingest.read_csv(str(f)))
    p = [x for x in st.practices() if x.npi == "2222222222"][0]
    assert p.domain == "bayou.com"  # URL normalised to a bare host
    assert p.city == "Houston" and p.phone == "7135550111"  # a sparse CSV must not blank registry fields


# ---- domain probing ----
def test_candidate_domains_order():
    p = Practice("1", "org", "Acme Primary Care Associates, P.C.", city="Dallas", state="TX")
    c = domainprobe.candidate_domains(p)
    assert c[0] == "acmeprimarycareassociates.com" and "acmeprimarycare.com" in c[:6]
    assert all(d.endswith(".com") for d in c[:6])
    assert domainprobe.candidate_domains(Practice("1", "org", "AB LLC")) == []  # too short to guess


class FakeResolver:
    def __init__(self, live):
        self.live = set(live)

    def mx_hosts(self, d):
        return ["mx"] if d in self.live else None


ACME = Practice("1", "org", "Acme Primary Care Associates PC", city="Dallas", state="TX", zip="75201", phone="2145550100")


def test_probe_accepts_only_with_identity_evidence():
    pages = {"https://acmeprimarycare.com/": "<html>Acme Primary Care. Call (214) 555-0100. 100 Main St, Dallas, TX 75201</html>",
             "https://acme.com/": "<html>Acme Corp: anvils and rockets since 1920</html>"}
    dom, conf, why = domainprobe.probe(ACME, FakeResolver(["acme.com", "acmeprimarycare.com"]), fetch=pages.get)
    assert dom == "acmeprimarycare.com" and conf == 100 and "phone" in why


def test_probe_rejects_resolving_but_unrelated_and_parked():
    pages = {"https://acmeprimarycare.com/": "<html>This domain is for sale! Buy now. Dallas</html>"}
    assert domainprobe.probe(ACME, FakeResolver(["acmeprimarycare.com"]), fetch=pages.get)[0] == ""
    assert domainprobe.probe(ACME, FakeResolver([]), fetch=lambda u: None)[0] == ""


def test_probe_uses_contact_page_and_respects_robots():
    pages = {"https://acmeprimarycare.com/": "<html>Welcome to Acme Primary Care</html>",
             "https://acmeprimarycare.com/contact": "Phone 214.555.0100"}
    assert domainprobe.probe(ACME, FakeResolver(["acmeprimarycare.com"]), fetch=pages.get)[0] == "acmeprimarycare.com"
    pages["https://acmeprimarycare.com/robots.txt"] = "User-agent: *\nDisallow: /"
    assert domainprobe.probe(ACME, FakeResolver(["acmeprimarycare.com"]), fetch=pages.get)[0] == ""


# ---- pipeline ----
SITE = {"https://clinic.com/": "Contact: <a href='mailto:billing@clinic.com'>"}


def practice(**kw):
    return Practice("1", "org", "Acme", contact_first="Jane", contact_last="Doe", domain="clinic.com",
                    domain_source="imported", domain_confidence=kw.pop("conf", 100), **kw)


def nofetch(u):
    return "" if u == "https://clinic.com/" else None


def test_no_crawl_by_default():
    v = make_validator(MX, mailboxes={"billing@clinic.com", "jane.doe@clinic.com"})
    _, leads = process_practice(practice(), v, fetch=SITE.get)
    assert [l.email for l in leads] == ["jane.doe@clinic.com"]  # billing@ only shows up with crawl=True


def test_crawl_scraped_plus_verified_pattern_ranking():
    v = make_validator(MX, mailboxes={"billing@clinic.com", "jane.doe@clinic.com"})
    _, leads = process_practice(practice(), v, crawl=True, fetch=SITE.get)
    assert [(l.email, l.source, l.score) for l in leads] == [("jane.doe@clinic.com", "pattern", 85), ("billing@clinic.com", "scraped", 80)]


def test_first_accepted_pattern_wins_and_invalid_dropped():
    v = make_validator(MX, mailboxes={"jdoe@clinic.com"})
    _, leads = process_practice(practice(), v)
    assert [l.email for l in leads] == ["jdoe@clinic.com"]
    assert v.calls[:3] == ["jane.doe@clinic.com", "jane@clinic.com", "jdoe@clinic.com"]


def test_catchall_gets_single_low_confidence_guess():
    v = make_validator({"clinic.com": ["mx"]}, catch_all={"clinic.com"})
    _, leads = process_practice(practice(), v)
    assert len(leads) == 1 and leads[0].validation.status == "risky" and leads[0].score == 35


def test_no_smtp_gives_one_unverified_guess():
    _, leads = process_practice(practice(), make_validator(MX, smtp=False))
    assert len(leads) == 1 and leads[0].validation.status == "unknown" and leads[0].score == 30


def test_low_domain_confidence_scales_score_and_no_domain_skips():
    _, leads = process_practice(practice(conf=60), make_validator(MX, mailboxes={"jane.doe@clinic.com"}))
    assert leads[0].score == 85 * 60 // 100
    assert process_practice(Practice("1", "org", "X"), make_validator(MX))[1] == []


def test_suppression_and_cache_hooks():
    v = make_validator(MX, mailboxes={"jane.doe@clinic.com", "jane@clinic.com"})
    hooks = Hooks(suppressed=lambda e: e == "jane.doe@clinic.com")
    _, leads = process_practice(practice(), v, hooks=hooks)
    assert [l.email for l in leads] == ["jane@clinic.com"] and "jane.doe@clinic.com" not in v.calls
    cached = Hooks(cached=lambda e: Validation(email=e, status="valid", smtp="accepted"))
    v.calls.clear()
    _, leads = process_practice(practice(), v, hooks=cached)
    assert leads[0].email == "jane.doe@clinic.com" and v.calls == []


# ---- feedback loop ----
def seeded_store(tmp_path):
    st = Store(str(tmp_path / "f.db"))
    ps = [Practice("A", "org", "Acme Clinic", contact_first="Jane", contact_last="Doe", domain="acme.com", domain_source="imported",
                   domain_confidence=100, state="TX"),
          Practice("B", "org", "Acme Clinic North", contact_first="Bob", contact_last="Smith", domain="acme.com",
                   domain_source="imported", domain_confidence=100, state="TX")]
    st.upsert_practices(ps)
    v = make_validator({"acme.com": ["mx"]}, smtp=False)
    _, leads = process_practice(ps[0], v)
    st.save_leads("A", leads)
    return st, leads[0].email


def test_bounce_suppresses_and_reply_teaches_pattern(tmp_path):
    st, email = seeded_store(tmp_path)
    assert email == "jane.doe@acme.com"
    assert st.record_feedback(email, "replied") == "{first}.{last}"
    assert st.learned_template("acme.com") == "{first}.{last}"
    assert st.lead_rows()[0]["status"] == "valid" and st.lead_rows()[0]["score"] == 100
    # the colleague at the same domain now gets the learned address at a bonus
    b = st.practices()[1]
    _, leads = process_practice(b, make_validator({"acme.com": ["mx"]}, smtp=False),
                                hooks=Hooks(learned_template=st.learned_template))
    assert (leads[0].email, leads[0].source) == ("bob.smith@acme.com", "learned")
    st.record_feedback(email, "bounced")
    assert st.is_suppressed(email) and st.lead_rows() == []
    with pytest.raises(ValueError):
        st.record_feedback(email, "maybe")


def test_validation_cache_expiry(tmp_path):
    st, email = seeded_store(tmp_path)
    assert st.cached_validation(email).email == email
    assert st.cached_validation(email, max_age_days=-1) is None


# ---- dashboard ----
def test_dashboard_security_and_feedback(tmp_path):
    st, email = seeded_store(tmp_path)
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(st, "tok123", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        html = urllib.request.urlopen(base + "/").read().decode()
        assert email in html and "tok123" in html
        csv_text = urllib.request.urlopen(base + "/export.csv").read().decode()
        assert csv_text.splitlines()[0].startswith("practice,") and email in csv_text

        def post(data, host=None):
            req = urllib.request.Request(base + "/feedback", urllib.parse.urlencode(data).encode(), method="POST")
            if host:
                req.add_header("Host", host)
            return urllib.request.urlopen(req)
        with pytest.raises(urllib.error.HTTPError) as e:
            post({"token": "wrong", "email": email, "result": "bounced"})
        assert e.value.code == 403 and not st.is_suppressed(email)
        with pytest.raises(urllib.error.HTTPError) as e:  # DNS-rebinding style Host
            post({"token": "tok123", "email": email, "result": "bounced"}, host="evil.example")
        assert e.value.code == 403
        post({"token": "tok123", "email": email, "result": "bounced"})
        assert st.is_suppressed(email)
    finally:
        srv.shutdown()


def test_dashboard_escapes_html(tmp_path):
    st = Store(str(tmp_path / "x.db"))
    st.upsert_practices([Practice("X", "org", "<script>alert(1)</script>", domain="a.com", domain_source="imported", domain_confidence=100)])
    st.save_leads("X", [type("L", (), {"email": "a@a.com", "source": "pattern", "score": 50, "validation": Validation("a@a.com")})()])
    from findmyclient.webui import lead_dicts
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), None); port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(st, "t", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
        assert "<script>alert" not in html and "&lt;script&gt;" in html
    finally:
        srv.shutdown()


def test_normalize_domain():
    from findmyclient.models import normalize_domain
    assert normalize_domain("https://www.Clinic.com/about?x=1") == "clinic.com"
    assert normalize_domain("Dr@Clinic.com") == "clinic.com" and normalize_domain("") == ""


def test_sparse_csv_attaches_domain_to_existing_npi(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    st.upsert_practices(ingest.read_nppes(FIX))
    f = tmp_path / "d.csv"
    f.write_text("npi,domain\n1111111111,acmepc.com\n,orphan.com\n")  # second row has neither npi nor name: ignored
    assert st.upsert_practices(ingest.read_csv(str(f))) == 1
    p = [x for x in st.practices() if x.npi == "1111111111"][0]
    assert (p.name, p.domain, p.domain_confidence, p.city) == ("Acme Primary Care Associates Pc", "acmepc.com", 100, "Dallas")


# ---- nppes fetch ----
def test_monthly_urls_roll_back_across_year():
    import datetime
    from findmyclient import nppes_fetch
    urls = list(nppes_fetch.monthly_urls(datetime.date(2027, 1, 15)))
    assert [u.rsplit("_", 2)[-2:] for u in urls] == [["January", "2027.zip"], ["December", "2026.zip"], ["November", "2026.zip"]]


def test_fetch_streams_main_file_not_header_and_falls_back(tmp_path):
    import datetime
    import zipfile
    import urllib.error
    from findmyclient import nppes_fetch
    src = open(FIX, "rb").read()

    def fake_open(url, dest, progress):
        if "October" in url:
            raise urllib.error.URLError("404 not published yet")
        with zipfile.ZipFile(dest, "w") as z:
            z.writestr("npidata_pfile_20050523-20260913_FileHeader.csv", "NPI\n")
            z.writestr("npidata_pfile_20050523-20260913.csv", src)
            z.writestr("endpoint_pfile_20050523-20260913.csv", "x\n")
    zp = nppes_fetch.fetch_zip(str(tmp_path), today=datetime.date(2026, 10, 3), opener=fake_open)
    with nppes_fetch.open_npidata(zp) as f:
        got = list(ingest.read_nppes(f))
    assert len(got) == 3 and sorted(os.listdir(tmp_path)) == ["nppes.zip"]  # nothing extracted to disk


def test_title_filter():
    assert [p.npi for p in ingest.read_nppes(FIX, titles=["administrator", "manager"])] == ["2222222222"]
    assert [p.npi for p in ingest.read_nppes(FIX, titles=["ceo"])] == ["1111111111"]


def test_fetch_failure_names_every_url(tmp_path):
    import urllib.error
    from findmyclient import nppes_fetch

    def boom(url, dest, progress):
        raise urllib.error.URLError("proxy 403")
    with pytest.raises(RuntimeError, match="download.cms.gov") as e:
        nppes_fetch.fetch_zip(str(tmp_path), opener=boom)
    assert str(e.value).count("proxy 403") == 3
