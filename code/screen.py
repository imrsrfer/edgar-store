"""Stage 4: apply the Gate 0 shortlisting screens as tested, composable
functions over gate0.csv, instead of hand-written per-run.

    python screen.py --lane main       --min-mktcap 500e6 --max-mktcap 5e9 --out shortlist_main.csv
    python screen.py --lane shorthist  --min-mktcap 500e6 --max-mktcap 5e9 --out shortlist_shorthist.csv
    python screen.py --lane ifrs       --min-mktcap 500e6 --max-mktcap 5e9 --out shortlist_ifrs.csv
    python screen.py --lane inflection --min-mktcap 500e6 --max-mktcap 5e9 --out shortlist_inflection.csv

On 2026-08-07 these screens were hand-written three separate times (main
universe, spin-off lane, IFRS lane) and a bug was introduced in one of them:
the margin-expansion filter tested ``operating_margin_delta > 0`` but never
``operating_margin_latest > 0``, so a company improving from deeply negative
to merely less-negative reached a shortlist meant to find margin expansion.
Every screen here is a single tested function specifically so that class of
bug cannot recur silently -- see apply_margin_expansion.

Every row screen.py evaluates is written to --out, survivors and rejects
alike, with a ``rejected_because`` column explaining exactly which stage cut
it (empty string for survivors). A screen that silently drops names is
unauditable.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

import polars as pl

from edgar_lib import Paths, log_stage
from gate0 import DEFAULT_EXCLUDE_SIC, parse_sic_ranges

LANES = ("main", "shorthist", "ifrs", "inflection")

DEFAULT_MIN_REVENUE = 50e6
GROWTH_MIN_REVENUE_CAGR = 0.02
GROWTH_MIN_FCF_CAGR = 0.05
QUALITY_MIN_INCOME_QUALITY = 0.90
QUALITY_MAX_SBC_PCT_REVENUE = 0.10
EPS_GUARD_RATIO = 0.60
PRICE_STALE_DAYS = 7

ELIGIBLE_STATUSES = ("pass", "pass_stale")


# --------------------------------------------------------------------------
# Eligibility -- everything a candidate must clear before the named screens
# even apply.
# --------------------------------------------------------------------------


def apply_eligibility(frame, args, exclude_tickers):
    """gate0_status pass/pass_stale, non-financial unless asked, min revenue,
    and the exclude list. Returns (survivors, [rejected batches])."""
    batches = []
    remaining = frame

    if exclude_tickers:
        excluded_mask = pl.col("ticker").is_in(exclude_tickers) | pl.col(
            "cik"
        ).cast(pl.Utf8).is_in(exclude_tickers)
        batches.append(
            remaining.filter(excluded_mask).with_columns(
                pl.lit("excluded_list").alias("rejected_because")
            )
        )
        remaining = remaining.filter(~excluded_mask)

    if not args.include_financials:
        sic = pl.col("sic").cast(pl.Int32, strict=False)
        is_financial = pl.lit(False)
        for low, high in parse_sic_ranges(args.exclude_sic):
            is_financial = is_financial | sic.is_between(low, high)
        batches.append(
            remaining.filter(is_financial).with_columns(
                pl.lit("ineligible:financial_sic").alias("rejected_because")
            )
        )
        remaining = remaining.filter(~is_financial)

    status_ok = pl.col("gate0_status").is_in(list(ELIGIBLE_STATUSES))
    batches.append(
        remaining.filter(~status_ok).with_columns(
            pl.lit("ineligible:gate0_status").alias("rejected_because")
        )
    )
    remaining = remaining.filter(status_ok)

    revenue_ok = pl.col("revenue").is_not_null() & (pl.col("revenue") > args.min_revenue)
    batches.append(
        remaining.filter(~revenue_ok).with_columns(
            pl.lit("ineligible:min_revenue").alias("rejected_because")
        )
    )
    remaining = remaining.filter(revenue_ok)

    return remaining, batches


# --------------------------------------------------------------------------
# Screen 2: growth
# --------------------------------------------------------------------------


def _effective_cagr(prefix):
    """The CAGR actually usable for a company: 5y if it has one, 3y (short
    history) otherwise, null if neither -- i.e. growth_basis made concrete."""
    return (
        pl.when(pl.col("growth_basis") == "5y")
        .then(pl.col(f"{prefix}_cagr_5y"))
        .when(pl.col("growth_basis") == "3y")
        .then(pl.col(f"{prefix}_cagr_3y"))
        .otherwise(None)
    )


def apply_growth(frame, lane):
    """revenue CAGR > 2% and FCF/share CAGR > 5%, using growth_basis (5y,
    falling back to 3y for short_history).

    In the inflection lane the FCF/share CAGR leg is replaced with
    fcf_inflection and a positive fcf_per_share_delta_abs -- the CAGR is
    undefined for a negative-to-positive move by construction (see
    build_fcf_inflection), so a CAGR-only screen is structurally blind to
    exactly the turnaround category this lane exists for. The revenue leg is
    unchanged.
    """
    revenue_cagr = _effective_cagr("revenue")
    revenue_ok = revenue_cagr.is_not_null() & (revenue_cagr > GROWTH_MIN_REVENUE_CAGR)

    if lane == "inflection":
        fcf_ok = pl.col("fcf_inflection").fill_null(False) & (
            pl.col("fcf_per_share_delta_abs").is_not_null()
            & (pl.col("fcf_per_share_delta_abs") > 0)
        )
    else:
        fcf_cagr = _effective_cagr("fcf_per_share")
        fcf_ok = fcf_cagr.is_not_null() & (fcf_cagr > GROWTH_MIN_FCF_CAGR)

    ok = revenue_ok & fcf_ok
    rejected = frame.filter(~ok).with_columns(pl.lit("growth").alias("rejected_because"))
    return frame.filter(ok), rejected


# --------------------------------------------------------------------------
# Screen 3: margin expansion -- the PERF bug, fixed and made impossible to
# reintroduce by living in exactly one function.
# --------------------------------------------------------------------------


def apply_margin_expansion(frame):
    """operating_margin_delta > 0 AND operating_margin_latest > 0.

    BOTH conditions are mandatory. On 2026-08-07 a hand-written version of
    this screen tested only the delta, so a company improving from -30% to
    -2.5% operating margin (PERF) reached a "margin expansion" shortlist --
    it expanded, but it is still unprofitable. delta>0 alone answers
    "improving"; latest>0 alone answers "profitable"; a margin-expansion
    screen means both.
    """
    ok = (
        pl.col("operating_margin_delta").is_not_null()
        & (pl.col("operating_margin_delta") > 0)
        & pl.col("operating_margin_latest").is_not_null()
        & (pl.col("operating_margin_latest") > 0)
    )
    rejected = frame.filter(~ok).with_columns(
        pl.lit("margin_expansion").alias("rejected_because")
    )
    return frame.filter(ok), rejected


# --------------------------------------------------------------------------
# Gate 0 quality screen
# --------------------------------------------------------------------------


def apply_quality(frame):
    """income_quality >= 0.9, sbc_pct_revenue <= 0.10, tangible_book > 0,
    tangible_book_yrs_negative = 0, not warn_inorganic."""
    ok = (
        pl.col("income_quality").is_not_null()
        & (pl.col("income_quality") >= QUALITY_MIN_INCOME_QUALITY)
        & pl.col("sbc_pct_revenue").is_not_null()
        & (pl.col("sbc_pct_revenue") <= QUALITY_MAX_SBC_PCT_REVENUE)
        & pl.col("tangible_book").is_not_null()
        & (pl.col("tangible_book") > 0)
        & (pl.col("tangible_book_yrs_negative").fill_null(0) == 0)
        & ~pl.col("warn_inorganic").fill_null(False)
    )
    rejected = frame.filter(~ok).with_columns(pl.lit("quality").alias("rejected_because"))
    return frame.filter(ok), rejected


# --------------------------------------------------------------------------
# Market-cap band
# --------------------------------------------------------------------------


def apply_band(frame, min_mktcap, max_mktcap):
    """Market-cap band, with "too big/small" and "no market cap at all" kept
    as DIFFERENT rejections.

    They used to share the label ``band:market_cap``, and that one shared
    label cost twelve weeks. The store carries no market caps of its own --
    EDGAR does not publish them -- so without a price file every survivor of
    the quality legs arrives here with a null and is rejected. On 2026-08-19
    all four lanes were found to have produced ZERO shortlisted names, with
    115 companies that had cleared every growth, margin and quality leg
    rejected at this gate, 100% of them for a null rather than a size. The
    run reported "shortlist empty," which is indistinguishable from "the
    market contains nothing worth owning" -- and was read as exactly that.

    A missing input and a failed test are not the same finding. Anything that
    reports them identically will eventually be believed.
    """
    if min_mktcap is None and max_mktcap is None:
        return frame, frame.filter(pl.lit(False))

    missing = pl.col("market_cap").is_null()
    no_cap = frame.filter(missing).with_columns(
        pl.lit("band:market_cap_missing").alias("rejected_because")
    )
    remaining = frame.filter(~missing)

    ok = pl.lit(True)
    if min_mktcap is not None:
        ok = ok & (pl.col("market_cap") >= min_mktcap)
    if max_mktcap is not None:
        ok = ok & (pl.col("market_cap") <= max_mktcap)
    out_of_band = remaining.filter(~ok).with_columns(
        pl.lit("band:market_cap").alias("rejected_because")
    )
    return remaining.filter(ok), pl.concat([no_cap, out_of_band], how="diagonal_relaxed")


# --------------------------------------------------------------------------
# Screen 1: momentum
# --------------------------------------------------------------------------


def apply_momentum(frame, prices_supplied):
    """pct_vs_200ma > 0, requires --price-csv.

    Without a price file this is not "passed", it is not evaluated --
    momentum_not_evaluated makes that explicit rather than a bare null that
    reads the same as "computed and clean". With a price file, a company
    below its 200-day average is a real rejection, not skipped.
    """
    if not prices_supplied:
        return (
            frame.with_columns(pl.lit(True).alias("momentum_not_evaluated")),
            frame.filter(pl.lit(False)),
        )
    frame = frame.with_columns(pl.lit(False).alias("momentum_not_evaluated"))
    ok = pl.col("pct_vs_200ma").is_not_null() & (pl.col("pct_vs_200ma") > 0)
    rejected = frame.filter(~ok).with_columns(pl.lit("momentum").alias("rejected_because"))
    return frame.filter(ok), rejected


# --------------------------------------------------------------------------
# Adjusted-EPS guard -- flags, never rejects.
# --------------------------------------------------------------------------


def _safe_ratio(numerator, denominator):
    return (
        pl.when(numerator.is_null() | denominator.is_null() | (denominator == 0))
        .then(None)
        .otherwise(numerator / denominator)
    )


def apply_eps_guard(frame, eps_supplied):
    """fwd_trail_ratio = fwd_pe / trail_pe; flagged (not rejected) below 0.60.

    A low ratio can mean a depressed trailing base as easily as an inflated
    forward estimate -- opposite readings of the same number -- so this is
    strictly informational. It must never remove a row from the shortlist.
    """
    if not eps_supplied:
        return frame.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("fwd_trail_ratio"),
            pl.lit(False).alias("adjusted_eps_guard"),
        )
    ratio = _safe_ratio(pl.col("fwd_pe"), pl.col("trail_pe"))
    return frame.with_columns(
        ratio.alias("fwd_trail_ratio"),
        (ratio.is_not_null() & (ratio < EPS_GUARD_RATIO)).alias("adjusted_eps_guard"),
    )


# --------------------------------------------------------------------------
# Ticker/CIK matching -- exact only. On 2026-08-07 a substring match on
# "COPA" returned COPART (a different company, different industry) and its
# financials were read as Copa's for several minutes. Never again: this is
# the only lookup function screen.py uses anywhere, so there is exactly one
# place this class of bug can be reintroduced, and it is tested directly.
# --------------------------------------------------------------------------


def lookup_by_ticker_or_cik(frame, identifier):
    """Exact match on ticker (case-insensitive) or bare CIK. Never a
    substring/contains match on ticker or company_name."""
    identifier = identifier.strip().upper()
    if identifier.isdigit():
        return frame.filter(pl.col("cik") == int(identifier))
    return frame.filter(pl.col("ticker") == identifier)


def load_exclude_tickers(path):
    if not path:
        return set()
    with open(path, "r", encoding="utf-8") as handle:
        return {line.strip().upper() for line in handle if line.strip()}


# --------------------------------------------------------------------------
# prices.csv (see PRICES_SCHEMA.md)
# --------------------------------------------------------------------------


def load_prices(path, known_tickers, unmatched_out):
    """Load and validate the prices.csv contract: ticker,price,ma_200,
    market_cap,as_of. See PRICES_SCHEMA.md for the full contract."""
    raw = pl.read_csv(path)
    columns = {c.lower().strip(): c for c in raw.columns}
    required = ("ticker", "price", "as_of")
    missing = [c for c in required if c not in columns]
    if missing:
        raise SystemExit(
            f"{path}: missing required column(s) {missing}. Expected header "
            "ticker,price,ma_200,market_cap,as_of -- see PRICES_SCHEMA.md"
        )

    prices = raw.select(
        pl.col(columns["ticker"]).str.to_uppercase().alias("ticker"),
        pl.col(columns["price"]).cast(pl.Float64).alias("price"),
        pl.col(columns["ma_200"]).cast(pl.Float64).alias("ma_200")
        if "ma_200" in columns
        else pl.lit(None, dtype=pl.Float64).alias("ma_200"),
        pl.col(columns["market_cap"]).cast(pl.Float64).alias("market_cap_supplied")
        if "market_cap" in columns
        else pl.lit(None, dtype=pl.Float64).alias("market_cap_supplied"),
        pl.col(columns["as_of"]).str.to_date(strict=False).alias("as_of"),
    )

    missing_as_of = prices.filter(pl.col("as_of").is_null())
    if missing_as_of.height:
        raise SystemExit(
            f"{path}: {missing_as_of.height} row(s) have an unparseable or "
            "missing as_of date (required, ISO format YYYY-MM-DD)."
        )

    dupes = prices.filter(pl.col("ticker").is_duplicated())["ticker"].unique().to_list()
    if dupes:
        raise SystemExit(
            f"{path}: duplicate ticker(s) {sorted(dupes)} -- ambiguous, fix "
            "the file rather than have screen.py guess which row is current."
        )

    unmatched = prices.filter(~pl.col("ticker").is_in(list(known_tickers)))
    if unmatched.height:
        unmatched.drop("market_cap_supplied").write_csv(unmatched_out)
        print(
            f"  {unmatched.height} price row(s) matched no company in "
            f"gate0.csv -> {unmatched_out}"
        )

    stale_cutoff = date.today() - timedelta(days=PRICE_STALE_DAYS)
    stale = prices.filter(pl.col("as_of") < stale_cutoff)
    if stale.height:
        print(
            f"  WARNING: {stale.height} price row(s) have as_of older than "
            f"{PRICE_STALE_DAYS} days"
        )

    return prices.filter(pl.col("ticker").is_in(list(known_tickers)))


def merge_price_frames(frames):
    """One price table from several files, freshest as_of winning per ticker.

    load_prices already rejects a duplicate ticker WITHIN a file; across files
    a duplicate is expected and is resolved rather than rejected, because the
    normal reason to pass two files is that a later fetch refreshed some of
    the same names.
    """
    if not frames:
        return None
    if len(frames) == 1:
        return frames[0]
    combined = pl.concat(frames, how="diagonal_relaxed")
    return (
        combined.sort("as_of", descending=True, nulls_last=True)
        .unique(subset=["ticker"], keep="first")
        .sort("ticker")
    )


def apply_prices(frame, prices):
    """market_cap = price supplied, or price x shares_diluted from the store
    (market_cap_derived=True) when the price file omits it -- the store's
    share count lags buybacks, so a supplied cap is always preferred."""
    stale_cols = [
        c
        for c in (
            "market_cap",
            "pct_vs_200ma",
            "ma_200",
            "price",
            "ev",
            "p_fcf_after_sbc",
            "ev_fcf_after_sbc",
        )
        if c in frame.columns
    ]
    frame = frame.drop(stale_cols).join(prices, on="ticker", how="left")
    frame = frame.with_columns(
        pl.coalesce(
            [pl.col("market_cap_supplied"), pl.col("price") * pl.col("shares_diluted")]
        ).alias("market_cap"),
        (pl.col("market_cap_supplied").is_null() & pl.col("price").is_not_null()).alias(
            "market_cap_derived"
        ),
        pl.when(pl.col("ma_200").is_not_null() & pl.col("price").is_not_null())
        .then((pl.col("price") - pl.col("ma_200")) / pl.col("ma_200"))
        .otherwise(None)
        .alias("pct_vs_200ma"),
    )
    return frame.drop("market_cap_supplied")


def load_eps(path):
    """Optional ticker,fwd_pe,trail_pe CSV for the adjusted-EPS guard."""
    raw = pl.read_csv(path)
    columns = {c.lower().strip(): c for c in raw.columns}
    required = ("ticker", "fwd_pe", "trail_pe")
    missing = [c for c in required if c not in columns]
    if missing:
        raise SystemExit(f"{path}: expected columns ticker,fwd_pe,trail_pe, missing {missing}")
    return raw.select(
        pl.col(columns["ticker"]).str.to_uppercase().alias("ticker"),
        pl.col(columns["fwd_pe"]).cast(pl.Float64).alias("fwd_pe"),
        pl.col(columns["trail_pe"]).cast(pl.Float64).alias("trail_pe"),
    ).unique(subset="ticker")


# --------------------------------------------------------------------------
# Lanes
# --------------------------------------------------------------------------


def apply_lane_filter(frame, lane):
    """The subset each lane draws from before the named screens apply.

    main: no extra filter -- growth already falls back to the 3-year window
    for short_history companies (see apply_growth), so it is not blind to
    spin-offs, it is just not restricted to them either.
    shorthist / ifrs: the dedicated views the spec's hand-run screens were
    (main universe, spin-off lane, IFRS lane).
    inflection: no extra filter; the inflection condition itself (in
    apply_growth) is the filter.
    """
    if lane == "shorthist":
        return frame.filter(pl.col("short_history") == True)  # noqa: E712
    if lane == "ifrs":
        return frame.filter(pl.col("taxonomy") == "ifrs-full")
    return frame


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

STAGE_ORDER = ("eligible", "growth", "margin_expansion", "quality", "band", "momentum")


def run_screen(frame, args, exclude_tickers, prices, eps):
    frame = apply_lane_filter(frame, args.lane)

    eligible, elig_rejects = apply_eligibility(frame, args, exclude_tickers)
    funnel = {"lane_universe": frame.height, "eligible": eligible.height}

    survivors, growth_rejects = apply_growth(eligible, args.lane)
    funnel["growth"] = survivors.height

    survivors, margin_rejects = apply_margin_expansion(survivors)
    funnel["margin_expansion"] = survivors.height

    survivors, quality_rejects = apply_quality(survivors)
    funnel["quality"] = survivors.height

    if prices is not None:
        survivors = apply_prices(survivors, prices)
    pre_band = survivors.height
    survivors, band_rejects = apply_band(survivors, args.min_mktcap, args.max_mktcap)
    funnel["band"] = survivors.height
    funnel["_no_market_cap"] = (
        int((band_rejects["rejected_because"] == "band:market_cap_missing").sum())
        if band_rejects.height
        else 0
    )
    funnel["_pre_band"] = pre_band

    survivors, momentum_rejects = apply_momentum(survivors, prices is not None)
    funnel["momentum"] = survivors.height

    survivors = apply_eps_guard(survivors, eps is not None)
    if eps is not None:
        survivors = survivors.join(eps, on="ticker", how="left")
        survivors = apply_eps_guard(
            survivors.drop("fwd_trail_ratio", "adjusted_eps_guard"), True
        )

    survivors = survivors.with_columns(pl.lit("").alias("rejected_because"))

    all_rejects = [
        batch
        for batch in (
            elig_rejects
            + [growth_rejects, margin_rejects, quality_rejects, band_rejects, momentum_rejects]
        )
        if batch.height
    ]
    reject_cols = set(survivors.columns)
    for i, batch in enumerate(all_rejects):
        missing = [c for c in reject_cols if c not in batch.columns]
        if missing:
            all_rejects[i] = batch.with_columns(
                [pl.lit(None, dtype=survivors.schema[c]).alias(c) for c in missing]
            )

    combined = (
        pl.concat([survivors] + all_rejects, how="diagonal_relaxed")
        if all_rejects
        else survivors
    )
    return combined, funnel


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="data root directory")
    parser.add_argument(
        "--gate0-csv", default=None, help="input gate0.csv (default: <root>/gate0.csv)"
    )
    parser.add_argument("--lane", required=True, choices=LANES)
    parser.add_argument("--out", required=True)
    parser.add_argument("--min-mktcap", type=float, default=None)
    parser.add_argument("--max-mktcap", type=float, default=None)
    parser.add_argument("--min-revenue", type=float, default=DEFAULT_MIN_REVENUE)
    parser.add_argument("--include-financials", action="store_true")
    parser.add_argument("--exclude-sic", default=DEFAULT_EXCLUDE_SIC)
    parser.add_argument(
        "--exclude-tickers", default=None, help="file of tickers/CIKs to drop, one per line"
    )
    parser.add_argument(
        "--price-csv",
        action="append",
        default=None,
        metavar="PATH",
        help="ticker,price,ma_200,market_cap,as_of -- see PRICES_SCHEMA.md. "
        "REPEATABLE: pass it once per file and they are merged, freshest "
        "as_of winning. It used to take a single path, and on 2026-08-14 a "
        "90-row price file sat unused next to the 33-row one the run loaded, "
        "so 90 companies went through the band gate with a null cap.",
    )
    parser.add_argument(
        "--eps-csv",
        default=None,
        help="ticker,fwd_pe,trail_pe (optional, informational only)",
    )
    parser.add_argument("--tickers", default=None, help="ad-hoc exact lookup, e.g. COPA,1345105")
    args = parser.parse_args(argv)

    paths = Paths(args.root).ensure()
    gate0_path = args.gate0_csv or str(paths.gate0)
    frame = pl.read_csv(gate0_path, infer_schema_length=200000)
    if "market_cap" in frame.columns and frame.schema["market_cap"] != pl.Float64:
        # An all-null market_cap column (no price ever joined into gate0.csv)
        # infers as Utf8 from CSV; cast so apply_band's numeric comparison
        # doesn't blow up when --price-csv is not supplied either.
        frame = frame.with_columns(pl.col("market_cap").cast(pl.Float64, strict=False))

    if args.tickers:
        wanted = [t.strip() for t in args.tickers.split(",") if t.strip()]
        matches = [lookup_by_ticker_or_cik(frame, t) for t in wanted]
        result = pl.concat(matches, how="vertical") if matches else frame.filter(pl.lit(False))
        result.write_csv(args.out)
        log_stage("screen:tickers", requested=len(wanted), matched=result.height, out=args.out)
        return 0

    exclude_tickers = load_exclude_tickers(args.exclude_tickers)
    known_tickers = set(frame["ticker"].drop_nulls().to_list())
    prices = None
    if args.price_csv:
        loaded = [
            load_prices(path, known_tickers, str(paths.root / "prices_unmatched.csv"))
            for path in args.price_csv
        ]
        prices = merge_price_frames(loaded)
        if len(loaded) > 1:
            print(
                f"  merged {len(loaded)} price files -> {prices.height:,} tickers "
                "(freshest as_of wins per ticker)"
            )
    eps = load_eps(args.eps_csv) if args.eps_csv else None

    result, funnel = run_screen(frame, args, exclude_tickers, prices, eps)
    result.write_csv(args.out)

    shortlisted = (result["rejected_because"] == "").sum()

    # 🔴 An empty shortlist caused by missing prices must never read like an
    # empty shortlist caused by a picky market. Say which one it was, on the
    # run's own output, every time.
    no_cap = funnel.get("_no_market_cap", 0)
    if no_cap:
        print(
            f"  WARNING: {no_cap:,} of {funnel.get('_pre_band', 0):,} companies that "
            "cleared every growth/margin/quality leg were rejected at the band gate "
            "because they have NO MARKET CAP -- the store has none and the price "
            "file did not cover them. This is a MISSING INPUT, not a screening "
            "result. Supply --price-csv covering them (repeat the flag for several "
            "files) and re-run before concluding anything about the shortlist."
        )
    if not shortlisted and no_cap:
        print(
            "  🔴 SHORTLIST IS EMPTY AND THE CAUSE IS MISSING PRICES. Do not report "
            "this run as 'nothing passed'."
        )
    if "capex_suspect" in result.columns:
        flagged = int(
            result.filter((pl.col("rejected_because") == "") & pl.col("capex_suspect")).height
        )
        if flagged:
            print(
                f"  WARNING: {flagged} shortlisted name(s) carry capex_suspect -- their "
                "FCF, FCF/share and every P/FCF multiple derived from it are unverified. "
                "Check capex against the cash-flow statement before scoring them."
            )

    log_stage(
        "screen:funnel",
        lane=args.lane,
        **{stage: f"{funnel[stage]:,}" for stage in ("lane_universe", "eligible", *STAGE_ORDER[1:])},
    )
    log_stage("screen", lane=args.lane, shortlisted=f"{shortlisted:,}", out=args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
