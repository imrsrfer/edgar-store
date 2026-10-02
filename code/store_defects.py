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

DEFECT_COLUMNS = (
    "total_debt_partial",
    "capex_gross_of_proceeds",
    "shares_history_scale_break",
    "fy_flows_stale",
    "ttm_behind_calendar",
)

# The subset that can make a Gate 0 or growth figure read BETTER than the truth
# or unknowable. The queue ranker demotes on these. total_debt_partial (a
# valuation floor) and capex_gross_of_proceeds (fails closed) are informational.
FAIL_OPEN_DEFECTS = (
    "shares_history_scale_break",
    "fy_flows_stale",
    "ttm_behind_calendar",
    "ttm_side_flows_lagging",
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


def add_store_defect_flags(frame, facts):
    """Attach the detectors above to the one-row-per-company frame.

    *facts* must be the FULL fact table, diagnostic concepts included --
    investing_inflows is diagnostic and the proceeds detector reads it.
    """
    for needed in ("shares_diluted", "latest_q_shares_diluted", "ttm_window_end"):
        if needed not in frame.columns:
            dtype = pl.Date if needed == "ttm_window_end" else pl.Float64
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(needed))
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
