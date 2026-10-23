"""One-step NPPES acquisition: download the CMS monthly full file, pull out the npidata CSV."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Callable, Iterator

BASE = "https://download.cms.gov/nppes/"
MEMBER_RE = re.compile(r"(^|/)npidata_pfile_\d+-\d+\.csv$", re.I)  # excludes *_FileHeader.csv


def monthly_urls(today: dt.date | None = None, months: int = 3) -> Iterator[str]:
    """Newest first. CMS posts the monthly file near the start of the month, so also try prior months."""
    d = (today or dt.date.today()).replace(day=1)
    for _ in range(months):
        # CMS has published both plain and _V2 names; the _V2 name is what the live page links to.
        for suffix in ("_V2", ""):
            yield f"{BASE}NPPES_Data_Dissemination_{d.strftime('%B')}_{d.year}{suffix}.zip"
        d = (d - dt.timedelta(days=1)).replace(day=1)


INDEX_URL = BASE + "NPI_Files.html"
_ANY_ZIP = re.compile(r'href=["\']([^"\']*\.zip)["\']', re.I)
_ZIP_HREF = re.compile(r'href=["\']([^"\']*NPPES_Data_Dissemination[^"\']*\.zip)["\']', re.I)
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]


def index_urls(html: str) -> list[str]:
    """Full-replacement monthly zips linked from CMS's download page, newest first.

    Reading the page (instead of guessing a file name) survives CMS renaming the file, e.g. adding a _V2 suffix.
    Weekly increments and deactivation lists are excluded.
    """
    def key(url: str) -> tuple[int, int]:
        m = re.search(r"_([A-Za-z]+)_(\d{4})", url)
        return (int(m.group(2)), _MONTHS.index(m.group(1).lower()) if m and m.group(1).lower() in _MONTHS else -1) if m else (0, -1)

    urls = []
    for href in _ZIP_HREF.findall(html):
        if re.search(r"weekly|deactiv", href, re.I):
            continue
        urls.append(urllib.parse.urljoin(INDEX_URL, href))
    return sorted(dict.fromkeys(urls), key=key, reverse=True)


def _http_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "findmyclient/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def download(url: str, dest: str, progress: Callable[[int, int], None] | None = None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "findmyclient/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
        total, done = int(r.headers.get("Content-Length") or 0), 0
        while chunk := r.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)


@contextlib.contextmanager
def open_npidata(zip_path: str):
    """Text stream of the npidata CSV read directly out of the zip (the CSV is ~10 GB; never written to disk)."""
    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist() if MEMBER_RE.search(n)]
        if not members:
            raise RuntimeError(f"{zip_path}: no npidata_pfile_*.csv inside (found: {z.namelist()[:5]})")
        with z.open(max(members, key=lambda n: z.getinfo(n).file_size)) as raw:
            yield io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")


def fetch_zip(workdir: str, url: str | None = None, today: dt.date | None = None,
              opener: Callable[[str, str, Callable | None], None] = download,
              index: Callable[[str], str] = _http_text) -> str:
    """Download the newest available monthly zip; returns its path."""
    os.makedirs(workdir, exist_ok=True)
    zip_path = os.path.join(workdir, "nppes.zip")

    def bar(done, total):
        if total:
            print(f"\r  downloading {done >> 20}/{total >> 20} MB", end="", file=sys.stderr, flush=True)

    tried = []
    candidates = [url] if url else []
    if not url:
        try:
            html = index(INDEX_URL)
            candidates = index_urls(html)[:3]
            if not candidates:
                seen = _ANY_ZIP.findall(html)
                tried.append(f"{INDEX_URL}: loaded ({len(html)} bytes) but no monthly NPPES_Data_Dissemination zip link matched; "
                             f"zip links on page: {seen[:8] or 'none (page may be script-rendered)'}")
        except (urllib.error.URLError, OSError) as e:
            tried.append(f"{INDEX_URL}: {e}")
        candidates += [u for u in monthly_urls(today) if u not in candidates]  # last resort: guessed names
    for u in candidates:
        try:
            opener(u, zip_path, bar)
            print(file=sys.stderr)
            return zip_path
        except (urllib.error.URLError, OSError) as e:
            tried.append(f"{u}: {e}")
    raise RuntimeError("could not download NPPES file:\n  " + "\n  ".join(tried) +
                       "\nCheck network access to download.cms.gov, or pass --url / use `import-nppes` on a file you have.")
