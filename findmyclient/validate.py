"""Email validation: syntax -> DNS/MX -> disposable/role -> SMTP mailbox probe -> catch-all."""
from __future__ import annotations

import re
import smtplib
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

import dns.exception
import dns.resolver

from .models import Validation

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}$"
)

ROLE_LOCALS = {
    "info", "admin", "office", "contact", "support", "help", "sales", "billing", "frontdesk", "reception",
    "appointments", "appointment", "scheduling", "hr", "jobs", "careers", "noreply", "no-reply", "webmaster",
    "marketing", "team", "hello", "mail", "service", "services", "records", "manager", "accounts", "staff",
}

FREE_PROVIDERS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "live.com", "msn.com",
    "comcast.net", "att.net", "verizon.net", "sbcglobal.net", "protonmail.com", "me.com",
}

DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org", "yopmail.com",
    "trashmail.com", "throwawaymail.com", "getnada.com", "sharklasers.com", "maildrop.cc", "dispostable.com",
    "fakeinbox.com", "mailnesia.com", "mintemail.com", "spamgourmet.com", "mohmal.com", "emailondeck.com",
}


class Resolver(Protocol):
    def mx_hosts(self, domain: str) -> list[str] | None:
        """MX hosts by preference. [] = domain exists but accepts no mail. None = no such domain / DNS failure."""


class DnsResolver:
    def __init__(self, timeout: float = 8.0):
        self._r = dns.resolver.Resolver()
        self._r.lifetime = timeout
        self._cache: dict[str, list[str] | None] = {}

    def mx_hosts(self, domain: str) -> list[str] | None:
        if domain in self._cache:
            return self._cache[domain]
        result: list[str] | None
        try:
            answers = sorted(self._r.resolve(domain, "MX"), key=lambda a: a.preference)
            hosts = [str(a.exchange).rstrip(".") for a in answers]
            result = [] if hosts == [""] else hosts  # RFC 7505 null MX
        except (dns.resolver.NoAnswer):
            # RFC 5321: no MX -> implicit MX at the A/AAAA record.
            result = None
            for rtype in ("A", "AAAA"):
                try:
                    self._r.resolve(domain, rtype)
                    result = [domain]
                    break
                except dns.exception.DNSException:
                    continue
        except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.exception.Timeout, dns.exception.DNSException):
            result = None
        self._cache[domain] = result
        return result


# (mx_host, address, helo, mail_from, timeout) -> (smtp_code | None, message)
SmtpProbe = Callable[[str, str, str, str, float], "tuple[int | None, str]"]


def smtp_probe(mx: str, address: str, helo: str, mail_from: str, timeout: float) -> tuple[int | None, str]:
    """Ask the MX whether it would accept `address`. Never sends a message (no DATA)."""
    try:
        with smtplib.SMTP(timeout=timeout, local_hostname=helo) as s:
            s.connect(mx, 25)
            s.ehlo_or_helo_if_needed()
            code, msg = s.mail(mail_from)
            if code >= 400:
                return code, msg.decode(errors="replace")
            code, msg = s.rcpt(address)
            return code, msg.decode(errors="replace")
    except (OSError, smtplib.SMTPException, socket.timeout) as e:
        return None, f"{type(e).__name__}: {e}"


def port25_open(host: str = "gmail-smtp-in.l.google.com", timeout: float = 6.0) -> bool:
    """Most cloud hosts and many ISPs block outbound :25; without it SMTP checks are meaningless."""
    try:
        with socket.create_connection((host, 25), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class Validator:
    resolver: Resolver = field(default_factory=DnsResolver)
    use_smtp: bool = False
    probe: SmtpProbe = smtp_probe
    helo: str = "mail.example.com"
    mail_from: str = "verify@example.com"
    timeout: float = 10.0
    delay: float = 1.0  # seconds between probes to the same domain
    _catch_all: dict[str, bool | None] = field(default_factory=dict)
    _last_probe: dict[str, float] = field(default_factory=dict)
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _locks_guard: threading.Lock = field(default_factory=threading.Lock)

    def _domain_lock(self, domain: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(domain, threading.Lock())

    def _probe(self, mx_hosts: list[str], domain: str, address: str) -> tuple[int | None, str]:
        wait = self.delay - (time.monotonic() - self._last_probe.get(domain, 0.0))
        if wait > 0:
            time.sleep(wait)
        code, msg = None, ""
        for mx in mx_hosts[:2]:
            code, msg = self.probe(mx, address, self.helo, self.mail_from, self.timeout)
            if code is not None:
                break
        self._last_probe[domain] = time.monotonic()
        return code, msg

    def _is_catch_all(self, mx_hosts: list[str], domain: str) -> bool | None:
        if domain not in self._catch_all:
            code, _ = self._probe(mx_hosts, domain, f"fmc-{uuid.uuid4().hex[:12]}@{domain}")
            self._catch_all[domain] = None if code is None or 400 <= code < 500 else (200 <= code < 300)
        return self._catch_all[domain]

    def validate(self, email: str) -> Validation:
        email = email.strip()
        v = Validation(email=email)
        if not EMAIL_RE.match(email) or ".." in email:
            v.syntax_ok, v.status, v.reason = False, "invalid", "bad syntax"
            return v
        local, domain = email.rsplit("@", 1)
        domain = domain.lower()
        v.role = local.lower() in ROLE_LOCALS
        v.free_provider = domain in FREE_PROVIDERS
        if domain in DISPOSABLE:
            v.disposable, v.status, v.reason = True, "invalid", "disposable domain"
            return v

        mx = self.resolver.mx_hosts(domain)
        v.has_mx = bool(mx)
        if not mx:
            v.status = "invalid"
            v.reason = "domain accepts no mail (null MX)" if mx == [] else "domain does not exist / no DNS"
            return v

        if not self.use_smtp:
            v.status, v.reason = "unknown", "MX ok; SMTP probe not run"
            return v

        with self._domain_lock(domain):  # one conversation per mail server at a time
            code, msg = self._probe(mx, domain, email)
            catch_all = self._is_catch_all(mx, domain) if code is not None and 200 <= code < 300 else None
        if code is None:
            v.smtp, v.status, v.reason = "unknown", "unknown", f"SMTP unreachable ({msg[:80]})"
        elif 200 <= code < 300:
            v.smtp = "accepted"
            v.catch_all = catch_all
            if v.catch_all:
                v.status, v.reason = "risky", "server accepts all addresses; mailbox unconfirmed"
            elif v.catch_all is None:
                v.status, v.reason = "unknown", "accepted but catch-all test inconclusive"
            else:
                v.status, v.reason = "valid", "mailbox accepted by server"
        elif code in (550, 551, 553, 501, 511):  # unknown user / bad mailbox
            v.smtp, v.status, v.reason = "rejected", "invalid", f"mailbox rejected: {msg[:80]}"
        elif code >= 500:
            # 5xx can be policy blocks (spamhaus, IP reputation) rather than a missing mailbox.
            blocked = re.search(r"block|spam|reputation|denied|policy|blacklist|rbl", msg, re.I)
            v.smtp = "unknown" if blocked else "rejected"
            v.status = "unknown" if blocked else "invalid"
            v.reason = ("blocked by server policy: " if blocked else "rejected: ") + msg[:80]
        else:  # 4xx: greylisting / rate limit
            v.smtp, v.status, v.reason = "unknown", "unknown", f"temporary failure {code} (greylisting?)"
        return v
