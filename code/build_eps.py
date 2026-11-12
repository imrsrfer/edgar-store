#!/usr/bin/env python3
"""Forward and trailing P/E for the lane survivors -> <root>/eps.csv.

    python build_eps.py                 # every lane's quality-stage survivors
    python build_eps.py --tickers AMPH,LQDT
    python build_eps.py --dry-run

Added 2026-11-12 (audit 2026-11-11). screen.py has carried an adjusted-EPS
guard since --eps-csv existed, and no build ever supplied the file: the guard
was null on all 418 survivors of the 2026-11-07 store. This writes the file.

    python screen.py --lane value ... --eps-csv eps.csv

🔴 WHAT THE GUARD IS. fwd_pe / trail_pe below 0.60 sets adjusted_eps_guard.
A low ratio means the forward (analyst, usually ADJUSTED) earnings sit far
above the trailing ones. That is either an inflated forward estimate or a
depressed trailing year -- opposite readings of one number -- so the guard
FLAGS and never rejects, and it does not clear the adjusted-vs-GAAP leg. The
§C-1 reconciliation is still owed by the underwrite on every row.

🔴 ONLY A POSITIVE MULTIPLE IS WRITTEN. A negative forward P/E over a positive
trailing one gives a negative ratio, which is below 0.60 and would fire the
guard on a company forecast to LOSE money -- a different finding under the
same flag. A zero, negative, missing or non-finite figure is left empty, and
an empty figure leaves the guard null: not measured, never a pass.

Source: yfinance Ticker.info (trailingPE, forwardPE), the same provider
build_prices.py uses. Yahoo returns 403 from the cloud, so like prices.csv
this is built on the PC at each store rebuild and committed with the store.
Yahoo's trailing figure is not guaranteed to be GAAP; that is why this is a
flag for a human and not a verdict.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from build_prices import tickers_from_lanes
from edgar_lib import Paths, log_stage

EPS_COLUMNS = ("ticker", "fwd_pe", "trail_pe", "as_of", "source")
EPS_SOURCE = "yfinance.info"
FORWARD_KEY, TRAILING_KEY = "forwardPE", "trailingPE"

InfoFetcher = Callable[[str], Mapping[str, Any]]


def positive_multiple(value: Any) -> float | None:
    """A finite multiple above zero, else None. Missing is never zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def eps_row(ticker: str, info: Mapping[str, Any], as_of: date) -> dict[str, Any]:
    """One eps.csv row from a provider info mapping."""
    return {
        "ticker": ticker.upper(),
        "fwd_pe": positive_multiple(info.get(FORWARD_KEY)),
        "trail_pe": positive_multiple(info.get(TRAILING_KEY)),
        "as_of": as_of.isoformat(),
        "source": EPS_SOURCE,
    }


def yahoo_info(ticker: str) -> Mapping[str, Any]:
    """Live fetch. Imported lazily so the module loads without yfinance."""
    import yfinance as yf

    return yf.Ticker(ticker).info or {}


def fetch_rows(
    tickers: Iterable[str], as_of: date, fetch: InfoFetcher = yahoo_info
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows for every ticker, plus the tickers whose fetch raised.

    A failed fetch still writes a row, with both figures empty, so the file
    names every ticker it was asked for and the guard reads null on it.
    """
    rows, failed = [], []
    for ticker in tickers:
        try:
            info = fetch(ticker)
        except Exception as error:  # noqa: BLE001 - provider errors are unbounded
            print(f"  {ticker}: fetch failed ({type(error).__name__}: {error})")
            failed.append(ticker)
            info = {}
        rows.append(eps_row(ticker, info, as_of))
    return rows, failed


def build_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    schema = {"ticker": pl.Utf8, "fwd_pe": pl.Float64, "trail_pe": pl.Float64,
              "as_of": pl.Utf8, "source": pl.Utf8}
    return pl.DataFrame(rows, schema=schema).select(EPS_COLUMNS).sort("ticker")


def resolve_out_path(paths: Paths, out: str | None) -> Path:
    """A bare filename lands in the data root, matching screen.py's --out."""
    if out is None:
        return paths.root / "eps.csv"
    if any(sep in out for sep in ("/", "\\")):
        return Path(out)
    return paths.root / out


def report(frame: pl.DataFrame, failed: list[str], out_path: Path) -> dict[str, int]:
    both = frame.filter(pl.col("fwd_pe").is_not_null() & pl.col("trail_pe").is_not_null())
    counts = {
        "requested": frame.height,
        "both_multiples": both.height,
        "guard_would_flag": both.filter(pl.col("fwd_pe") / pl.col("trail_pe") < 0.60).height,
        "not_measured": frame.height - both.height,
        "fetch_failed": len(failed),
    }
    print("")
    print("=" * 68)
    print(f"EPS COVERAGE  ->  {out_path}")
    print("=" * 68)
    for key, value in counts.items():
        print(f"  {key:18s}: {value}")
    print("  not_measured rows leave adjusted_eps_guard NULL -- never a pass.")
    print("=" * 68)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="data root directory")
    parser.add_argument("--out", default=None, help="output CSV (default: <root>/eps.csv)")
    parser.add_argument("--tickers", default=None,
                        help="explicit comma-separated list; default is every lane survivor")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be fetched; write nothing")
    args = parser.parse_args(argv)

    paths = Paths(args.root).ensure()
    out_path = resolve_out_path(paths, args.out)
    if args.tickers:
        tickers = sorted({t.strip().upper() for t in args.tickers.split(",") if t.strip()})
    else:
        tickers, missing, _ = tickers_from_lanes(paths.root)
        if missing:
            print("  🔴 lane files absent, their survivors get no EPS row: " + ", ".join(missing))
    if not tickers:
        raise SystemExit("no tickers to fetch; run screen.py first or pass --tickers")
    if args.dry_run:
        print(f"  --dry-run: would fetch {len(tickers)} tickers -> {out_path}; nothing written")
        return 0

    rows, failed = fetch_rows(tickers, date.today())
    frame = build_frame(rows)
    frame.write_csv(out_path)
    counts = report(frame, failed, out_path)
    log_stage("build_eps", out=str(out_path), **counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
