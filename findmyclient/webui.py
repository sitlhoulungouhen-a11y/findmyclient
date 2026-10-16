"""Local dashboard: browse/filter leads, export CSV, record outcomes. Binds to localhost only."""
from __future__ import annotations

import csv
import hmac
import io
import secrets
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .store import RESULTS, Store

COLUMNS = ["practice", "specialty", "city", "state", "phone", "domain", "contact", "title", "email", "source",
           "status", "score", "reason", "role", "domain_confidence"]

CSS = """body{font:14px system-ui;margin:20px;background:#fafafa;color:#222}table{border-collapse:collapse;width:100%}
th,td{padding:4px 8px;border-bottom:1px solid #ddd;text-align:left;white-space:nowrap}th{background:#eee;position:sticky;top:0}
.valid{color:#0a7d2c}.risky{color:#b36b00}.unknown{color:#666}.invalid{color:#b00020}
form.f{margin-bottom:12px}input,select,button{font:inherit;padding:3px 6px}.stats span{margin-right:16px}"""


def lead_dicts(store: Store, state: str, min_score: int, status: str, limit: int) -> list[dict]:
    return [{
        "practice": r["name"], "specialty": r["specialty"], "city": r["city"], "state": r["state"], "phone": r["phone"],
        "domain": r["domain"], "contact": f'{r["contact_first"]} {r["contact_last"]}'.strip(), "title": r["contact_title"],
        "email": r["email"], "source": r["source"], "status": r["status"], "score": r["score"], "reason": r["reason"],
        "role": bool(r["role"]), "domain_confidence": r["domain_confidence"],
    } for r in store.lead_rows(state=state, min_score=min_score, status=status, limit=limit)]


def make_handler(store: Store, token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:  # blocks DNS-rebinding from other sites to this local server
            if self.headers.get("Host") in allowed_hosts:
                return True
            self._send(403, b"bad host", "text/plain")
            return False

        def do_GET(self):
            if not self._host_ok():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            state, status = q.get("state", ""), q.get("status", "")
            min_score = int(q["min_score"]) if q.get("min_score", "").isdigit() else 0
            if u.path == "/export.csv":
                rows = lead_dicts(store, state, min_score, status, 1_000_000)
                buf = io.StringIO()
                w = csv.DictWriter(buf, COLUMNS); w.writeheader(); w.writerows(rows)
                return self._send(200, buf.getvalue().encode(), "text/csv", [("Content-Disposition", "attachment; filename=leads.csv")])
            if u.path != "/":
                return self._send(404, b"not found", "text/plain")
            rows = lead_dicts(store, state, min_score, status, 500)
            stats = "".join(f"<span><b>{escape(str(v))}</b> {escape(k)}</span>" for k, v in store.stats().items())
            opts = "".join(f'<option{" selected" if status == s else ""}>{s}</option>' for s in ["", "valid", "risky", "unknown", "invalid"])
            head = "".join(f"<th>{c}</th>" for c in COLUMNS) + "<th>outcome</th>"
            body = []
            for r in rows:
                cells = "".join(f'<td class="{escape(str(r[c]))}">{escape(str(r[c]))}</td>' if c == "status" else f"<td>{escape(str(r[c]))}</td>" for c in COLUMNS)
                res = "".join(f'<option>{x}</option>' for x in sorted(RESULTS))
                form = (f'<td><form method="post" action="/feedback"><input type="hidden" name="token" value="{token}">'
                        f'<input type="hidden" name="email" value="{escape(r["email"], quote=True)}">'
                        f'<select name="result">{res}</select><button>save</button></form></td>')
                body.append(f"<tr>{cells}{form}</tr>")
            page = (f"<!doctype html><meta charset=utf-8><title>findmyclient</title><style>{CSS}</style>"
                    f'<div class=stats>{stats}</div><form class=f>state <input name=state size=2 value="{escape(state, quote=True)}"> '
                    f'min score <input name=min_score size=3 value="{min_score}"> status <select name=status>{opts}</select> '
                    f'<button>filter</button> <a href="/export.csv?state={escape(state, quote=True)}&min_score={min_score}&status={escape(status, quote=True)}">export CSV</a></form>'
                    f"<table><tr>{head}</tr>{''.join(body)}</table><p>showing up to 500 rows</p>")
            self._send(200, page.encode())

        def do_POST(self):
            if not self._host_ok():
                return
            if urlparse(self.path).path != "/feedback":
                return self._send(404, b"not found", "text/plain")
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()).items()}
            if not hmac.compare_digest(form.get("token", ""), token):
                return self._send(403, b"bad token", "text/plain")
            try:
                store.record_feedback(form.get("email", ""), form.get("result", ""))
            except ValueError as e:
                return self._send(400, str(e).encode(), "text/plain")
            self._send(303, b"", extra=[("Location", self.headers.get("Referer") or "/")])
    return H


def serve(store: Store, port: int = 8765) -> None:
    token = secrets.token_urlsafe(16)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, token, port))
    print(f"findmyclient dashboard: http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
