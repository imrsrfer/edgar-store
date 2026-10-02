#!/usr/bin/env python3
"""Stage 5: rank every lane's survivors, plus the §G-1 refill, into ONE queue.

    python rank_queue.py --exclude-tickers tracked.txt
    python rank_queue.py --exclude-tickers tracked.txt --price-csv prices.csv --out queue_ranked.csv

Added 2026-10-01. Three jobs the sweep was doing by hand.

1. NOTHING RANKED ACROSS LANES. Each lane wrote its own shortlist and the queue
   was assembled by hand from seven files: ~370 unique survivors, 95 of them in
   two or more lanes, against MODE B capacity of five a run.

2. THE §G-1 REFILL WAS A HAND-BUILT FUNNEL. Once the in-band lane survivors
   ran out, the 2026-09-27 refill went back to every US non-financial
   ``gate0_framework_pass`` row, subtracted the tracked names, applied four
   cuts and priced the remainder one by one through Stocklake and
   stockanalysis (🆕 SMID refill page, R1-R106). That funnel is reproduced here
   as the ``direct`` source, step for step, and PRINTED every run -- the
   2026-09-27 corrected call: "exhausted" is never written without a funnel
   table beside it, and a lane outcome is not a universe count.

3. SLOTS WERE SPENT ON KNOWN-BAD STORE DATA. 34 of 251 discards (13.55%) cited
   a store defect found by hand. store_defects.py now names them at build time.

MEASURED YIELDS (370 Exclusion Index dispositions joined to the 2026-09-29
store; cap at current price; kept = pipeline or watchlist):

       route / band                     reviewed   kept   keep rate
       lane survivor, $0.5-5B               123     69      56.1%
       lane survivor, < $0.5B                34     17      50.0%
       lane survivor, $5-20B                 55     12      21.8%
       lane survivor, > $20B                 30      2       6.7%
       refill (direct), R1-R101             101     10       9.9%
       gate0 pass, framework FAIL             9      0       0.0%

       survives 2+ lanes                     66     37      54.5%
       survives 1 lane                      175     65      37.1%

   P/FCF-after-SBC at today's price barely separates keepers (48.6% under 12x,
   50.0% at 12-18x, 32.3% at 18-25x), so it is the LAST sort key.

THE RANKING RULE (stated, so it can be argued with). It orders effort only --
it cannot fire a trade, carries no date, and caps nothing.

    tier A  US domestic filer, non-financial, cap $0.5-5B   (§G-1 / Fork J band)
    tier B  cap below $0.5B, or $0.5-5B foreign filer        (§F-2 ADV owed)
    tier C  cap $5-20B
    tier D  cap above $20B
    tier E  no market cap -- PRICE IT FIRST, it cannot be tiered

    within a tier:
      1. rows with no fail-open store defect before rows with one
      2. more lanes first -- a direct (refill) name has zero and sorts after
         every lane survivor in its tier, which is what 56.1% vs 9.9% says
      3. cheaper first on live P/FCF-after-SBC, NULLS LAST (BELFB, 2026-08-06)

🔴 NO CAP. §G-1 says never cap the queue (Fork J, 2026-09-25). The 2026-08-26
residual asking for a top-N cap predates that ruling and is superseded by it;
an ordering is what was missing, not a limit.

🔴 TRACKED NAMES ARE LABELLED, NEVER DROPPED. --exclude-tickers takes the
raw-text diff the sweep already owes (console prose lists, Review Queue pages,
Exclusion Index, Checkpoint Register -- the 2026-09-21 near-miss rule). Those
rows carry tracked=True and no rank. Running without it is allowed and says so.

This script changes no lane verdict and fetches no price.
"""

from __future__ import annotations

import argparse
import os

import polars as pl

from build_prices import LANE_FILES
from edgar_lib import Paths, log_stage
from gate0 import DEFAULT_EXCLUDE_SIC, parse_sic_ranges
from store_defects import FAIL_OPEN_DEFECTS

BAND_LOW, BAND_HIGH, LARGE_HIGH = 500e6, 5e9, 20e9
US_FILER_FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A")
DIRECT_MIN_REVENUE = 50e6
TIER_LABELS = {
    "A": "in-band US non-fin",
    "B": "sub-band or foreign in-band (ADV check owed)",
    "C": "$5-20B",
    "D": ">$20B",
    "E": "NO MARKET CAP -- price first",
}

# Columns carried into the queue, when present.
CARRY = (
    "cik", "ticker", "company_name", "sic", "filing_form", "market_cap",
    "fcf_after_sbc", "p_fcf_after_sbc_live", "revenue", "gate0_status",
    "gate0_framework_pass", "drawdown_undiagnosed", "warn_inorganic",
    "gate0_unevaluated", "tier2_commodity", "store_defect_flags",
)


def _num(name):
    return pl.col(name).cast(pl.Float64, strict=False)


def _truthy(name):
    return pl.col(name).cast(pl.Utf8).str.to_lowercase().is_in(["true", "1"]).fill_null(False)


def load_survivors(root, lane_files=LANE_FILES):
    """Every lane's survivors, one row per company, with the lanes named."""
    frames, missing = [], []
    for name in lane_files:
        path = root / name
        if not path.exists():
            missing.append(name)
            continue
        frame = pl.read_csv(path, infer_schema_length=0)
        if "rejected_because" not in frame.columns:
            missing.append(f"{name} (no rejected_because)")
            continue
        lane = name.removeprefix("shortlist_").removesuffix(".csv")
        keep = [c for c in CARRY if c in frame.columns]
        frames.append(
            frame.filter(pl.col("rejected_because").fill_null("") == "")
            .select(keep)
            .with_columns(pl.lit(lane).alias("lane"))
        )
    if not frames:
        return pl.DataFrame(), missing
    stacked = pl.concat(frames, how="diagonal_relaxed")
    key = pl.coalesce(pl.col("ticker").str.to_uppercase(), pl.col("cik").cast(pl.Utf8))
    stacked = stacked.with_columns(key.alias("_key"))
    others = [c for c in stacked.columns if c not in ("lane", "_key", "ticker")]
    merged = stacked.group_by("_key").agg(
        *[pl.col(c).drop_nulls().first().alias(c) for c in others],
        pl.col("lane").unique().sort().str.join("+").alias("lanes"),
        pl.col("lane").n_unique().alias("lane_count"),
    )
    return merged.rename({"_key": "ticker"}), missing


def direct_pass(gate0, prices, lane_tickers, exclude_sic=DEFAULT_EXCLUDE_SIC):
    """The §G-1 refill funnel, step for step as the 2026-09-27 page built it.

    Returns (rows, funnel) where funnel is an ordered list of (step, count).
    A NULL growth figure is kept and NAMED (growth_not_measured), never cut --
    the refill page routed its three null-CAGR names to a NOT MEASURED list.
    """
    sic = pl.col("sic").cast(pl.Int32, strict=False)
    financial = pl.lit(False)
    for low, high in parse_sic_ranges(exclude_sic):
        financial = financial | sic.is_between(low, high)
    funnel = []

    def step(frame, label):
        funnel.append((label, frame.height))
        return frame

    frame = step(
        gate0.filter(
            pl.col("filing_form").is_in(list(US_FILER_FORMS))
            & ~financial.fill_null(False)
            & pl.col("ticker").is_not_null()
        ),
        "US 10-K/10-Q filers, non-financial",
    )
    frame = step(frame.filter(_truthy("gate0_framework_pass")), "gate0_framework_pass")
    frame = frame.with_columns(pl.col("ticker").str.to_uppercase())
    frame = step(
        frame.filter(~pl.col("ticker").is_in(list(lane_tickers))),
        "not already a lane survivor",
    )
    frame = step(
        frame.filter(_num("revenue") > DIRECT_MIN_REVENUE), "revenue above $50M"
    )
    suspect = pl.lit(False)
    for name in ("net_income_suspect", "shares_scale_suspect"):
        if name in frame.columns:
            suspect = suspect | _truthy(name)
    frame = step(frame.filter(~suspect), "minus net_income_suspect / shares_scale_suspect")
    frame = step(
        frame.filter(_num("ttm_fcf_after_sbc") > 0), "TTM FCF-after-SBC above 0"
    )
    rev = _num("revenue_cagr_3y")
    frame = step(frame.filter(rev.is_null() | (rev > 0)), "revenue CAGR 3y above 0 (nulls kept)")
    c3, c5 = _num("fcf_per_share_cagr_3y"), _num("fcf_per_share_cagr_5y")
    inflection = _truthy("fcf_inflection") if "fcf_inflection" in frame.columns else pl.lit(False)
    grows = (c3 > 0).fill_null(False) | (c5 > 0).fill_null(False) | inflection
    unmeasured = c3.is_null() & c5.is_null() & ~inflection
    frame = step(
        frame.filter(grows | unmeasured),
        "FCF/share CAGR 3y or 5y above 0, or an inflection (nulls kept)",
    ).with_columns((rev.is_null() | unmeasured).alias("growth_not_measured"))

    if prices is not None and prices.height:
        if "shares_diluted" not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("shares_diluted"))
        cap_col = "market_cap_supplied" if "market_cap_supplied" in prices.columns else "market_cap"
        frame = frame.drop([c for c in ("market_cap",) if c in frame.columns]).join(
            prices.select(
                pl.col("ticker").str.to_uppercase(),
                pl.col(cap_col).cast(pl.Float64, strict=False).alias("_cap"),
                pl.col("price").cast(pl.Float64, strict=False).alias("_price"),
            ),
            on="ticker",
            how="left",
        ).with_columns(
            # Same rule as screen.apply_prices: a supplied cap wins; otherwise
            # price x the store's diluted count, which lags buybacks.
            pl.coalesce(pl.col("_cap"), pl.col("_price") * _num("shares_diluted"))
            .alias("market_cap")
        ).drop("_cap", "_price")
    elif "market_cap" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("market_cap"))
    funnel.append(("priced (market cap known)", int(frame["market_cap"].is_not_null().sum())))
    keep = [c for c in CARRY if c in frame.columns] + ["growth_not_measured"]
    rows = frame.select(keep).with_columns(
        pl.lit("direct").alias("lanes"), pl.lit(0).alias("lane_count")
    )
    return rows, funnel


def rank(frame, tracked=(), exclude_sic=DEFAULT_EXCLUDE_SIC):
    """Tier and order the merged survivors. Pure; the tests pin it."""
    tracked = {t.strip().upper() for t in tracked if t and t.strip()}
    for column in ("store_defect_flags", "filing_form", "sic", "ticker"):
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Utf8).alias(column))
    cap = _num("market_cap")
    sic = pl.col("sic").cast(pl.Int32, strict=False)
    financial = pl.lit(False)
    for low, high in parse_sic_ranges(exclude_sic):
        financial = financial | sic.is_between(low, high)
    us = pl.col("filing_form").is_in(list(US_FILER_FORMS)).fill_null(False)
    in_band = (cap >= BAND_LOW) & (cap <= BAND_HIGH)
    tier = (
        pl.when(cap.is_null()).then(pl.lit("E"))
        .when(in_band & us & ~financial.fill_null(False)).then(pl.lit("A"))
        .when((cap < BAND_LOW) | in_band).then(pl.lit("B"))
        .when(cap <= LARGE_HIGH).then(pl.lit("C"))
        .otherwise(pl.lit("D"))
    )
    flags = pl.col("store_defect_flags").fill_null("")
    fail_open = pl.lit(False)
    for name in FAIL_OPEN_DEFECTS:
        fail_open = fail_open | flags.str.contains(name, literal=True)
    live = _num("p_fcf_after_sbc_live") if "p_fcf_after_sbc_live" in frame.columns else pl.lit(None, dtype=pl.Float64)
    fcf = _num("fcf_after_sbc") if "fcf_after_sbc" in frame.columns else pl.lit(None, dtype=pl.Float64)
    multiple = pl.coalesce(live, pl.when(fcf > 0).then(cap / fcf).otherwise(None))
    out = frame.with_columns(
        tier.alias("tier"),
        fail_open.alias("fail_open_defect"),
        multiple.alias("p_fcf_after_sbc_rank"),
        pl.col("ticker").str.to_uppercase().is_in(list(tracked)).fill_null(False).alias("tracked"),
        pl.col("lane_count").cast(pl.Int32),
    ).sort(
        ["tracked", "tier", "fail_open_defect", "lane_count", "p_fcf_after_sbc_rank", "ticker"],
        descending=[False, False, False, True, False, False],
        nulls_last=True,
    )
    untracked = ~pl.col("tracked")
    out = out.with_columns(
        pl.when(untracked).then(untracked.cast(pl.Int32).cum_sum()).otherwise(None).alias("rank"),
        pl.concat_str(
            [
                pl.col("tier"),
                pl.lit(" "),
                pl.col("tier").replace_strict(TIER_LABELS, default=""),
                pl.lit(" | "),
                pl.when(pl.col("lane_count") == 0)
                .then(pl.lit("refill (no lane)"))
                .otherwise(pl.col("lane_count").cast(pl.Utf8) + pl.lit(" lane(s)")),
                pl.lit(" | "),
                pl.when(pl.col("p_fcf_after_sbc_rank").is_null())
                .then(pl.lit("P/FCF-aSBC n/a"))
                .otherwise(pl.col("p_fcf_after_sbc_rank").round(1).cast(pl.Utf8) + pl.lit("x")),
                pl.when(pl.col("fail_open_defect"))
                .then(pl.lit(" | STORE DEFECT: ") + flags)
                .otherwise(pl.lit("")),
            ]
        ).alias("rank_reason"),
    )
    lead = ["rank", "tracked", "tier", "ticker", "lanes", "lane_count", "market_cap",
            "p_fcf_after_sbc_rank", "fail_open_defect", "store_defect_flags", "rank_reason"]
    lead = [c for c in lead if c in out.columns]
    return out.select(lead + [c for c in out.columns if c not in lead])


def load_tracked(path):
    if not path:
        return []
    with open(path, encoding="utf-8") as handle:
        return [line.strip() for line in handle
                if line.strip() and not line.lstrip().startswith("#")]


def summarise(ranked, funnel, tracked_supplied, head=10):
    live = ranked.filter(~pl.col("tracked"))
    counts = dict(live.group_by("tier").len().iter_rows())
    print("")
    print("=" * 68)
    print(f"REVIEW QUEUE  ({live.height} untracked, uncapped -- §G-1)")
    print("=" * 68)
    for key in "ABCDE":
        print(f"  tier {key}  {counts.get(key, 0):4d}  {TIER_LABELS[key]}")
    print(f"  tracked (labelled, not ranked): {ranked.height - live.height}")
    if not tracked_supplied:
        print("  🔴 NO --exclude-tickers: settled names are ranked as if new. Supply the")
        print("     raw-text diff of the console, Review Queue pages and Exclusion Index.")
    if funnel:
        print("")
        print("  §G-1 REFILL FUNNEL (direct source; tracked names are subtracted at ranking)")
        for label, n in funnel:
            print(f"    {n:6,d}  {label}")
        direct_live = live.filter(pl.col("lane_count") == 0)
        print(f"    {direct_live.height:6,d}  untracked after the --exclude-tickers diff")
        print(f"    {direct_live.filter(pl.col('tier') == 'A').height:6,d}  of which tier A")
    if counts.get("E", 0):
        print(f"  ⚠️  {counts['E']} row(s) have NO MARKET CAP and cannot be tiered -- run")
        print("     build_prices.py --from-framework-pass, then re-rank.")
    shown = live.head(head)
    if shown.height:
        print("")
        for row in shown.iter_rows(named=True):
            print(f"  {row['rank']:3d}  {str(row['ticker']):7s} {row['rank_reason']}")
    print("=" * 68)
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="data root directory")
    parser.add_argument("--out", default="queue_ranked.csv",
                        help="output CSV; a bare filename lands in the data root")
    parser.add_argument("--exclude-tickers", default=None,
                        help="tracked tickers, one per line (labelled, never dropped)")
    parser.add_argument("--price-csv", action="append", default=None, metavar="PATH",
                        help="price file(s) for the direct source; default <root>/prices.csv")
    parser.add_argument("--no-direct", action="store_true",
                        help="lane survivors only; skip the §G-1 refill funnel")
    parser.add_argument("--exclude-sic", default=DEFAULT_EXCLUDE_SIC)
    args = parser.parse_args(argv)

    paths = Paths(args.root).ensure()
    out_path = args.out
    if not os.path.isabs(out_path) and os.path.dirname(out_path) == "":
        out_path = str(paths.root / out_path)

    merged, missing = load_survivors(paths.root)
    if missing:
        print("  🔴 lane files absent, their survivors are NOT ranked: " + ", ".join(missing))
    frames, funnel = [merged] if merged.height else [], []
    if not args.no_direct:
        from screen import load_prices, merge_price_frames  # validates the files

        gate0 = pl.read_csv(paths.gate0, infer_schema_length=0)
        known = set(gate0["ticker"].drop_nulls().to_list())
        sources = args.price_csv or [str(paths.root / "prices.csv")]
        loaded = [
            load_prices(p, known, str(paths.root / "prices_unmatched.csv"))
            for p in sources if os.path.exists(p)
        ]
        prices = merge_price_frames(loaded) if loaded else None
        lane_tickers = set(merged["ticker"].to_list()) if merged.height else set()
        direct, funnel = direct_pass(gate0, prices, lane_tickers, args.exclude_sic)
        if direct.height:
            frames.append(direct)
    if not frames:
        raise SystemExit("no lane survivors and no direct-source rows; run screen.py first")
    combined = pl.concat(frames, how="diagonal_relaxed")
    tracked = load_tracked(args.exclude_tickers)
    ranked = rank(combined, tracked, args.exclude_sic)
    ranked.write_csv(out_path)
    counts = summarise(ranked, funnel, bool(tracked))
    log_stage("rank_queue", rows=ranked.height, out=out_path,
              **{f"tier_{k}": v for k, v in sorted(counts.items())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
