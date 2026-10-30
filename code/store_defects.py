"""Pre-review store-defect flags (added 2026-10-01).

Every detector here exists because a MODE B review slot was spent finding the
defect by hand, one name at a time. Between 2026-09-08 and 2026-10-01, 34 of
251 discards (13.55%) cited a store error, and each one cost a full slot before
the reviewer could tell the store was wrong. These flags move that finding to
build time, so a slot opens already knowing which store figures not to quote.

🔴 FLAGS ONLY. Nothing here changes a value another column already publishes,
and nothing here feeds gate0_pass, gate0_status or any lane gate. A flag says
"this figure is not what it looks like"; it does not say which way the truth
lies, and correcting through a detector that has only been validated on a
handful of named cases would be the fail-open error this file exists to stop.
The one new VALUE (capex_net) sits beside gross capex and replaces nothing.

Detectors, each with the case that proved it:

  total_debt_partial         UNFI, LII, PLOW, DIT, TMDX. The total_debt chain
                             resolved ONE component (usually LongTermDebtCurrent)
                             and marked it "(partial)". The figure is a FLOOR on
                             debt; net_cash built on it fails in the company's
                             favour. Notes 2026-09-22, 2026-09-28, 2026-10-01.
  capex_gross_of_proceeds    KNX. capex is GROSS; recurring equipment-sale proceeds
                             sit on a separate investing line, so FCF is
                             understated. Fails CLOSED. Note 2026-10-01. Does NOT
                             reach TRN: its lease-fleet proceeds are not on a
                             pure PP&E-sale tag, and a mixed inflow sum cannot be
                             split, so it stays NOT MEASURED rather than guessed.
  shares_history_scale_break BMI (history in thousands), SWBI / RGEN / EVI (FY in
                             thousands vs latest quarter in units). Corrupts every
                             fcf_per_share_* figure. shares_scale_suspect tests the
                             latest period only and reads False on all four.
                             Notes 2026-09-27, 2026-09-29.
  shares_split_basis_break  DECK. A restated FY count jumps at a split while
                             older FY counts remain on the pre-split basis.
  share_count_stale_vs_live RUSHA, IESC. A fresh USD cap and matching price
                             imply materially more shares than FY diluted.
  fy_flows_stale             KR, OLLI, IESC (a whole fiscal year), FERG (153 days,
                             FYE change). period_end comes from balances while the
                             flows on the row are from an older fiscal year, so the
                             standing "check period_end" rule passes and is wrong.
                             Notes 2026-09-30 (both).
  ttm_behind_calendar        VMC, LSTR, CTSH, KNX, LII. The newest period the store
                             holds for the filer ends more than a quarter plus the
                             next report's filing deadline (10-Q or 10-K) before the
                             store's own as-of date, so a later period has been
                             filed and is not in the store. Measured on the newest
                             period, NOT ttm_window_end (AVPT: one stale leg drags
                             the binding window back while the flows are current). US
                             domestic filers only; NULL for 20-F / 40-F filers.
                             Verified 2026-10-01 that the gap is SEC-side, not a
                             build defect: data.sec.gov's own companyconcept API
                             holds no VMC 10-Q after the Q1 one filed 2026-04-29.
                             The flag therefore means "read the latest quarter
                             from the filing itself", not "rebuild the store".
  ttm_side_flows_lagging     OKE. Built in gate0.build_ttm (it needs the per-concept
                             windows); summarised here.
  latest_q_stale             SBLK. The latest quarterly balance sheet is more than
                             200 days behind ttm_window_end (a 2021 6-K beside a
                             2026 TTM). Built in gate0.add_latest_q_staleness,
                             which also nulls those balances; registered here so
                             the row says why latest_q_* is blank.
  revenue_subset_suspect     SEZL, RUSHA. The filer reports two side-by-side
                             revenue totals that disagree materially on the
                             latest fiscal year (the ASC 606 contract-revenue
                             subset beside the broader Revenues), so the
                             revenue figure on the row was at risk of being
                             the subset. The value is now the larger total --
                             fixed at the source; this flag says the filer's
                             tags disagreed, nothing more. Reader-only.
  revenue_subset_history_suspect  IONQ, Biora. A PRIOR fiscal year inside the
                             trend window (revenue_cagr_3y/5y,
                             operating_margin_2y_ago) carries a side-by-side
                             disagreement of 10x or more -- the mis-tag shape.
                             An inflated base year shrinks base-year operating
                             margin, so margin-expansion legs read BETTER than
                             truth: fail-open, hence FAIL_OPEN_DEFECTS. The
                             latest-year twin stays with revenue_subset_suspect.

``store_defect_flags`` lists every detector that fired, comma-joined, so the
queue ranker and a human reader see one column rather than six.
"""

from __future__ import annotations

import polars as pl

# A one-year flow is 330-400 days; anything else tagged FY is a transition or
# stub period and must not be read as a fiscal year.
FY_DAYS = (330, 400)

# capex_gross_of_proceeds: the note's proposal, unchanged. Proceeds at or above
# 15% of gross capex in at least 3 of the last 5 fiscal years is a recurring
# fleet-sale business, not a one-off disposal.
PROCEEDS_SHARE_OF_CAPEX = 0.15
PROCEEDS_MIN_YEARS = 3
PROCEEDS_LOOKBACK_YEARS = 5

# Only these tags are PP&E-sale proceeds. investing_inflows is a component SUM
# (divestitures, insurance, grants ride in it too), so proceeds are published
# only when EVERY component on the period is in this set -- LII's
# PP&E+Divestiture sum cannot be split and is reported as NOT MEASURED.
PPE_SALE_TAGS = frozenset(
    {
        "ProceedsFromSaleOfPropertyPlantAndEquipment",
        "ProceedsFromSaleOfProductiveAssets",
        "ProceedsFromSaleOfOtherPropertyPlantAndEquipment",
        "ProceedsFromSaleOfMachineryAndEquipment",
        "ProceedsFromSalesOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    }
)

# A share count that moves 100x in a year is a units error or a split large
# enough that every per-share series has to be re-read either way.
SHARES_SCALE_BREAK_RATIO = 100.0
SHARES_LOOKBACK_YEARS = 6  # the 5-year CAGR needs six fiscal years of counts
# A 1.5x threshold caught RBA/CLVT merger issuance and recent IPOs. DECK's
# 6-for-1 discontinuity is 5.76x after buybacks; require a larger break.
SHARES_SPLIT_MIN_RATIO = 3.75
SHARE_COUNT_LIVE_GAP = 0.25
CAP_PRICE_PAIR_MAX_DAYS = 4

# FERG's FYE change opened a 153-day gap; the earlier 300-day proposal missed it.
FY_FLOWS_STALE_DAYS = 90

# One quarter (~92 days) plus the filing deadline of the NEXT report. That report
# is a 10-Q (45 days for the slowest filer class) unless the window ended on the
# last interim before a fiscal year-end, when it is a 10-K (90 days). Measured
# against the store's own as-of date: a window older than this means a later
# period has been filed and is not in the store. UNFI (window ending three months
# before its August year-end, 10-K not yet due) must NOT fire; VMC (March window,
# June 10-Q filed in July) must.
TTM_CALENDAR_MAX_DAYS_10Q = 137
TTM_CALENDAR_MAX_DAYS_10K = 182
# A window ending within this many days of the next fiscal year-end is the Q3
# window; its successor is the annual report.
NEXT_IS_ANNUAL_WITHIN_DAYS = 100
# Only US domestic filers have a quarterly XBRL calendar to be behind. A 20-F /
# 40-F filer's interims are 6-Ks with no XBRL, so its TTM is FY-based by design.
QUARTERLY_FILER_FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A")

# revenue_subset_suspect: the relative gap between the shipped revenue (the
# side-by-side MAX after the 2026-10-29 fix) and the smallest disagreeing
# sibling total above which the disagreement is material. RUSHA's gap is
# 5.0%, SEZL's 47.6%; both are named cases the flag exists to catch.
REVENUE_SUBSET_TOLERANCE = 0.01

# revenue_subset_history_suspect: the max/min ratio between a prior year's
# side-by-side revenue totals above which the year is treated as resting on
# a disputed tag. Measured on 2,684 FY candidate rows (2026-10-22 archive):
# the distribution is CONTINUOUS -- 1-1.5x: 1,616 rows, 2-3x: 92, 5-10x: 195,
# 10-50x: 282, >50x: 183 -- so no threshold separates the mis-tag shape from
# a genuine large gap (AER's real lease-revenue total sits at 396-463x). 10x
# is a precision choice, not a natural break: the named mis-tags (IONQ 111x,
# Biora 458x) sit far above it, 80% of candidate rows sit below it, and the
# flag's wording ("rests on a disputed tag -- re-derive by hand") is true of
# every row it catches, AER and OSCR included.
REVENUE_HISTORY_SUSPECT_RATIO = 10.0
# The window the flag protects: the years feeding revenue_cagr_3y,
# revenue_cagr_5y and operating_margin_2y_ago (CAGR_LONG_YEARS in gate0),
# EXCLUDING the latest year -- that one is revenue_subset_suspect's question.
REVENUE_HISTORY_WINDOW_YEARS = 5

DEFECT_COLUMNS = (
    "total_debt_partial",
    "capex_gross_of_proceeds",
    "shares_history_scale_break",
    "shares_split_basis_break",
    "fy_flows_stale",
    "ttm_behind_calendar",
    "latest_q_stale",
    "revenue_subset_suspect",
    "revenue_subset_history_suspect",
)

# The subset that can make a Gate 0 or growth figure read BETTER than the truth
# or unknowable. The queue ranker demotes on these. total_debt_partial (a
# valuation floor) and capex_gross_of_proceeds (fails closed) are informational.
FAIL_OPEN_DEFECTS = (
    "shares_history_scale_break",
    "shares_split_basis_break",
    "share_count_stale_vs_live",
    "fy_flows_stale",
    "ttm_behind_calendar",
    "ttm_side_flows_lagging",
    "revenue_subset_history_suspect",
)


def _latest_filed(facts):
    """One value per (cik, concept, period_end): the most recently filed."""
    return (
        facts.sort(["cik", "concept", "period_end", "filed"])
        .group_by(["cik", "concept", "period_end"], maintain_order=True)
        .last()
    )


def _annual(facts, concept):
    rows = facts.filter(
        (pl.col("concept") == concept)
        & (pl.col("fiscal_period") == "FY")
        & pl.col("period_start").is_not_null()
    ).with_columns(
        (pl.col("period_end") - pl.col("period_start")).dt.total_days().alias("_days")
    )
    rows = rows.filter(pl.col("_days").is_between(*FY_DAYS))
    return _latest_filed(rows)


def _fy_flows_end(facts):
    """Latest one-year OCF period per filer: the fiscal year the FLOWS describe."""
    return _annual(facts, "ocf").group_by("cik").agg(
        pl.col("period_end").max().alias("fy_flows_period_end")
    )


def _total_debt_partial(facts, frame):
    """True when the debt figure actually on the row is a single component.

    Read against the SAME periods the row publishes: the FY fact at period_end
    and, separately, the latest quarterly fact. Either being partial makes the
    corresponding published figure a floor.
    """
    debt = facts.filter(pl.col("concept") == "total_debt")
    partial = pl.col("source_tag").str.ends_with("(partial)")
    fy = _latest_filed(debt.filter(pl.col("fiscal_period") == "FY")).select(
        "cik", "period_end", partial.alias("_fy_partial")
    )
    q = (
        debt.filter(pl.col("fiscal_period") != "FY")
        .sort(["cik", "period_end", "filed"])
        .group_by("cik")
        .last()
        .select("cik", partial.alias("_q_partial"))
    )
    keyed = frame.select("cik", "period_end").join(fy, on=["cik", "period_end"], how="left")
    keyed = keyed.join(q, on="cik", how="left")
    measured = pl.col("_fy_partial").is_not_null() | pl.col("_q_partial").is_not_null()
    return keyed.select(
        "cik",
        pl.when(measured)
        .then(pl.col("_fy_partial").fill_null(False) | pl.col("_q_partial").fill_null(False))
        .otherwise(None)
        .alias("total_debt_partial"),
    )


def _ppe_proceeds(facts, flows_end):
    """PP&E-sale proceeds per fiscal year, published only when unambiguous."""
    inflows = _annual(facts, "investing_inflows").with_columns(
        pl.col("source_tag")
        .str.replace(r" \(partial\)$", "")
        .str.split("+")
        .list.eval(pl.element().is_in(list(PPE_SALE_TAGS)))
        .list.all()
        .alias("_pure_ppe")
    )
    proceeds = inflows.filter(pl.col("_pure_ppe")).select(
        "cik", "period_end", pl.col("value").alias("_proceeds")
    )
    capex = _annual(facts, "capex").select("cik", "period_end", pl.col("value").alias("_capex"))
    years = capex.join(proceeds, on=["cik", "period_end"], how="inner").join(
        flows_end, on="cik", how="inner"
    )
    window = years.filter(
        (pl.col("period_end") <= pl.col("fy_flows_period_end"))
        & (
            pl.col("period_end")
            > pl.col("fy_flows_period_end").dt.offset_by(f"-{PROCEEDS_LOOKBACK_YEARS}y")
        )
        & (pl.col("_capex") > 0)
    )
    recurring = window.group_by("cik").agg(
        ((pl.col("_proceeds") / pl.col("_capex")) >= PROCEEDS_SHARE_OF_CAPEX)
        .sum()
        .alias("_years_material")
    )
    latest = years.filter(pl.col("period_end") == pl.col("fy_flows_period_end")).select(
        "cik",
        pl.col("_proceeds").alias("ppe_sale_proceeds"),
        pl.when(pl.col("_proceeds") >= 0)
        .then(pl.col("_capex") - pl.col("_proceeds"))
        .otherwise(None)
        .alias("capex_net"),
    )
    return recurring.join(latest, on="cik", how="full", coalesce=True).select(
        "cik",
        "ppe_sale_proceeds",
        "capex_net",
        (pl.col("_years_material").fill_null(0) >= PROCEEDS_MIN_YEARS).alias(
            "capex_gross_of_proceeds"
        ),
    )


def _shares_scale_break(facts, frame):
    """A >100x jump in the FY share series, or between FY and latest quarter."""
    shares = _annual(facts, "shares_diluted").select("cik", "period_end", "value")
    anchor = frame.select("cik", pl.col("period_end").alias("_anchor"))
    hist = (
        shares.join(anchor, on="cik", how="inner")
        .filter(
            (pl.col("period_end") <= pl.col("_anchor"))
            & (
                pl.col("period_end")
                > pl.col("_anchor").dt.offset_by(f"-{SHARES_LOOKBACK_YEARS}y")
            )
            & (pl.col("value") > 0)
        )
        .sort(["cik", "period_end"])
        .with_columns((pl.col("value") / pl.col("value").shift(1).over("cik")).alias("_r"))
    )
    lo, hi = 1.0 / SHARES_SCALE_BREAK_RATIO, SHARES_SCALE_BREAK_RATIO
    hist_break = hist.group_by("cik").agg(
        ((pl.col("_r") > hi) | (pl.col("_r") < lo)).any().alias("_hist")
    )
    out = frame.select("cik", "shares_diluted", "latest_q_shares_diluted").join(
        hist_break, on="cik", how="left"
    )
    ratio = pl.when(
        (pl.col("shares_diluted") > 0) & (pl.col("latest_q_shares_diluted") > 0)
    ).then(pl.col("latest_q_shares_diluted") / pl.col("shares_diluted"))
    q_break = (ratio > hi) | (ratio < lo)
    measured = pl.col("_hist").is_not_null() | ratio.is_not_null()
    return out.select(
        "cik",
        pl.when(measured)
        .then(pl.col("_hist").fill_null(False) | q_break.fill_null(False))
        .otherwise(None)
        .alias("shares_history_scale_break"),
    )


def _shares_split_basis_break(facts, frame):
    """DECK: an adjacent FY count jumps after restatement, below the 100x scale test."""
    shares = _annual(facts, "shares_diluted")
    if "restated" not in shares.columns:
        shares = shares.with_columns(pl.lit(None, dtype=pl.Boolean).alias("restated"))
    anchor = frame.select("cik", pl.col("period_end").alias("_anchor"))
    hist = (
        shares.select("cik", "period_end", "value", "restated")
        .join(anchor, on="cik", how="inner")
        .filter(
            (pl.col("period_end") <= pl.col("_anchor"))
            & (pl.col("period_end") > pl.col("_anchor").dt.offset_by(f"-{SHARES_LOOKBACK_YEARS}y"))
            & (pl.col("value") > 0)
        )
        .sort(["cik", "period_end"])
        .with_columns(
            (pl.col("value") / pl.col("value").shift(1).over("cik")).alias("_ratio"),
            (pl.col("period_end") - pl.col("period_end").shift(1).over("cik"))
            .dt.total_days().alias("_year_gap"),
        )
    )
    ratio = pl.col("_ratio")
    split = (
        pl.col("restated").fill_null(False)
        & pl.col("_year_gap").is_between(*FY_DAYS)
        & (((ratio >= SHARES_SPLIT_MIN_RATIO) & (ratio < SHARES_SCALE_BREAK_RATIO))
           | ((ratio <= 1 / SHARES_SPLIT_MIN_RATIO) & (ratio > 1 / SHARES_SCALE_BREAK_RATIO)))
    )
    # One FY count cannot establish agreement; only an actual adjacent pair can.
    breaks = hist.filter(pl.col("_year_gap").is_between(*FY_DAYS)).group_by("cik").agg(
        split.any().alias("shares_split_basis_break"),
        pl.when(split).then(ratio).otherwise(None).drop_nulls().first()
        .alias("shares_split_basis_ratio"),
    )
    return frame.select("cik").join(breaks, on="cik", how="left")


def add_share_count_live_check(frame):
    """RUSHA/IESC: compare FY diluted shares with fresh USD cap / price.

    screen.load_prices has already dropped stale or non-USD caps. Re-check pair
    provenance here so direct callers cannot silently treat missing as a pass.
    """
    optional = {"market_cap_supplied": pl.Float64, "price": pl.Float64,
                "as_of": pl.Date, "market_cap_fetched_at": pl.String,
                "market_cap_currency": pl.String, "filing_form": pl.String,
                "shares_diluted": pl.Float64, "store_defect_flags": pl.String}
    for name, dtype in optional.items():
        if name not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(name))
    fetched = (
        pl.col("market_cap_fetched_at").cast(pl.String)
        .str.slice(0, 10).str.to_date(strict=False)
    )
    as_of = pl.col("as_of").cast(pl.String).str.to_date(strict=False)
    cap = pl.col("market_cap_supplied").cast(pl.Float64, strict=False)
    price = pl.col("price").cast(pl.Float64, strict=False)
    shares = pl.col("shares_diluted").cast(pl.Float64, strict=False)
    valid = (
        (cap > 0) & (price > 0) & (shares > 0)
        & (pl.col("market_cap_currency") == "USD")
        & pl.col("filing_form").is_in(QUARTERLY_FILER_FORMS)
        & ((fetched - as_of).dt.total_days().abs() <= CAP_PRICE_PAIR_MAX_DAYS)
    ).fill_null(False)
    frame = frame.with_columns(
        pl.when(valid).then(cap / price).otherwise(None).alias("live_share_count")
    ).with_columns(
        (pl.col("live_share_count") / shares - 1).alias("share_count_live_gap")
    ).with_columns(
        (pl.col("share_count_live_gap").abs() > SHARE_COUNT_LIVE_GAP)
        .alias("share_count_stale_vs_live")
    )
    flags = pl.col("store_defect_flags").fill_null("")
    token = "share_count_stale_vs_live"
    return frame.with_columns(
        pl.when(pl.col(token).fill_null(False) & ~flags.str.contains(token, literal=True))
        .then(pl.when(flags == "").then(pl.lit(token)).otherwise(flags + "," + token))
        .otherwise(flags).alias("store_defect_flags")
    )


def _revenue_subset_suspect(frame):
    """True when the latest-year revenue had a materially smaller sibling total.

    Reads the revenue_subset_candidate diagnostic column (the figure
    first-match-wins would have published; null unless two side-by-side
    totals disagreed). A null candidate is a measured non-disagreement --
    False, not null. A null REVENUE means nothing was tested, and the flag
    is null with it: missing is never zero.
    """
    if "revenue_subset_candidate" not in frame.columns:
        return frame.with_columns(
            pl.lit(None, dtype=pl.Boolean).alias("revenue_subset_suspect")
        )
    gap = (pl.col("revenue") - pl.col("revenue_subset_candidate")) / pl.col("revenue")
    return frame.with_columns(
        pl.when(pl.col("revenue").is_null() | (pl.col("revenue") == 0))
        .then(None)
        .otherwise(
            pl.col("revenue_subset_candidate").is_not_null()
            & (gap > REVENUE_SUBSET_TOLERANCE)
        )
        .alias("revenue_subset_suspect")
    )


def _revenue_subset_history_suspect(facts, frame):
    """True when a PRIOR fiscal year in the trend window rests on a disputed tag.

    IONQ (FY2022: revenue 1,235.0M beside a subset candidate of 11.131M) and
    Biora (2020: 74.3M beside 162k) carry SPAC-era mis-tags in their history.
    The W3 max rule fixes the latest year; the poisoned base year remains,
    and because it shrinks base-year operating margin, the margin-expansion
    legs (operating_margin_2y_ago, the margin2y lane, inflection) read BETTER
    than truth -- the fail-open direction, so this sits in FAIL_OPEN_DEFECTS
    for queue demotion. The window is latest-5 .. latest-1: the years feeding
    revenue_cagr_3y/5y and operating_margin_2y_ago. A disagreement on the
    latest year is revenue_subset_suspect's question and does NOT fire this
    flag. FY rows only: the trend columns read nothing else.

    🔴 A FLAG, NOT A CORRECTION. The 10x ratio cannot tell IONQ's mis-tag
    from AER's genuine 462x lease-revenue total (AER/OSCR fire too -- the
    flag says "this history rests on a disputed tag, re-derive by hand",
    which is true of both). NULL when the filer has no FY revenue history at
    all: missing is never zero, and an unmeasured history is not a clean one.
    """
    if "latest_fiscal_year" not in frame.columns:
        return frame.with_columns(
            pl.lit(None, dtype=pl.Boolean).alias("revenue_subset_history_suspect")
        )
    rev = facts.filter(
        (pl.col("concept") == "revenue") & (pl.col("fiscal_period") == "FY")
    ).select("cik", "fiscal_year", "value")
    cand = facts.filter(
        (pl.col("concept") == "revenue_subset_candidate")
        & (pl.col("fiscal_period") == "FY")
    ).select("cik", "fiscal_year", pl.col("value").alias("_candidate"))
    pairs = (
        rev.join(cand, on=["cik", "fiscal_year"], how="inner")
        .join(
            frame.select("cik", "latest_fiscal_year"), on="cik", how="inner"
        )
        .filter(
            (pl.col("fiscal_year") < pl.col("latest_fiscal_year"))
            & (
                pl.col("fiscal_year")
                >= pl.col("latest_fiscal_year") - REVENUE_HISTORY_WINDOW_YEARS
            )
        )
    )
    lo = pl.min_horizontal("value", "_candidate")
    hi = pl.max_horizontal("value", "_candidate")
    fires = ((lo <= 0) & (hi > 0)) | (
        (lo > 0) & (hi / lo >= REVENUE_HISTORY_SUSPECT_RATIO)
    )
    hits = pairs.filter(fires).select("cik").unique().with_columns(
        pl.lit(True).alias("_hit")
    )
    history = rev.select("cik").unique().with_columns(
        pl.lit(True).alias("_has_history")
    )
    return (
        frame.join(history, on="cik", how="left")
        .join(hits, on="cik", how="left")
        .with_columns(
            pl.when(pl.col("_has_history").is_null())
            .then(None)
            .otherwise(pl.col("_hit").fill_null(False))
            .alias("revenue_subset_history_suspect")
        )
        .drop("_has_history", "_hit")
    )


def add_store_defect_flags(frame, facts):
    """Attach the detectors above to the one-row-per-company frame.

    *facts* must be the FULL fact table, diagnostic concepts included --
    investing_inflows is diagnostic and the proceeds detector reads it.
    """
    for needed in ("shares_diluted", "latest_q_shares_diluted", "ttm_window_end"):
        if needed not in frame.columns:
            dtype = pl.Date if needed == "ttm_window_end" else pl.Float64
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(needed))
    if "latest_q_stale" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, dtype=pl.Boolean).alias("latest_q_stale"))
    if "ttm_side_flows_lagging" not in frame.columns:
        frame = frame.with_columns(pl.lit("").alias("ttm_side_flows_lagging"))

    flows_end = _fy_flows_end(facts)
    # The newest period the store holds for each filer, across every concept.
    # NOT ttm_window_end: that is the BINDING window (the earliest end in the
    # FCF chain), so one stale leg -- a lease line, say -- drags it back while
    # OCF, capex and SBC are current. AVPT, 2026-10-01: ttm_window_end read
    # 2025-09-30 with all three tying to Jul-25..Jun-26. The question this flag
    # answers is "has the filer filed a period the store does not have", and
    # only the newest period on file answers it.
    newest = facts.group_by("cik").agg(pl.col("period_end").max().alias("_newest_period"))
    # The store's own as-of: the newest filing it holds. Never the wall clock --
    # a rebuild of an old store must reproduce the same flags.
    as_of = facts.select(pl.col("filed").max()).item()

    frame = (
        frame.join(flows_end, on="cik", how="left")
        .join(_total_debt_partial(facts, frame), on="cik", how="left")
        .join(_ppe_proceeds(facts, flows_end), on="cik", how="left")
        .join(_shares_scale_break(facts, frame), on="cik", how="left")
        .join(_shares_split_basis_break(facts, frame), on="cik", how="left")
        .join(newest, on="cik", how="left")
    )
    gap = (pl.col("period_end") - pl.col("fy_flows_period_end")).dt.total_days()
    ttm_age = (pl.lit(as_of, dtype=pl.Date) - pl.col("_newest_period")).dt.total_days()
    # Days from the TTM window's end to the next fiscal year-end, from the row's
    # own year-end anniversary. 20-100 days means the window is the Q3 window and
    # the next report is the 10-K; anything else, a 10-Q. The 20-day floor keeps
    # a 52/53-week year that ends a few days before the anniversary on the 10-Q
    # side, which is where it belongs.
    since_fy = (pl.col("_newest_period") - pl.col("period_end")).dt.total_days() % 365
    to_next_fy = 365 - since_fy
    allowance = (
        pl.when(to_next_fy.is_between(20, NEXT_IS_ANNUAL_WITHIN_DAYS))
        .then(pl.lit(TTM_CALENDAR_MAX_DAYS_10K))
        .otherwise(pl.lit(TTM_CALENDAR_MAX_DAYS_10Q))
    )
    quarterly_filer = (
        pl.col("filing_form").is_in(list(QUARTERLY_FILER_FORMS))
        if "filing_form" in frame.columns
        else pl.lit(True)
    )
    frame = frame.with_columns(
        pl.col("capex_gross_of_proceeds").fill_null(False),
        # NULL where the flows date is unknown: not measured, not "fresh".
        pl.when(pl.col("fy_flows_period_end").is_null())
        .then(None)
        .otherwise(gap > FY_FLOWS_STALE_DAYS)
        .alias("fy_flows_stale"),
        pl.lit(as_of, dtype=pl.Date).alias("store_as_of"),
        # NULL, not False, for a 20-F/40-F filer or a filer with no facts:
        # there is no quarterly calendar to be behind, so nothing was tested.
        pl.when(pl.col("_newest_period").is_null() | ~quarterly_filer.fill_null(False))
        .then(None)
        .otherwise(ttm_age > allowance)
        .alias("ttm_behind_calendar"),
        pl.col("ttm_side_flows_lagging").fill_null(""),
    ).drop("_newest_period")
    frame = _revenue_subset_suspect(frame)
    frame = _revenue_subset_history_suspect(facts, frame)
    fired = [
        pl.when(pl.col(c).fill_null(False)).then(pl.lit(c)).otherwise(None)
        for c in DEFECT_COLUMNS
    ] + [
        pl.when(pl.col("ttm_side_flows_lagging") != "")
        .then(pl.lit("ttm_side_flows_lagging"))
        .otherwise(None)
    ]
    return frame.with_columns(
        pl.concat_list(fired).list.drop_nulls().list.join(",").alias("store_defect_flags")
    )
