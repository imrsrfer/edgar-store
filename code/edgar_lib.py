"""Shared plumbing for the EDGAR pipeline: paths, polite HTTP, and manifest I/O.

Everything that touches sec.gov goes through :func:`sec_session` and
:class:`RateLimiter` so the request rules are enforced in exactly one place.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# SEC requires a User-Agent naming a real contact. Requests without it get 403.
USER_AGENT = "Fer imrsrfer@gmail.com"

# SEC's published ceiling is 10 requests/second. We stay just under it.
MAX_REQUESTS_PER_SECOND = 9.0
REQUEST_SPACING_SECONDS = 0.15

# Retry policy for transient throttling / outages.
RETRY_STATUSES = (429, 502, 503, 504)
MAX_RETRIES = 6
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0

# Streamed download chunk. Large enough to keep syscall overhead low.
CHUNK_BYTES = 1 << 20

PROGRESS_INTERVAL_SECONDS = 5

DEFAULT_ROOT = Path(r"C:\Users\Fer\claude\Projects\Portfolio\edgar")


class Paths:
    """Every path the pipeline writes, derived from a single root."""

    def __init__(self, root=None):
        self.root = Path(root) if root else DEFAULT_ROOT
        self.raw = self.root / "raw"

    def ensure(self):
        for directory in (self.root, self.raw):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def companyfacts_zip(self):
        return self.raw / "companyfacts.zip"

    @property
    def tickers_json(self):
        return self.raw / "company_tickers.json"

    @property
    def tickers_exchange_json(self):
        return self.raw / "company_tickers_exchange.json"

    @property
    def manifest(self):
        return self.raw / "manifest.json"

    @property
    def facts(self):
        return self.root / "facts.parquet"

    @property
    def meta(self):
        return self.root / "meta.parquet"

    @property
    def gate0(self):
        return self.root / "gate0.csv"

    @property
    def data_quality(self):
        return self.root / "data_quality.csv"

    @property
    def inactive_filers(self):
        return self.root / "inactive_filers.csv"

    @property
    def invalid_fiscal_years(self):
        return self.root / "invalid_fiscal_years.csv"

    @property
    def duplicate_filers(self):
        return self.root / "duplicate_filers.csv"

    @property
    def company_meta_jsonl(self):
        """Append-only cache of per-CIK submissions metadata (sic, fye, exchange).

        One consolidated file rather than ~12k tiny ones: far kinder to Windows,
        and resuming is just "which CIKs are already on disk".
        """
        return self.raw / "company_meta.jsonl"


class RateLimiter:
    """Thread-safe request spacing so concurrent workers still respect the cap."""

    def __init__(self, per_second=MAX_REQUESTS_PER_SECOND):
        self._min_interval = 1.0 / float(per_second)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._min_interval
        if wait:
            time.sleep(wait)


def sec_session():
    """A requests Session carrying the mandatory SEC headers."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/plain, */*",
        }
    )
    return session


def get_with_retry(session, url, limiter=None, extra_headers=None, stream=False,
                   timeout=120):
    """GET with rate limiting and exponential backoff on transient failures.

    Returns the response, or re-raises the last error once retries are exhausted.
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        if limiter is not None:
            limiter.acquire()
        else:
            time.sleep(REQUEST_SPACING_SECONDS)
        try:
            response = session.get(
                url, headers=extra_headers, stream=stream, timeout=timeout
            )
        except requests.RequestException as error:
            last_error = error
        else:
            if response.status_code not in RETRY_STATUSES:
                return response
            last_error = requests.HTTPError(
                f"HTTP {response.status_code} for {url}", response=response
            )
            response.close()
        time.sleep(min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_CAP_SECONDS))
    raise last_error


def download_stream(session, url, destination, limiter=None, force=False):
    """Download ``url`` to ``destination``, resuming a partial ``.part`` file.

    Idempotent: an already-complete destination is left alone unless ``force``.
    Returns a dict describing what happened, suitable for the manifest.
    """
    destination = Path(destination)
    partial = destination.with_name(destination.name + ".part")

    if destination.exists() and not force:
        return {
            "path": destination.name,
            "bytes": destination.stat().st_size,
            "action": "cached",
        }
    if force and partial.exists():
        partial.unlink()

    already = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={already}-"} if already else None

    response = get_with_retry(
        session, url, limiter=limiter, extra_headers=headers, stream=True
    )
    # Byte offsets only line up when the body is not transfer-compressed. For a
    # gzipped response we write decompressed bytes, so a Range resume would
    # corrupt the file and Content-Length describes the compressed stream.
    # Restart such downloads from zero and skip the size assertion.
    if _is_encoded(response) or (already and response.status_code != 206):
        already = 0
        if partial.exists():
            partial.unlink()
    response.raise_for_status()

    total = _expected_total(response, already)
    written = _stream_to_file(response, partial, already, destination.name, total)
    response.close()

    if total and written != total:
        raise IOError(
            f"{destination.name}: got {written} bytes, expected {total}. "
            "Re-run to resume from where it stopped."
        )
    os.replace(partial, destination)
    _progress(destination.name, written, total, final=True)
    return {
        "path": destination.name,
        "bytes": written,
        "action": "resumed" if already else "downloaded",
        "last_modified": response.headers.get("Last-Modified"),
    }


def _stream_to_file(response, partial, already, label, total):
    """Append the response body to the partial file, reporting progress."""
    written = already
    last_report = time.monotonic()
    with open(partial, "ab" if already else "wb") as handle:
        for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
            if not chunk:
                continue
            handle.write(chunk)
            written += len(chunk)
            if time.monotonic() - last_report > PROGRESS_INTERVAL_SECONDS:
                _progress(label, written, total)
                last_report = time.monotonic()
    return written


def _is_encoded(response):
    """True when the body arrives transfer-compressed, so byte counts are not ours."""
    encoding = (response.headers.get("Content-Encoding") or "identity").lower()
    return encoding not in ("identity", "")


def _expected_total(response, already):
    """Complete file size in the bytes we will write, or None if unknowable.

    Returns None for a compressed body: Content-Length then describes the
    compressed stream, which is not what lands on disk.
    """
    if _is_encoded(response):
        return None
    content_range = response.headers.get("Content-Range")
    if content_range and "/" in content_range:
        tail = content_range.rsplit("/", 1)[1]
        if tail.isdigit():
            return int(tail)
    length = response.headers.get("Content-Length")
    if length and length.isdigit():
        return int(length) + already
    return None


def _progress(name, written, total, final=False):
    if total:
        pct = 100.0 * written / total
        line = f"  {name}: {written / 1e6:,.0f} / {total / 1e6:,.0f} MB ({pct:.1f}%)"
    else:
        line = f"  {name}: {written / 1e6:,.0f} MB"
    sys.stdout.write(line.ljust(72) + ("\n" if final else "\r"))
    sys.stdout.flush()


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_manifest(paths):
    if not paths.manifest.exists():
        return {}
    try:
        return json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def write_manifest(paths, manifest):
    paths.manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


def manifest_download_date(paths):
    """Date the companyfacts bulk file was pulled, so staleness stays visible."""
    for entry in read_manifest(paths).values():
        if entry.get("path") == "companyfacts.zip":
            stamp = entry.get("downloaded_utc")
            if stamp:
                return stamp[:10]
    return None


def log_stage(label, **fields):
    """One-line stage summary, printed at the end of every stage."""
    parts = ", ".join(f"{key}={value}" for key, value in fields.items())
    print(f"[{label}] {parts}")
