#!/usr/bin/env python3
"""
server.py — tiny stdlib HTTP server wrapping libgen_dl for a browser UI.

Run:
    python3 server.py               # listens on http://127.0.0.1:8765
    python3 server.py --port 9000 --host 0.0.0.0

Endpoints:
    GET /                           → index.html
    GET /api/search?q=QUERY&ext=pdf → JSON list of results
    GET /api/download?md5=MD5&name=FILE.pdf → streams the file with Content-Disposition
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import libgen_dl  # noqa: E402

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "BooksUI/1.0"

    # Quiet the default noisy per-request logging.
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def do_GET(self) -> None:
        try:
            url = urlparse(self.path)
            if url.path == "/" or url.path == "/index.html":
                return self._serve_index()
            if url.path == "/api/search":
                return self._api_search(parse_qs(url.query))
            if url.path == "/api/download":
                return self._api_download(parse_qs(url.query))
            self._send_json(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self._send_json(500, {"error": str(e)})
            except Exception:
                pass

    # --- routes -----------------------------------------------------------

    def _serve_index(self) -> None:
        if not INDEX.exists():
            self._send_json(500, {"error": "index.html missing"})
            return
        body = INDEX.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _api_search(self, qs: dict[str, list[str]]) -> None:
        q = (qs.get("q", [""])[0] or "").strip()
        ext = (qs.get("ext", [""])[0] or "").strip().lower()
        try:
            limit = max(1, min(50, int(qs.get("n", ["20"])[0])))
        except ValueError:
            limit = 20
        if not q:
            return self._send_json(400, {"error": "missing q"})

        rows: list[dict] = []
        used_mirror: str | None = None
        last_err: str | None = None
        # Once a mirror responds successfully (even with zero rows), treat it as
        # authoritative and stop. Only fall through on actual network/parse errors.
        for mirror, schema in libgen_dl.DEFAULT_MIRRORS:
            try:
                rows = libgen_dl.search(q, mirror, schema, limit * 3)
                used_mirror = mirror
                break
            except Exception as e:
                last_err = f"{mirror}: {e}"

        if ext:
            rows = [r for r in rows if r["ext"] == ext]
        rows = rows[:limit]

        self._send_json(200, {"mirror": used_mirror, "error": last_err if used_mirror is None else None, "results": rows})

    def _api_download(self, qs: dict[str, list[str]]) -> None:
        md5 = (qs.get("md5", [""])[0] or "").strip().lower()
        name = (qs.get("name", [""])[0] or "").strip()
        if not md5 or len(md5) != 32 or any(c not in "0123456789abcdef" for c in md5):
            return self._send_json(400, {"error": "bad md5"})

        candidates = libgen_dl.resolve_downloads(md5)
        if not candidates:
            return self._send_json(502, {"error": "could not resolve download link"})

        last_err: str | None = None
        for i, (url, referer) in enumerate(candidates):
            headers = dict(libgen_dl.HEADERS)
            headers["Referer"] = referer
            headers["Accept"] = "*/*"
            try:
                r = requests.get(url, headers=headers, stream=True, timeout=120,
                                 allow_redirects=True, verify=libgen_dl.VERIFY_TLS)
                if r.status_code >= 400:
                    last_err = f"{url} → HTTP {r.status_code}"
                    r.close()
                    continue
            except requests.RequestException as e:
                last_err = f"{url} → {e}"
                continue

            try:
                content_type = r.headers.get("Content-Type", "application/octet-stream")
                # Cheap guard: if upstream accidentally returned an HTML error page,
                # fall through to the next candidate instead of streaming junk.
                if "text/html" in content_type.lower():
                    r.close()
                    last_err = f"{url} → returned HTML, not a file"
                    continue

                total = r.headers.get("Content-Length")
                fname = name or libgen_dl._filename_from_response(r, f"{md5}.bin")
                fname = fname.replace('"', "'")

                # Pull the first chunk BEFORE sending headers. If the upstream
                # connection is dead on arrival, this raises here while we can
                # still fall through to the next candidate / emit a clean 502.
                stream = r.iter_content(chunk_size=1 << 15)
                try:
                    first = next((c for c in stream if c), b"")
                except requests.RequestException as e:
                    last_err = f"{url} → stream failed before any data: {e}"
                    r.close()
                    continue

                self.send_response(200)
                self.send_header("Content-Type", content_type)
                if total:
                    self.send_header("Content-Length", total)
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

                # Headers are committed now — we can no longer change the status
                # code or switch candidates. _stream_to_client absorbs upstream
                # breaks (resuming via Range when possible) and client
                # disconnects, so nothing bubbles up to the do_GET handler.
                self._stream_to_client(stream, first, url, headers, total)
                return
            finally:
                r.close()

        self._send_json(502, {"error": f"all mirrors failed; last: {last_err}"})

    def _stream_to_client(self, stream, first: bytes, url: str,
                          base_headers: dict, total: str | None) -> None:
        """Pump `first` + `stream` to the client, resuming from upstream breaks.

        Headers are already sent by the caller, so we can't change status or
        switch mirrors. On a mid-transfer ChunkedEncodingError/IncompleteRead we
        re-request the same url with a Range header starting at the byte count
        already delivered, and keep going — but only if the upstream honours the
        range (HTTP 206) and we know the expected total. BrokenPipeError means
        the client hung up; we just stop.
        """
        total_int = int(total) if total and total.isdigit() else None
        MAX_RESUMES = 5
        written = 0
        pending = first
        resume = None  # extra response opened for a Range continuation
        attempt = 0
        try:
            while True:
                try:
                    if pending:
                        self.wfile.write(pending)
                        written += len(pending)
                        pending = b""
                    for chunk in stream:
                        if not chunk:
                            continue
                        self.wfile.write(chunk)
                        written += len(chunk)
                    return  # upstream finished cleanly
                except BrokenPipeError:
                    return  # client disconnected
                except requests.RequestException as e:
                    if total_int is None or written >= total_int or attempt >= MAX_RESUMES:
                        sys.stderr.write(
                            f"[download] stream broke at {written}"
                            f"{'/' + str(total_int) if total_int else ''} bytes; "
                            f"giving up after {attempt} resume(s): {e}\n")
                        return
                    attempt += 1
                    if resume is not None:
                        resume.close()
                    rheaders = dict(base_headers)
                    rheaders["Range"] = f"bytes={written}-"
                    try:
                        resume = requests.get(url, headers=rheaders, stream=True,
                                              timeout=120, allow_redirects=True,
                                              verify=libgen_dl.VERIFY_TLS)
                    except requests.RequestException as e2:
                        sys.stderr.write(f"[download] resume request failed: {e2}\n")
                        return
                    if resume.status_code != 206:
                        sys.stderr.write(
                            f"[download] upstream won't resume (HTTP {resume.status_code}); "
                            f"truncated at {written}/{total_int} bytes\n")
                        return
                    sys.stderr.write(
                        f"[download] resuming from byte {written} (attempt {attempt})\n")
                    stream = resume.iter_content(chunk_size=1 << 15)
        finally:
            if resume is not None:
                resume.close()

    # --- helpers ----------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"books UI → http://{args.host}:{args.port}/", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
