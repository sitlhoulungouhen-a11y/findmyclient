"""SQLite store: practices, leads, verification cache, learned email patterns, suppression list."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict
from typing import Iterable

from .models import Lead, Practice, Validation

SCHEMA = """
CREATE TABLE IF NOT EXISTS practices (
  npi TEXT PRIMARY KEY, kind TEXT, name TEXT, specialty TEXT, taxonomy TEXT, address TEXT, city TEXT,
  state TEXT, zip TEXT, phone TEXT, contact_first TEXT, contact_last TEXT, contact_title TEXT,
  domain TEXT DEFAULT '', domain_source TEXT DEFAULT '', domain_confidence INTEGER DEFAULT 0,
  domain_checked_at REAL, leads_checked_at REAL
);
CREATE INDEX IF NOT EXISTS ix_practices_state ON practices(state);
CREATE INDEX IF NOT EXISTS ix_practices_domain ON practices(domain);
CREATE TABLE IF NOT EXISTS leads (
  email TEXT PRIMARY KEY, npi TEXT, source TEXT, score INTEGER, status TEXT, reason TEXT, role INTEGER,
  free_provider INTEGER, catch_all INTEGER, smtp TEXT, has_mx INTEGER, checked_at REAL
);
CREATE INDEX IF NOT EXISTS ix_leads_npi ON leads(npi);
CREATE TABLE IF NOT EXISTS feedback (email TEXT, result TEXT, at REAL);
CREATE TABLE IF NOT EXISTS suppressed (email TEXT PRIMARY KEY, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS domain_patterns (domain TEXT PRIMARY KEY, template TEXT, confirmations INTEGER, at REAL);
"""

_PRACTICE_COLS = [c for c in Practice.csv_columns()]
SUPPRESSING = {"bounced", "unsubscribed", "complained"}
RESULTS = SUPPRESSING | {"delivered", "replied"}


class Store:
    def __init__(self, path: str = "findmyclient.db"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- practices ----
    def upsert_practices(self, practices: Iterable[Practice], batch: int = 5000) -> int:
        """Insert or refresh registry fields; never clobbers a domain already found or set by hand."""
        n = 0
        cols = [c for c in _PRACTICE_COLS if not c.startswith("domain")]
        sql = (f"INSERT INTO practices ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
               f"ON CONFLICT(npi) DO UPDATE SET "
               f"{','.join(f'{c}=COALESCE(NULLIF(excluded.{c},\'\'), practices.{c})' for c in cols if c != 'npi')}")
        rows = []
        with self._lock:
            for p in practices:
                rows.append([getattr(p, c) for c in cols])
                if p.domain:
                    self._db.execute(
                        "INSERT OR IGNORE INTO practices (npi) VALUES (?)", (p.npi,))
                    self._db.execute(
                        "UPDATE practices SET domain=?, domain_source=?, domain_confidence=? WHERE npi=?",
                        (p.domain, p.domain_source or "imported", p.domain_confidence or 100, p.npi))
                if len(rows) >= batch:
                    self._db.executemany(sql, rows); n += len(rows); rows = []
            if rows:
                self._db.executemany(sql, rows); n += len(rows)
            self._db.commit()
        return n

    def practices(self, *, state: str = "", needs_domain: bool = False, min_domain_confidence: int = 0,
                  unchecked_domains: bool = False, limit: int | None = None) -> list[Practice]:
        q, args = "SELECT * FROM practices WHERE 1=1", []
        if state:
            q += " AND state=?"; args.append(state.upper())
        if needs_domain:
            q += " AND domain=''"
        if unchecked_domains:
            q += " AND domain_checked_at IS NULL"
        if min_domain_confidence:
            q += " AND domain!='' AND domain_confidence>=?"; args.append(min_domain_confidence)
        q += " ORDER BY npi" + (f" LIMIT {int(limit)}" if limit else "")
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [Practice(**{c: r[c] for c in _PRACTICE_COLS}) for r in rows]

    def set_domain(self, npi: str, domain: str, source: str, confidence: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE practices SET domain=?, domain_source=?, domain_confidence=?, domain_checked_at=? WHERE npi=?",
                (domain, source, confidence, time.time(), npi))
            self._db.commit()

    def mark_domain_checked(self, npi: str) -> None:
        with self._lock:
            self._db.execute("UPDATE practices SET domain_checked_at=? WHERE npi=?", (time.time(), npi))
            self._db.commit()

    # ---- leads / validation cache ----
    def cached_validation(self, email: str, max_age_days: float = 14) -> Validation | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM leads WHERE email=? AND checked_at>?",
                                 (email, time.time() - max_age_days * 86400)).fetchone()
        if not r:
            return None
        return Validation(email=email, status=r["status"], reason=r["reason"] or "", has_mx=None if r["has_mx"] is None else bool(r["has_mx"]),
                          role=bool(r["role"]), free_provider=bool(r["free_provider"]), smtp=r["smtp"] or "skipped",
                          catch_all=None if r["catch_all"] is None else bool(r["catch_all"]))

    def save_leads(self, npi: str, leads: Iterable[Lead]) -> None:
        now = time.time()
        with self._lock:
            for l in leads:
                v = l.validation
                self._db.execute(
                    "INSERT OR REPLACE INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (l.email, npi, l.source, l.score, v.status, v.reason, int(v.role), int(v.free_provider),
                     None if v.catch_all is None else int(v.catch_all), v.smtp, None if v.has_mx is None else int(v.has_mx), now))
            self._db.execute("UPDATE practices SET leads_checked_at=? WHERE npi=?", (now, npi))
            self._db.commit()

    def lead_rows(self, *, state: str = "", min_score: int = 0, status: str = "", limit: int = 100000) -> list[sqlite3.Row]:
        q = ("SELECT l.*, p.name, p.specialty, p.city, p.state, p.phone, p.domain, p.contact_first, p.contact_last, "
             "p.contact_title, p.domain_confidence FROM leads l JOIN practices p USING(npi) "
             "WHERE l.score>=? AND l.email NOT IN (SELECT email FROM suppressed)")
        args: list = [min_score]
        if state:
            q += " AND p.state=?"; args.append(state.upper())
        if status:
            q += " AND l.status=?"; args.append(status)
        q += " ORDER BY l.score DESC, p.name LIMIT ?"; args.append(limit)
        with self._lock:
            return self._db.execute(q, args).fetchall()

    # ---- feedback loop ----
    def is_suppressed(self, email: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM suppressed WHERE email=?", (email.lower(),)).fetchone() is not None

    def suppress(self, email: str, reason: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO suppressed VALUES (?,?,?)", (email.lower(), reason, time.time()))
            self._db.commit()

    def record_feedback(self, email: str, result: str) -> str | None:
        """Log an outcome. Returns the learned pattern template, if this outcome taught one."""
        from . import patterns
        if result not in RESULTS:
            raise ValueError(f"result must be one of {sorted(RESULTS)}")
        email = email.lower()
        learned = None
        with self._lock:
            self._db.execute("INSERT INTO feedback VALUES (?,?,?)", (email, result, time.time()))
            if result in SUPPRESSING:
                self.suppress(email, result)
            if result == "bounced":
                self._db.execute("UPDATE leads SET status='invalid', reason='bounced', score=0 WHERE email=?", (email,))
            if result == "replied":  # a human answered: the strongest possible proof
                self._db.execute("UPDATE leads SET status='valid', reason='replied', score=100 WHERE email=?", (email,))
                r = self._db.execute(
                    "SELECT p.contact_first, p.contact_last FROM leads l JOIN practices p USING(npi) WHERE l.email=?",
                    (email,)).fetchone()
                if r:
                    local, domain = email.split("@")
                    learned = patterns.infer(local, r["contact_first"], r["contact_last"])
                    if learned:
                        self._db.execute(
                            "INSERT INTO domain_patterns VALUES (?,?,1,?) ON CONFLICT(domain) DO UPDATE SET "
                            "template=excluded.template, confirmations=confirmations+1, at=excluded.at", (domain, learned, time.time()))
            self._db.commit()
        return learned

    def learned_template(self, domain: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT template FROM domain_patterns WHERE domain=?", (domain,)).fetchone()
        return r["template"] if r else None

    def stats(self) -> dict:
        with self._lock:
            one = lambda q: self._db.execute(q).fetchone()[0]
            return {
                "practices": one("SELECT COUNT(*) FROM practices"),
                "with_domain": one("SELECT COUNT(*) FROM practices WHERE domain!=''"),
                "domain_confident(>=50)": one("SELECT COUNT(*) FROM practices WHERE domain!='' AND domain_confidence>=50"),
                "leads": one("SELECT COUNT(*) FROM leads"),
                "leads_valid": one("SELECT COUNT(*) FROM leads WHERE status='valid'"),
                "leads_risky": one("SELECT COUNT(*) FROM leads WHERE status='risky'"),
                "leads_unknown": one("SELECT COUNT(*) FROM leads WHERE status='unknown'"),
                "suppressed": one("SELECT COUNT(*) FROM suppressed"),
                "learned_domain_patterns": one("SELECT COUNT(*) FROM domain_patterns"),
            }
