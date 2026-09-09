#!/usr/bin/env python3
"""Stage 4: build prices.csv for a named ticker list, and REPORT ITS COVERAGE.

    python build_prices.py --from-lanes
    python build_prices.py --tickers-from candidates.txt --out prices.csv
    python build_prices.py --tickers OMCL,SKYW,MCRI

EDGAR publishes no prices, so this is the one external join the screen needs.
It used to be an ad-hoc refresh, and that is why prices.csv covered ~137 of the
6,031 gate0 rows: every unpriced row reaches ``apply_band`` with a null
market_cap and is rejected there. A size-banded lane then returns ZERO FILERS
rather than zero matches -- the 2026-08-19 null-gate shape, where "shortlist
empty" was read as "the market contains nothing worth owning."

🔴 THE ORACLE FOR THIS SCRIPT IS COVERAGE, NOT EXIT CODE. A run that resolves
nothing exits 0 with a valid, tiny CSV. So every run prints tickers requested,
priced, and unpriced BY NAME, plus how many rows are older than --max-age-days.
A ticker that fails to resolve must appear in that list; it must never simply
be absent from the output.

Rows are REPLACED, never appended: one row per ticker, always. A stale row is
re-fetched rather than left beside a newer one -- on 2026-08-14 a 90-row price
file sat unused next to a 33-row one and 90 companies banded on a null.

The market_cap column is written deliberately, not left blank: a supplied cap
is what makes screen.py's shares/market-cap identity check
(``_add_shares_identity``) evaluable at all. A DERIVED cap is
price x shares_diluted by construction, so the ratio would be exactly 1.00 and
the check would confirm nothing but its own arithmetic.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from edgar_lib import Paths, log_stage

PRICE_COLUMNS = ["ticker", "price", "ma_200", "market_cap", "as_of"]
DEFAULT_MAX_AGE_DAYS = 7
MA_WINDOW = 200
# A year of trading days is ~252, comfortably more than the 200 the average
# needs, and short enough that the download stays one request per ticker.
HISTORY_PERIOD = "1y"

# Lane outputs carry every row they considered with a rejected_because label.
# A row rejected at BAND or MOMENTUM cleared the quality legs -- it is exactly
# a row that needed a price and did not have one. A row rejected earlier never
# reached the price gate and does not need pricing.
POST_QUALITY_REJECTIONS = ("", "band:market_cap", "band:market_cap_missing", "momentum")
LANE_FILES = (
    "shortlist_main.csv",
    "shortlist_shorthist.csv",
    "shortlist_ifrs.csv",
    "shortlist_inflection.csv",
    "shortlist_value.csv",
    "shortlist_accel.csv",
    "shortlist_margin2y.csv",
    "shortlist_unevaluated.csv",
)


# --------------------------------------------------------------------------
# Ticker list
# --------------------------------------------------------------------------


def tickers_from_lanes(root):
    """Union of every lane's quality-stage survivors.

    🔴 The UNION, not one lane's. The lanes overlap only partly -- value,
    accel, margin2y and unevaluated each draw from a different slice of the
    universe -- so pricing one lane's survivors leaves the others banding on
    nulls, which is the failure this script exists to end.

    Read from the shortlists rather than by re-running the screen: the lane
    files already record which stage cut each row, so this cannot drift from
    screen.py's own definition of the quality stage.
    """
    found, missing, per_lane = set(), [], {}
    for name in LANE_FILES:
        path = root / name
        if not path.exists():
            missing.append(name)
            continue
        frame = pl.read_csv(path, infer_schema_length=200000)
        if "rejected_because" not in frame.columns:
            missing.append(f"{name} (no rejected_because column)")
            continue
        survivors = frame.filter(
            pl.col("rejected_because").fill_null("").is_in(POST_QUALITY_REJECTIONS)
            & pl.col("ticker").is_not_null()
        )
        names = set(survivors["ticker"].str.to_uppercase().to_list())
        per_lane[name] = len(names)
        found |= names
    return sorted(found), missing, per_lane


def tickers_from_file(path):
    with open(path, encoding="utf-8") as handle:
        return sorted(
            {
                line.strip().upper()
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            }
        )


# --------------------------------------------------------------------------
# Freshness (pure -- this is the part the tests pin)
# --------------------------------------------------------------------------


def read_existing(path):
    """Existing rows keyed by ticker. A malformed as_of counts as absent."""
    if not path.exists():
        return {}
    frame = pl.read_csv(path)
    have = {c.lower().strip(): c for c in frame.columns}
    if "ticker" not in have or "as_of" not in have:
        return {}
    rows = {}
    for row in frame.to_dicts():
        ticker = str(row[have["ticker"]]).strip().upper()
        if not ticker:
            continue
        try:
            as_of = date.fromisoformat(str(row[have["as_of"]]).strip())
        except (ValueError, TypeError):
            continue
        rows[ticker] = {
            "ticker": ticker,
            "price": row.get(have.get("price", "")),
            "ma_200": row.get(have.get("ma_200", "")),
            "market_cap": row.get(have.get("market_cap", "")),
            "as_of": as_of,
        }
    return rows


def partition_by_freshness(requested, existing, max_age_days, today):
    """Split *requested* into (reusable, needs_fetch).

    A row is reusable only if it is present, priced, and within the age
    window. Anything else is re-fetched -- never appended beside.
    """
    cutoff = today - timedelta(days=max_age_days)
    reusable, needs_fetch = {}, []
    for ticker in requested:
        row = existing.get(ticker)
        if row is not None and row.get("price") is not None and row["as_of"] >= cutoff:
            reusable[ticker] = row
        else:
            needs_fetch.append(ticker)
    return reusable, needs_fetch


def stale_rows(rows, max_age_days, today):
    """Tickers in *rows* whose as_of is older than the age window."""
    cutoff = today - timedelta(days=max_age_days)
    return sorted(t for t, r in rows.items() if r["as_of"] < cutoff)


# --------------------------------------------------------------------------
# Quotes (the network seam -- tests substitute a fake)
# --------------------------------------------------------------------------


def _closes_for(frame, ticker, single):
    """The Close series for *ticker* out of a yfinance download frame."""
    if single:
        block = frame
    else:
        if ticker not in set(frame.columns.get_level_values(0)):
            return None
        block = frame[ticker]
    if "Close" not in block:
        return None
    return block["Close"].dropna()


def fetch_quotes(tickers, as_of):
    """{ticker: {price, ma_200, market_cap, as_of}} for whatever resolves.

    A ticker that does not resolve is simply ABSENT from the mapping; the
    caller reports it by name. Nothing is invented and nothing is carried
    forward -- a name with no price this week has no price this week.
    """
    tickers = list(tickers)
    if not tickers:
        return {}
    import yfinance as yf

    frame = yf.download(
        tickers,
        period=HISTORY_PERIOD,
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=True,
        group_by="ticker",
    )
    single = len(tickers) == 1
    out = {}
    for ticker in tickers:
        try:
            closes = _closes_for(frame, ticker, single)
        except (KeyError, IndexError, TypeError, AttributeError):
            continue
        if closes is None or len(closes) == 0:
            continue
        price = float(closes.iloc[-1])
        if not price > 0:
            continue
        ma_200 = float(closes.iloc[-MA_WINDOW:].mean()) if len(closes) >= MA_WINDOW else None
        try:
            cap = yf.Ticker(ticker).fast_info.market_cap
            market_cap = float(cap) if cap else None
        except Exception:  # noqa: BLE001 - a missing cap is reported, never fatal
            market_cap = None
        out[ticker] = {
            "ticker": ticker,
            "price": price,
            "ma_200": ma_200,
            "market_cap": market_cap,
            "as_of": as_of,
        }
    return out


# --------------------------------------------------------------------------
# Output + coverage
# --------------------------------------------------------------------------


def build_frame(rows):
    """One row per ticker, sorted, in the PRICES_SCHEMA.md column order."""
    schema = {
        "ticker": pl.String,
        "price": pl.Float64,
        "ma_200": pl.Float64,
        "market_cap": pl.Float64,
        "as_of": pl.String,
    }
    records = [
        {
            "ticker": r["ticker"],
            "price": None if r.get("price") is None else float(r["price"]),
            "ma_200": None if r.get("ma_200") is None else float(r["ma_200"]),
            "market_cap": None if r.get("market_cap") is None else float(r["market_cap"]),
            "as_of": r["as_of"].isoformat()
            if isinstance(r["as_of"], date)
            else str(r["as_of"]),
        }
        for r in sorted(rows.values(), key=lambda x: x["ticker"])
    ]
    if not records:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(records, schema=schema).select(PRICE_COLUMNS)


def report_coverage(
    requested, priced, carried, max_age_days, today, out_path, stream=sys.stdout
):
    """🔴 Printed on EVERY run, including a fully successful one.

    Unpriced tickers are listed BY NAME. A count alone is what let 137-of-6031
    look like a working price file.
    """
    unpriced = sorted(set(requested) - set(priced))
    old = stale_rows(priced, max_age_days, today)
    pct = f"  ({len(priced) / len(requested):.1%})" if requested else ""

    def write(line):
        print(line, file=stream)

    write("")
    write("=" * 68)
    write(f"PRICE COVERAGE  ->  {out_path}")
    write("=" * 68)
    write(f"  tickers requested       : {len(requested)}")
    write(f"  priced                  : {len(priced)}{pct}")
    write(f"  UNPRICED                : {len(unpriced)}")
    write(f"  rows older than {max_age_days}d      : {len(old)}")
    if carried:
        write(f"  carried (not requested) : {len(carried)}")
    if unpriced:
        write("")
        write("  UNPRICED TICKERS -- no price written, so each of these is")
        write("  rejected at apply_band as market_cap_missing, NOT screened:")
        for i in range(0, len(unpriced), 10):
            write("    " + ", ".join(unpriced[i : i + 10]))
    if old:
        write("")
        write(f"  STALE (as_of older than {max_age_days} days):")
        for i in range(0, len(old), 10):
            write("    " + ", ".join(old[i : i + 10]))
    write("=" * 68)
    return {
        "requested": len(requested),
        "priced": len(priced),
        "unpriced": len(unpriced),
        "stale": len(old),
        "unpriced_tickers": unpriced,
    }


def resolve_out_path(paths, out):
    """A bare filename lands in the data root, matching screen.py's --out."""
    if out is None:
        return paths.root / "prices.csv"
    if any(sep in out for sep in ("/", "\\")):
        return Path(out)
    return paths.root / out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="data root directory")
    parser.add_argument("--out", default=None, help="output CSV (default: <root>/prices.csv)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-lanes",
        action="store_true",
        help="union of every lane's quality-stage survivors",
    )
    source.add_argument(
        "--tickers-from", metavar="FILE", help="explicit ticker list, one per line"
    )
    source.add_argument("--tickers", help="explicit ticker list, comma separated")
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=f"re-fetch rows older than this (default {DEFAULT_MAX_AGE_DAYS})",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-fetch every requested ticker regardless of age"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be fetched; write nothing"
    )
    args = parser.parse_args(argv)

    paths = Paths(args.root).ensure()
    out_path = resolve_out_path(paths, args.out)

    if args.from_lanes:
        requested, missing_lanes, per_lane = tickers_from_lanes(paths.root)
        for name, count in sorted(per_lane.items()):
            print(f"  {name}: {count} quality-stage survivors")
        if missing_lanes:
            print(
                "  🔴 lane files absent, their survivors are NOT in this list: "
                + ", ".join(missing_lanes)
            )
        if not requested:
            raise SystemExit(
                "--from-lanes found no quality-stage survivors. Run screen.py for "
                "each lane first; an empty list here would silently produce an "
                "empty price file."
            )
    elif args.tickers_from:
        requested = tickers_from_file(args.tickers_from)
    else:
        requested = sorted({t.strip().upper() for t in args.tickers.split(",") if t.strip()})

    today = date.today()
    existing = read_existing(out_path)
    if args.force:
        reusable, needs_fetch = {}, list(requested)
    else:
        reusable, needs_fetch = partition_by_freshness(
            requested, existing, args.max_age_days, today
        )

    print(
        f"  {len(requested)} requested | {len(reusable)} fresh enough to reuse | "
        f"{len(needs_fetch)} to fetch"
    )
    if args.dry_run:
        report_coverage(requested, reusable, {}, args.max_age_days, today, out_path)
        print("  --dry-run: nothing written")
        return 0

    fetched = fetch_quotes(needs_fetch, today) if needs_fetch else {}

    priced = dict(reusable)
    priced.update(fetched)
    # Rows for tickers nobody asked about are KEPT -- a price already paid for
    # is not garbage -- but they are reported separately so the coverage
    # numbers describe the requested list and nothing else.
    carried = {t: r for t, r in existing.items() if t not in priced and t not in requested}

    build_frame({**carried, **priced}).write_csv(out_path)

    stats = report_coverage(requested, priced, carried, args.max_age_days, today, out_path)
    log_stage(
        "build_prices",
        rows=f"{len(carried) + len(priced):,}",
        requested=str(stats["requested"]),
        priced=str(stats["priced"]),
        unpriced=str(stats["unpriced"]),
        stale=str(stats["stale"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
