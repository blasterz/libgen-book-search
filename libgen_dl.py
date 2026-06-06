#!/usr/bin/env python3
"""
libgen_dl.py — search Library Genesis for a book by title and download it.

Usage:
    python3 libgen_dl.py "The Textbook of Clinical Sexual Medicine"
    python3 libgen_dl.py -n 20 -o ./books "quantum computing nielsen"
    python3 libgen_dl.py --ext pdf --auto "melville moby dick"

Options:
    -n N        Max results to show (default 15)
    -o DIR      Output directory (default ./downloads)
    --ext E     Filter by extension (pdf, epub, djvu, mobi, ...)
    --auto      Auto-pick the first result without prompting
    --mirror M  Override base mirror (default: libgen.is)

Depends only on stdlib + requests. HTML is parsed with regex since bs4 is
not installed in this environment.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import urllib3

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

# libgen download mirrors routinely serve self-signed / mismatched TLS certs
# (e.g. library.lol), so we skip cert verification for resolver + file fetches.
# This trusts whatever cert the mirror presents — acceptable for libgen scraping,
# but do not reuse this session for anything sensitive.
VERIFY_TLS = False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Public libgen search mirrors. Each entry says which search-page schema
# to use when scraping the results.
DEFAULT_MIRRORS: list[tuple[str, str]] = [
    ("https://libgen.li", "li"),
    ("https://libgen.is", "is"),
    ("https://libgen.rs", "is"),
    ("https://libgen.st", "is"),
]

# Download resolver mirrors — these take an MD5 and return pages with a
# direct file link. Two fork families: the libgen.li/.gs/.la group serves
# get.php links via ads.php; the library.lol / libgen.is/.rs/.st group serves
# a "GET" anchor. resolve_downloads() understands both, and tries every entry
# so a single timeout or dead host doesn't sink the request.
DL_RESOLVERS = [
    "https://libgen.li/ads.php?md5={md5}",
    "https://libgen.gs/ads.php?md5={md5}",
    "https://libgen.la/ads.php?md5={md5}",
    "https://library.lol/main/{md5}",
    "https://libgen.is/ads.php?md5={md5}",
    "https://libgen.rs/ads.php?md5={md5}",
]


def search(query: str, mirror: str, schema: str, limit: int) -> list[dict]:
    """Scrape a libgen search page. Dispatches on mirror schema."""
    if schema == "li":
        return _search_li(query, mirror, limit)
    return _search_is(query, mirror, limit)


def _search_is(query: str, mirror: str, limit: int) -> list[dict]:
    """libgen.is / .rs / .st — table.c, columns ID|Author|Title|Pub|Year|Pages|Lang|Size|Ext|Mirrors."""
    url = f"{mirror}/search.php"
    params = {"req": query, "res": max(25, limit), "view": "simple", "column": "def"}
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    body = r.text

    m = re.search(r'<table[^>]*class=["\']c["\'][^>]*>(.*?)</table>', body, re.S | re.I)
    if not m:
        return []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S | re.I)
    out: list[dict] = []
    for row in rows[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        if len(cells) < 9:
            continue
        md5_m = re.search(r"md5=([A-Fa-f0-9]{32})", cells[2])
        if not md5_m:
            continue
        out.append({
            "md5": md5_m.group(1).lower(),
            "author": _strip_tags(cells[1]),
            "title": _strip_tags(cells[2]),
            "publisher": _strip_tags(cells[3]),
            "year": _strip_tags(cells[4]),
            "pages": _strip_tags(cells[5]),
            "lang": _strip_tags(cells[6]),
            "size": _strip_tags(cells[7]),
            "ext": _strip_tags(cells[8]).lower(),
        })
        if len(out) >= limit:
            break
    return out


def _search_li(query: str, mirror: str, limit: int) -> list[dict]:
    """libgen.li — table#tablelibgen, columns Title|Author|Publisher|Year|Lang|Pages|Size|Ext|Mirrors."""
    url = f"{mirror}/index.php"
    params = [
        ("req", query),
        ("columns[]", "t"),
        ("columns[]", "a"),
        ("columns[]", "s"),
        ("columns[]", "y"),
        ("columns[]", "p"),
        ("columns[]", "i"),
        ("objects[]", "f"),
        ("topics[]", "l"),
        ("res", max(25, limit)),
    ]
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    body = r.text

    m = re.search(r'<table[^>]*id=["\']tablelibgen["\'][^>]*>(.*?)</table>', body, re.S | re.I)
    if not m:
        return []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S | re.I)
    out: list[dict] = []
    for row in rows[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        if len(cells) < 9:
            continue
        md5_m = re.search(r"md5=([A-Fa-f0-9]{32})", cells[8])
        if not md5_m:
            continue
        # The title cell can contain: <b>series</b> plus one or more
        # <a href="edition.php?id=..."> anchors. The first anchor is often
        # just a tooltip wrapper with empty visible text; the real title
        # sits in a later anchor. Collect all candidate texts and pick the
        # longest non-empty one, prefixed with the bold series if present.
        series = ""
        bm = re.search(r"<b[^>]*>(.*?)</b>", cells[0], re.S | re.I)
        if bm:
            # Keep only the text directly inside <b>, excluding nested anchors.
            bt = re.sub(r"<a[^>]*>.*?</a>", "", bm.group(1), flags=re.S | re.I)
            series = _strip_tags(bt)
        candidates = [_strip_tags(m.group(1))
                      for m in re.finditer(r'href="edition\.php\?id=\d+"[^>]*>(.*?)</a>',
                                           cells[0], re.S | re.I)]
        candidates = [c for c in candidates if c]
        body = max(candidates, key=len) if candidates else _strip_tags(cells[0])
        title = f"{series} — {body}" if series and series not in body else body
        out.append({
            "md5": md5_m.group(1).lower(),
            "title": title,
            "author": _strip_tags(cells[1]),
            "publisher": _strip_tags(cells[2]),
            "year": _strip_tags(cells[3]),
            "lang": _strip_tags(cells[4]),
            "pages": _strip_tags(cells[5]),
            "size": _strip_tags(cells[6]),
            "ext": _strip_tags(cells[7]).lower(),
        })
        if len(out) >= limit:
            break
    return out


def _strip_tags(s: str) -> str:
    # Kill attribute *values* first — libgen.li crams raw '<' and '>' into
    # title="..." tooltips, which confuses naive tag regex.
    s = re.sub(r'="(?:[^"\\]|\\.)*"', "", s)
    s = re.sub(r"='(?:[^'\\]|\\.)*'", "", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def resolve_downloads(md5: str) -> list[tuple[str, str]]:
    """Return a list of (direct_url, referer) candidates from every resolver."""
    candidates: list[tuple[str, str]] = []
    for tmpl in DL_RESOLVERS:
        url = tmpl.format(md5=md5)
        try:
            r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True, verify=VERIFY_TLS)
            r.raise_for_status()
        except Exception as e:
            print(f"[resolver] {url} -> {e}", file=sys.stderr)
            continue
        referer = r.url  # page we scraped the link from — some CDNs demand it

        # library.lol — <a href="https://..."><h2>GET</h2></a>
        for m in re.finditer(r'href=["\'](https?://[^"\']+)["\'][^>]*>\s*<h2[^>]*>\s*GET\s*</h2>', r.text, re.I):
            candidates.append((html.unescape(m.group(1)), referer))
        # libgen.li — <a href="get.php?md5=...&key=...">
        for m in re.finditer(r'href=["\'](get\.php\?[^"\']+)["\']', r.text, re.I):
            candidates.append((urljoin(r.url, html.unescape(m.group(1))), referer))
        # Generic: any direct link to a binary.
        for m in re.finditer(r'href=["\'](https?://[^"\']+\.(?:pdf|epub|djvu|mobi|azw3|zip))["\']', r.text, re.I):
            candidates.append((html.unescape(m.group(1)), referer))

    # dedupe while preserving order
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for url, ref in candidates:
        if url in seen:
            continue
        seen.add(url)
        out.append((url, ref))
    return out


def resolve_download(md5: str) -> str | None:
    """Back-compat single-URL resolver (used by the CLI)."""
    cands = resolve_downloads(md5)
    return cands[0][0] if cands else None


def download(url: str, out_dir: Path, default_name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with requests.get(url, headers=HEADERS, stream=True, timeout=120, verify=VERIFY_TLS) as r:
        r.raise_for_status()
        fname = _filename_from_response(r, default_name)
        dest = out_dir / fname
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        t0 = time.time()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 15):
                if not chunk:
                    continue
                fh.write(chunk)
                got += len(chunk)
                _progress(got, total, t0)
        sys.stderr.write("\n")
        return dest


def _filename_from_response(r: requests.Response, fallback: str) -> str:
    cd = r.headers.get("Content-Disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        name = m.group(1).strip()
    else:
        name = os.path.basename(urlparse(r.url).path) or fallback
    # strip any unsafe path chars
    name = re.sub(r"[\\/]+", "_", name)
    return name or fallback


def _progress(got: int, total: int, t0: float) -> None:
    dt = max(time.time() - t0, 0.001)
    speed = got / dt / 1024
    if total:
        pct = got * 100 / total
        sys.stderr.write(f"\r  {got/1e6:6.2f} / {total/1e6:6.2f} MB  ({pct:5.1f}%)  {speed:7.0f} KB/s")
    else:
        sys.stderr.write(f"\r  {got/1e6:6.2f} MB  {speed:7.0f} KB/s")
    sys.stderr.flush()


def pick(rows: list[dict], auto: bool) -> dict | None:
    if not rows:
        return None
    for i, b in enumerate(rows, 1):
        print(f"[{i:2}] {b['title']!r}")
        print(f"     by {b['author']} — {b['publisher']} {b['year']}   "
              f"{b['ext']}  {b['size']}  {b['lang']}  {b['pages']}p")
    if auto:
        return rows[0]
    try:
        raw = input(f"\nPick 1-{len(rows)} (enter=1, q=quit): ").strip().lower()
    except EOFError:
        return rows[0]
    if raw in ("q", "quit", "exit"):
        return None
    if not raw:
        return rows[0]
    if not raw.isdigit() or not (1 <= int(raw) <= len(rows)):
        print("bad choice")
        return None
    return rows[int(raw) - 1]


def main() -> int:
    ap = argparse.ArgumentParser(description="Search & download books from Library Genesis.")
    ap.add_argument("query", nargs="+", help="Book title / search terms")
    ap.add_argument("-n", "--limit", type=int, default=15, help="Max results to show")
    ap.add_argument("-o", "--out", default="./downloads", help="Output directory")
    ap.add_argument("--ext", help="Only show this extension (pdf/epub/djvu/...)")
    ap.add_argument("--auto", action="store_true", help="Auto-pick the first (best) match")
    ap.add_argument("--mirror", help="Override base mirror, e.g. https://libgen.rs")
    args = ap.parse_args()

    query = " ".join(args.query)
    mirrors = [(args.mirror, "li" if "libgen.li" in args.mirror else "is")] if args.mirror else DEFAULT_MIRRORS

    rows: list[dict] = []
    used_mirror: str | None = None
    for m, schema in mirrors:
        try:
            print(f"searching {m} for {query!r} ...", file=sys.stderr)
            rows = search(query, m, schema, args.limit * 3)  # over-fetch for ext filter
            if rows:
                used_mirror = m
                break
        except Exception as e:
            print(f"  {m} failed: {e}", file=sys.stderr)
    if not rows:
        print("no results (or all mirrors unreachable)", file=sys.stderr)
        return 1

    if args.ext:
        rows = [r for r in rows if r["ext"] == args.ext.lower()]
        if not rows:
            print(f"no results with extension .{args.ext}", file=sys.stderr)
            return 1

    rows = rows[: args.limit]
    print(f"\n{len(rows)} results from {used_mirror}:\n")
    choice = pick(rows, auto=args.auto)
    if not choice:
        return 0

    print(f"\nresolving download for md5={choice['md5']} ...", file=sys.stderr)
    url = resolve_download(choice["md5"])
    if not url:
        print("could not resolve a direct download link", file=sys.stderr)
        return 2
    print(f"downloading {url}", file=sys.stderr)

    safe = re.sub(r"[^\w.\- ]+", "_", f"{choice['title']}.{choice['ext']}").strip()[:180]
    dest = download(url, Path(args.out), default_name=safe)
    print(f"\nsaved -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
