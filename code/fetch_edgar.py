"""Stage 1: download and cache raw SEC data. Idempotent, resumable, polite.

    python fetch_edgar.py                 # full fetch, skipping anything cached
    python fetch_edgar.py --force         # re-download everything
    python fetch_edgar.py --skip-submissions

Three artifacts land in ``raw/``:

* ``companyfacts.zip``      -- bulk XBRL facts for every filer (~1.4 GB)
* ``company_tickers*.json`` -- ticker/exchange maps (the bulk data has no tickers)
* ``company_meta.jsonl``    -- per-CIK sic / fiscal-year-end / exchange

The last one exists because SEC's bulk ``submissions.zip`` is not served (it
returns 403), and neither ``companyfacts.zip`` nor the ticker maps carry SIC
codes -- which the default ``--exclude-sic 6000-6799`` screen needs. So we pull
it per-CIK from data.sec.gov, restricted to the ticker universe, cached
append-only so it is paid for once.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from edgar_lib import (
    Paths,
    RateLimiter,
    download_stream,
    get_with_retry,
    log_stage,
    read_manifest,
    sec_session,
    utc_now_iso,
    write_manifest,
)

COMPANYFACTS_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
)
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Concurrency for the per-CIK metadata pull. The shared RateLimiter, not this
# number, enforces the request ceiling; workers only hide network latency.
SUBMISSION_WORKERS = 8

# Progress is reported and the sink flushed every this many companies.
PROGRESS_EVERY = 100

# Fields kept from each submissions document. The rest (filings.recent, former
# names, addresses) is megabytes per company that this pipeline never reads.
SUBMISSION_FIELDS = (
    ("name", "name"),
    ("sic", "sic"),
    ("sicDescription", "sic_description"),
    ("fiscalYearEnd", "fiscal_year_end"),
    ("entityType", "entity_type"),
)


def download_bulk(paths, session, limiter, force):
    """Fetch the three whole-file artifacts, resuming partial downloads."""
    manifest = read_manifest(paths)
    targets = (
        (COMPANYFACTS_URL, paths.companyfacts_zip),
        (TICKERS_URL, paths.tickers_json),
        (TICKERS_EXCHANGE_URL, paths.tickers_exchange_json),
    )
    actions = {}
    for url, destination in targets:
        print(f"  fetching {destination.name} ...")
        result = download_stream(
            session, url, destination, limiter=limiter, force=force
        )
        if result["action"] == "cached":
            previous = manifest.get(url, {})
            result["downloaded_utc"] = previous.get("downloaded_utc", utc_now_iso())
            result["last_modified"] = previous.get("last_modified")
        else:
            result["downloaded_utc"] = utc_now_iso()
        manifest[url] = result
        actions[destination.name] = result["action"]
    write_manifest(paths, manifest)
    return actions


def load_ticker_universe(paths):
    """CIK -> {ticker, title, exchange} from the two SEC ticker maps.

    A CIK can carry several tickers (share classes). We keep the first one seen,
    which is the primary listing in SEC's own ordering.
    """
    universe = {}
    raw = json.loads(paths.tickers_json.read_text(encoding="utf-8"))
    for row in raw.values():
        cik = int(row["cik_str"])
        universe.setdefault(
            cik,
            {"ticker": row["ticker"], "title": row.get("title"), "exchange": None},
        )

    if paths.tickers_exchange_json.exists():
        payload = json.loads(paths.tickers_exchange_json.read_text(encoding="utf-8"))
        index = {name: position for position, name in enumerate(payload.get("fields", []))}
        for row in payload.get("data", []):
            cik = int(row[index["cik"]])
            entry = universe.setdefault(
                cik,
                {
                    "ticker": row[index["ticker"]],
                    "title": row[index["name"]],
                    "exchange": None,
                },
            )
            if entry.get("exchange") is None:
                entry["exchange"] = row[index["exchange"]]
    return universe


def already_fetched(paths):
    """CIKs already present in the append-only metadata cache."""
    if not paths.company_meta_jsonl.exists():
        return set()
    done = set()
    with open(paths.company_meta_jsonl, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(int(json.loads(line)["cik"]))
            except (ValueError, KeyError):
                continue  # truncated final line from an interrupted run
    return done


def _fetch_one(session, limiter, cik):
    """Pull one company's submissions document and trim it to what we use."""
    response = get_with_retry(
        session, SUBMISSIONS_URL.format(cik=cik), limiter=limiter, timeout=60
    )
    if response.status_code != 200:
        return {"cik": cik, "error": f"HTTP {response.status_code}"}
    payload = response.json()
    record = {"cik": cik}
    for source_key, target_key in SUBMISSION_FIELDS:
        record[target_key] = payload.get(source_key)
    record["tickers"] = payload.get("tickers") or []
    record["exchanges"] = payload.get("exchanges") or []
    record["fetched_utc"] = utc_now_iso()
    return record


def fetch_submissions(paths, session, limiter, force, limit=None):
    """Fetch per-CIK metadata for the ticker universe, resuming where we stopped."""
    universe = load_ticker_universe(paths)
    if force and paths.company_meta_jsonl.exists():
        paths.company_meta_jsonl.unlink()

    done = already_fetched(paths)
    pending = sorted(cik for cik in universe if cik not in done)
    if limit:
        pending = pending[:limit]
    if not pending:
        return {"total": len(universe), "cached": len(done), "fetched": 0, "failed": 0}

    print(f"  {len(pending):,} companies to fetch ({len(done):,} already cached)")
    write_lock = threading.Lock()
    counters = {"fetched": 0, "failed": 0}
    started = time.monotonic()

    with open(paths.company_meta_jsonl, "a", encoding="utf-8") as sink:

        def worker(cik):
            try:
                record = _fetch_one(session, limiter, cik)
            except Exception as error:  # network gave up after full backoff
                record = {"cik": cik, "error": f"{type(error).__name__}: {error}"}
            with write_lock:
                sink.write(json.dumps(record) + "\n")
                counters["failed" if record.get("error") else "fetched"] += 1
                _report_progress(counters, len(pending), started, sink)

        with ThreadPoolExecutor(max_workers=SUBMISSION_WORKERS) as pool:
            list(pool.map(worker, pending))

    print()
    return {
        "total": len(universe),
        "cached": len(done),
        "fetched": counters["fetched"],
        "failed": counters["failed"],
    }


def _report_progress(counters, total, started, sink):
    """Progress line plus a periodic flush, so an interrupt loses almost nothing."""
    seen = counters["fetched"] + counters["failed"]
    if seen % PROGRESS_EVERY:
        return
    sink.flush()
    elapsed = time.monotonic() - started
    rate = seen / elapsed if elapsed else 0.0
    remaining_min = ((total - seen) / rate / 60) if rate else 0.0
    sys.stdout.write(
        f"  {seen:,}/{total:,} ({rate:.1f}/s, ~{remaining_min:.1f} min left)".ljust(70)
        + "\r"
    )
    sys.stdout.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="output root directory")
    parser.add_argument(
        "--force", action="store_true", help="re-download even if cached"
    )
    parser.add_argument(
        "--skip-submissions",
        action="store_true",
        help="skip per-CIK sic/fiscal-year-end metadata (leaves SIC null)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="cap companies fetched (testing)"
    )
    args = parser.parse_args(argv)

    paths = Paths(args.root).ensure()
    session = sec_session()
    limiter = RateLimiter()
    started = time.monotonic()

    print(f"EDGAR fetch -> {paths.root}")
    actions = download_bulk(paths, session, limiter, args.force)
    log_stage(
        "fetch:bulk",
        **{name.replace(".", "_"): action for name, action in actions.items()},
    )

    if args.skip_submissions:
        log_stage("fetch:submissions", status="skipped")
    else:
        stats = fetch_submissions(paths, session, limiter, args.force, limit=args.limit)
        log_stage("fetch:submissions", **stats)

    log_stage("fetch", elapsed_min=f"{(time.monotonic() - started) / 60:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
