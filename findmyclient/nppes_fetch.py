"""One-step NPPES acquisition: download the CMS monthly full file, pull out the npidata CSV."""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from typing import Callable, Iterator

BASE = "https://download.cms.gov/nppes/"
MEMBER_RE = re.compile(r"(^|/)npidata_pfile_\d+-\d+\.csv$", re.I)  # excludes *_FileHeader.csv


def monthly_urls(today: dt.date | None = None, months: int = 3) -> Iterator[str]:
    """Newest first. CMS posts the monthly file near the start of the month, so also try prior months."""
    d = (today or dt.date.today()).replace(day=1)
    for _ in range(months):
        yield f"{BASE}NPPES_Data_Dissemination_{d.strftime('%B')}_{d.year}.zip"
        d = (d - dt.timedelta(days=1)).replace(day=1)


def download(url: str, dest: str, progress: Callable[[int, int], None] | None = None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "findmyclient/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
        total, done = int(r.headers.get("Content-Length") or 0), 0
        while chunk := r.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)


def extract_npidata(zip_path: str, out_dir: str) -> str:
    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist() if MEMBER_RE.search(n)]
        if not members:
            raise RuntimeError(f"{zip_path}: no npidata_pfile_*.csv inside (found: {z.namelist()[:5]})")
        member = max(members, key=lambda n: z.getinfo(n).file_size)
        dest = os.path.join(out_dir, os.path.basename(member))
        with z.open(member) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    return dest


def fetch(workdir: str, url: str | None = None, today: dt.date | None = None,
          opener: Callable[[str, str, Callable | None], None] = download) -> str:
    """Download + extract; returns path of the npidata CSV. Removes the zip afterwards."""
    os.makedirs(workdir, exist_ok=True)
    zip_path = os.path.join(workdir, "nppes.zip")

    def bar(done, total):
        if total:
            print(f"\r  downloading {done >> 20}/{total >> 20} MB", end="", file=sys.stderr, flush=True)

    tried = []
    for u in ([url] if url else list(monthly_urls(today))):
        try:
            opener(u, zip_path, bar)
            print(file=sys.stderr)
            break
        except (urllib.error.URLError, OSError) as e:
            tried.append(f"{u}: {e}")
    else:
        raise RuntimeError("could not download NPPES file:\n  " + "\n  ".join(tried) +
                           "\nCheck network access to download.cms.gov, or pass --url / use `import-nppes` on a file you have.")
    try:
        return extract_npidata(zip_path, workdir)
    finally:
        os.remove(zip_path)
