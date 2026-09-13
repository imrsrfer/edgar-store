"""Stage 3: compute the Gate 0 quality tests across the whole universe.

    python gate0.py --min-mktcap 500e6 --max-mktcap 5e9 --out gate0.csv
    python gate0.py --tickers MCRI,SKYW,CPRX

Financials (SIC 6000-6799) are excluded by default: for banks, brokers, insurers
and REITs, "FCF" is balance-sheet flow and every FCF multiple is meaningless.
Pass --include-financials to keep them.

Nulls are load-bearing here. A missing input propagates to a null metric and a
null flag, and the company lands in ``data_quality.csv`` rather than quietly
passing. ``gate0_status`` distinguishes pass / fail / unknown for that reason.
"""

from __future__ import annotations

import argparse
import time

import polars as pl

from concepts import QUARTERS, REQUIRED_CONCEPTS
from edgar_lib import Paths, log_stage

# Gate 0 thresholds.
# Capex-integrity thresholds. Capex under 0.5% of revenue while OCF runs
# above 5% of revenue is the shape a mis-extracted capex line takes; it is
# also the shape a genuinely asset-light business takes, so it warns and
# never rejects.
CAPEX_SUSPECT_RATIO = 0.005
CAPEX_SUSPECT_OCF_RATIO = 0.05
# capex below this multiple of D&A. Industry-neutral where capex/revenue is
# not: D&A is the filer's own measure of how fast it consumes its assets, so a
# going concern sits near 1 regardless of sector. Set at 0.25 because across
# 76 post-quality rows the broken names sat at 0.00 and 0.09 and the nearest
# genuine one at 0.29 -- the gap is real, so the threshold is not a guess.
CAPEX_VS_DA_FLOOR = 0.25
# The short margin window. Two years, not three: the point is to catch a turn
# the five-year window cannot see, and a three-year window on a five-year
# comparison is not enough separation to be worth a second column.
MARGIN_SHORT_YEARS = 2
# 🔴 income_quality (OCF/NI) is tested from BELOW only -- Gate 0 asks for
# >= 0.90 and imposes no ceiling. As net income collapses toward zero the
# ratio explodes, so a company whose earnings have nearly vanished scores as
# HIGHER quality than one earning normally. Measured on the 2026-08-25 store,
# the relationship is monotonic and stark: median net margin falls 13.22% ->
# 8.31% -> 4.50% -> 2.05% -> 0.67% across the 0.9-1.5 / 1.5-3 / 3-5 / 5-10 /
# >10 bands, and 83% of the >10 group earns under 2% of revenue. The ratio is
# measuring a vanishing denominator, not cash conversion.
#
# This is the exact MIRROR of the JOYY discard, which was correctly caught at
# 0.14x -- one-off gain carrying the bottom line. Nothing caught 38x, and NOG
# is live on the watchlist with "income quality 38.8x" cited as a STRENGTH in
# the same entry that describes its earnings collapse as a red flag.
INCOME_QUALITY_CEILING = 5.0

# Investing-statement reconciliation: the residual an investing statement fails
# to close by.
#
# 🔴 READ FROM THE GAP BETWEEN THE POPULATIONS, NOT FROM THE CASES.
#
# The first version of this was 2% of investing CF AND $1M, both bars. Neither
# number was derived from anything: a four-point sweep showed the flag count
# moving only 426 -> 403, that was read as robustness, and a round number was
# picked. The sweep varied the PERCENTAGE bar; the ABSOLUTE bar was never taken
# below $250k, so what it suppressed was never visible.
#
# Measured properly on the 2026-09-09 store, the two populations separate
# cleanly, and they separate at ZERO:
#
#     |residual| exactly 0 (<$1) : 1,232 filers   <- 52.7% of the 2,336 evaluable
#     $1 - $1k                   :     7
#     $1k - $100k                :   138
#     $100k - $1M                :   188
#     > $1M                      :   771
#
# An investing statement either closes to the dollar or it does not. The next
# non-zero residual above zero is $21, then $49, then $100 -- so the gap is
# between $0 and $21, and the old $1M bar sat deep INSIDE the non-zero
# population, suppressing 380 filers whose statements demonstrably do not close.
#
# $1,000 is therefore not a tuning parameter, it is ONE REPORTING UNIT: filings
# state cash-flow figures in thousands, so a residual below the smallest number
# a filer can express is rounding rather than a missing leg. It discards only
# the 7 rows in the $1-$1k band.
#
# There is no percentage bar. A percentage of the investing total is not what
# makes a statement fail to close, and adding one only re-admits the arbitrary
# choice this replaced -- on the same store it changed the OMCL-shape catch by
# one filer (214 of 272 against 213 for the absolute bar alone) while
# suppressing hundreds.
INVESTING_RESIDUAL_ABS = 1_000

INCOME_QUALITY_FLOOR = 0.80
SBC_FAIL = 0.15
SBC_WARN = 0.10
EFFECTIVE_TAX_FLOOR = 0.05
ACQUISITION_INTENSITY_WARN = 0.05
INORGANIC_LOOKBACK_YEARS = 3

# Trend windows.
TREND_YEARS = 5
CAGR_SHORT_YEARS = 3
CAGR_LONG_YEARS = 5
INCOME_QUALITY_WINDOW = 3

# Financial-sector SIC range excluded unless --include-financials.
DEFAULT_EXCLUDE_SIC = "6000-6799"

# Implied EPS above which a diluted share count is presumed to be reported in
# thousands (or in a different currency) rather than in shares. See
# shares_scale_suspect in add_flags for the measurement and the controls.
SHARES_SCALE_EPS_CEILING = 1000.0

# Concepts whose chosen XBRL tag is echoed into the output, because these are
# the ones that drive a pass/fail verdict.
TAG_WITNESS_CONCEPTS = (
    "equity",
    "goodwill",
    "intangibles",
    "ocf",
    "capex",
    "sbc",
    "net_income",
    "operating_income",
)

ALL_CONCEPTS = (
    "equity",
    "goodwill",
    "intangibles",
    "total_debt",
    "cash",
    "revenue",
    "net_income",
    "operating_income",
    "ocf",
    "capex",
    "sbc",
    "acquisitions",
    "buybacks",
    "dividends",
    "dep_amort",
    "tax_expense",
    "pretax_income",
    "shares_diluted",
)

# Concepts that DESCRIBE a filing without scoring it. They are joined onto the
# rows the scoring concepts form -- see widen -- so adding one can never change
# which fiscal year a company is judged on.
DIAGNOSTIC_CONCEPTS = (
    "investing_cf",
    "investing_outflows",
    "investing_inflows",
    "investing_portfolio",
)

FLAG_COLUMNS = (
    "fail_tangible_book",
    "fail_income_quality",
    "fail_fcf",
    "fail_sbc",
    "fail_ni_over_oi",
    "fail_tax_anomaly",
)

# fail_* boolean column -> short test name used for the three-state PASS /
# FAIL / NOT_EVALUABLE columns and for gate0_not_evaluable.
TEST_LABELS = {
    "fail_tangible_book": "tangible_book",
    "fail_income_quality": "income_quality",
    "fail_fcf": "fcf",
    "fail_sbc": "sbc",
    "fail_ni_over_oi": "ni_vs_oi",
    "fail_tax_anomaly": "tax_anomaly",
}

# Tests a company must actually pass -- not merely fail to fail -- for
# gate0_pass. A bank with no operating-income subtotal is NOT_EVALUABLE on
# ni_vs_oi, which does not belong in this set: the test genuinely does not
# apply to it, so it should not block a verdict on the tests that do.
LOAD_BEARING_TESTS = ("tangible_book", "income_quality", "fcf")

# Companies that never filed since this year are dead/delisted, not unscored.
DEFAULT_MIN_FISCAL_YEAR = 2024

# Relative disagreement between the cash-flow-tag and balance-sheet-delta
# measures of inorganic growth above which the two are flagged as conflicting.
ACQ_DISAGREEMENT_THRESHOLD = 0.20

# A carried-forward goodwill/intangibles balance older than this (days) is not
# evidence about the scored period; the company stays unresolved rather than
# leaning on a stale number.
CARRY_FORWARD_MAX_AGE_DAYS = 730

# Bases on which goodwill/intangibles were resolved for the latest fiscal year.
# "reported" needs no inference; the other four are explained in
# resolve_goodwill_intangibles.
BASIS_REPORTED = "reported"
BASIS_STRUCTURED_ABSENCE = "structured_absence_asc350"
BASIS_NEVER_ACQUIRED = "never_acquired"
BASIS_CARRIED_FORWARD = "carried_forward"
BASIS_UNRESOLVED = "unresolved"

# 🔴 WHICH BALANCE SHEET THE TANGIBLE-BOOK LEG IS TESTED ON (added 2026-09-10).
#
# The store carries TWO balance-sheet vintages for most filers: the fiscal-year
# one (equity/goodwill/intangibles) and the newer quarterly one
# (latest_q_equity/latest_q_goodwill/latest_q_intangibles). Until this change
# the leg was tested on the FISCAL-YEAR vintage even when the quarterly one was
# months newer and said the opposite -- so any filer that did a large buyback,
# special dividend, recap, acquisition or impairment AFTER its fiscal year end
# cleared the leg on equity that no longer existed. This was never a data gap:
# the right number was already sitting in gate0.csv, in a column nothing tested.
#
# Proven case, TaskUs (TASK): FY2025 (31-Dec-25) tangible book +226.966M ->
# test PASS. Q2'26 (30-Jun-26) tangible book -62.296M. The gap is a $3.65/share
# (~$333M) special dividend declared 2026-02-25 and paid 2026-03-25, funded by
# a new $600M facility -- confirmed against TaskUs's own Q2 2026 release. On the
# 2026-09-09 store, 244 rows flipped sign between the two vintages and 34 of
# them held gate0_pass = true.
#
# 🔴 THE RULE IS "THE LATER VINTAGE", NOT "THE QUARTERLY ONE". A filer whose
# quarterly coverage LAGS its annual filing has a latest_q that is OLDER than
# period_end -- Kimball Electronics (KE), FY ending 30-Jun-26, has its last
# interim at 31-Mar-26. Preferring the quarterly unconditionally would test that
# filer on a STALER balance sheet, which is the same bug pointing the other way.
# So the dates are compared; presence is not enough.
TANGIBLE_BOOK_BASIS_LATEST_Q = "latest_q"
TANGIBLE_BOOK_BASIS_FISCAL_YEAR = "fiscal_year"
TANGIBLE_BOOK_BASIS_NONE = "none"

# Balance-sheet concepts whose latest quarterly period_end defines the quarterly
# vintage date. equity anchors it -- it is the term the leg cannot do without --
# and the others only stand in when equity itself was not tagged that quarter.
QUARTERLY_BALANCE_CONCEPTS = ("equity", "goodwill", "intangibles", "cash", "total_debt")

# 🔴 A TTM WINDOW THAT STOPS SHORT OF THE FISCAL YEAR END (added 2026-09-10).
#
# The rollforward identity FY(prior) - YTD(prior) + YTD(current) requires the
# annual to close BETWEEN the two interims, so a filer whose quarterly coverage
# stops before its own year end gets a TTM window built off the PRIOR fiscal
# year -- a window that is real, internally consistent, and staler than the
# annual figures sitting on the same row.
#
# Kimball Electronics (KE), FY ending 30-Jun-26, last interim 31-Mar-26:
# ttm_ocf 107.904M against FY2026 OCF of 72.267M, because the window
# (Apr-25..Mar-26) straddles the far stronger FY2025 (OCF 183.94M). That made
# ttm_fcf_after_sbc 47.222M and implied 12.7x P/FCF-after-SBC where FY2026's
# 12.547M gives 47.8x -- a 3.8x error in the flattering direction, on a row
# where ttm_unavailable was FALSE and ttm_stale_concepts named only
# `acquisitions`. Nothing distinguished it from a clean row.
#
# TTM_RECENCY_MAX_DAYS does not catch this: it measures the window end against
# the filer's own latest reported period (400-day ceiling), and KE's lag is 91
# days. So the window is published and the shortfall is stated.
#
# 75 days rather than a nominal 91: fiscal quarters are 13 weeks in most
# calendars but 4-4-5 filers run short ones, and the finding here is "the
# window stops materially short of the year end", not "exactly one quarter".
TTM_WINDOW_MISALIGN_MIN_DAYS = 75

# TTM concepts whose window is the one worth publishing: they are the FCF chain
# (ttm_ocf - ttm_capex - ttm_sbc - ttm_lease_payments), which is what every multiple downstream
# rests on. Where they disagree the BINDING one -- the earliest window end --
# is published, because that is the vintage the FCF figure actually has.
TTM_WINDOW_CONCEPTS = ("ocf", "capex", "sbc", "lease_payments")


def parse_sic_ranges(text):
    """Parse "6000-6799,7370" into a list of inclusive (low, high) pairs."""
    ranges = []
    for chunk in (text or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            low, high = chunk.split("-", 1)
            ranges.append((int(low), int(high)))
        else:
            ranges.append((int(chunk), int(chunk)))
    return ranges


def _safe_div(numerator, denominator):
    """Ratio that is null when either side is null or the denominator is zero."""
    return (
        pl.when(denominator.is_null() | numerator.is_null() | (denominator == 0))
        .then(None)
        .otherwise(numerator / denominator)
    )


def widen(facts, period="FY"):
    """One row per (cik, fiscal_year) with a column per concept, plus tag witnesses.

    🔴 DIAGNOSTIC_CONCEPTS ARE JOINED ON, NEVER PIVOTED INTO THE INDEX.
    (Added 2026-09-09.) The row index and its period_end come from the scoring
    concepts alone; the investing_* concepts are then left-joined onto whatever
    rows already exist.

    Adding a concept must not move a company's latest reported year, and this
    function is where it otherwise would. Two mechanisms, both measured when
    the investing_* concepts were added: a (cik, fiscal_year) group with ONLY
    diagnostic facts becomes a brand-new row, and ``period_end.max()`` over a
    group lets a diagnostic fact at a later date drag an existing row forward.
    Between them they moved four filers -- KMB, INTZ, LIFD, EDTK -- off a
    scored fiscal year onto a later interim period where the scoring concepts
    are null, taking three of them from ``fail`` to ``unknown``. A concept
    added to DESCRIBE the data had silently changed which data was scored.
    """
    annual = facts.filter(pl.col("fiscal_period") == period)
    is_diagnostic = pl.col("concept").is_in(DIAGNOSTIC_CONCEPTS)
    scoring = annual.filter(~is_diagnostic)

    values = scoring.pivot(
        on="concept",
        index=["cik", "fiscal_year"],
        values="value",
        aggregate_function="first",
    )
    for concept in ALL_CONCEPTS:
        if concept not in values.columns:
            values = values.with_columns(pl.lit(None, dtype=pl.Float64).alias(concept))

    ends = scoring.group_by(["cik", "fiscal_year"]).agg(
        pl.col("period_end").max().alias("period_end")
    )
    frame = values.join(ends, on=["cik", "fiscal_year"], how="left")

    diagnostics = annual.filter(is_diagnostic)
    if diagnostics.height:
        frame = frame.join(
            diagnostics.pivot(
                on="concept",
                index=["cik", "fiscal_year"],
                values="value",
                aggregate_function="first",
            ),
            on=["cik", "fiscal_year"],
            how="left",
        )
    for concept in DIAGNOSTIC_CONCEPTS:
        if concept not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(concept))

    witnesses = annual.filter(pl.col("concept").is_in(TAG_WITNESS_CONCEPTS)).pivot(
        on="concept",
        index=["cik", "fiscal_year"],
        values="source_tag",
        aggregate_function="first",
    )
    renames = {
        c: f"source_tag_{c}"
        for c in witnesses.columns
        if c not in ("cik", "fiscal_year")
    }
    return frame.join(witnesses.rename(renames), on=["cik", "fiscal_year"], how="left")


def _company_provenance(facts):
    """Per-company taxonomy, filing form and reporting currency.

    The most frequently seen value across a company's whole fact history --
    in practice a company's facts concentrate in one taxonomy/currency, so
    this is just a robust way to pick "the" value without depending on row
    order. reporting_currency is never converted or normalised to USD; it is
    surfaced so a caller comparing figures across companies knows when not to
    take a raw dollar comparison at face value.

    🔴 .sort() BEFORE .first() (added 2026-09-11). ``mode()`` returns every
    most-frequent value in UNSPECIFIED order, so on a tie ``.first()`` picked
    arbitrarily and the answer changed between two runs of identical code.
    Measured: 6 rows moved on taxonomy/filing_form across two consecutive
    builds of the same source. The docstring above already claimed the
    opposite -- "without depending on row order" -- which is exactly the kind
    of claim that survives because nothing checks it.
    A nondeterministic column also defeats the whole-store diff that is
    supposed to catch an unintended relabel before a sync, so this is a
    correctness fix, not tidiness. Lexicographic tie-break: arbitrary, but
    the same arbitrary answer every time.
    """
    return facts.group_by("cik").agg(
        pl.col("taxonomy").mode().sort().first().alias("taxonomy"),
        pl.col("form").mode().sort().first().alias("filing_form"),
        pl.col("unit")
        .filter(pl.col("unit") != "shares")
        .mode()
        .sort()
        .first()
        .alias("reporting_currency"),
    )


def _acq_disagreement(cf_intensity, bs_intensity):
    """True when the cash-flow-tag and balance-sheet-delta signals conflict.

    Both null, or both effectively zero, is agreement (no acquisitions by
    either measure) -- not a disagreement to flag.
    """
    denom = pl.max_horizontal(cf_intensity.abs(), bs_intensity.abs())
    relative_gap = (cf_intensity - bs_intensity).abs() / denom
    return (
        pl.when(cf_intensity.is_null() | bs_intensity.is_null() | (denom == 0))
        .then(False)
        .otherwise(relative_gap > ACQ_DISAGREEMENT_THRESHOLD)
    )


def _classify_annual_sbc(frame, sbc_evidence=None):
    """Attach sbc_ever_reported and the two per-year SBC gap flags.

    🔴 THE EVIDENCE TEST IS FILER-LEVEL, THE FLAGS ARE YEAR-LEVEL. A filer
    that reports SBC in ANY year HAS an SBC line, so a null in a different
    year is a lost tag rather than an absence. Per-year evidence would make
    the same filer assumable in one year and lost in the next, which is
    precisely the mixed convention the push-down exists to remove.

    Consequence, and it is load-bearing: one filer may hold assumed-zero
    years and REPORTED years, but never assumed-zero and window-lost years.

    History window: every FY row this frame holds, which in the pipeline is
    every annual period in facts.parquet for that CIK -- the same rows
    build_trends later reads, so the evidence test can never see a narrower
    history than the trend columns it protects. It cannot see SBC a filer
    reported ONLY in interim periods (widen() takes period="FY"); the
    ttm_sbc leg passed in as sbc_evidence is what covers that case.

    ``sbc > 0``, not ``is_not_null``: a reported zero corroborates the
    absence rather than contradicting it.
    """
    if "sbc" not in frame.columns:
        return frame.with_columns(
            pl.lit(False).alias("sbc_ever_reported"),
            pl.lit(False).alias("sbc_assumed_zero"),
            pl.lit(False).alias("sbc_window_lost"),
        )
    frame = frame.with_columns(
        ((pl.col("sbc").is_not_null()) & (pl.col("sbc") > 0))
        .any()
        .over("cik")
        .alias("sbc_ever_reported")
    )
    if sbc_evidence is not None:
        frame = frame.join(
            sbc_evidence.rename({"sbc_ever_reported": "_external_sbc_evidence"}),
            on="cik",
            how="left",
        ).with_columns(
            (
                pl.col("sbc_ever_reported")
                | pl.col("_external_sbc_evidence").fill_null(False)
            ).alias("sbc_ever_reported")
        ).drop("_external_sbc_evidence")

    gap = pl.col("sbc").is_null()
    return frame.with_columns(
        (gap & ~pl.col("sbc_ever_reported")).fill_null(False).alias("sbc_assumed_zero"),
        (gap & pl.col("sbc_ever_reported")).fill_null(False).alias("sbc_window_lost"),
    )


def _annual_sbc_term(resolve_sbc_zero):
    """The SBC figure fcf_after_sbc subtracts for one fiscal year."""
    if not resolve_sbc_zero:
        return pl.col("sbc")
    return pl.when(pl.col("sbc_assumed_zero")).then(0.0).otherwise(pl.col("sbc"))


def compute_metrics(frame, assume_absent_zero=False, sbc_evidence=None,
                    resolve_sbc_zero=False):
    """Add the Gate 0 ratios to a wide per-year frame.

    Every ratio propagates nulls: a company missing ``sbc`` has unknown
    FCF-after-SBC, not higher FCF.

    🔴 THE SBC RESOLUTION RUNS HERE, PER FISCAL YEAR (moved 2026-09-11). It
    used to run on the latest row only, after the joins. Everything the
    growth screen reads -- fcf_per_share_cagr_3y/5y,
    fcf_per_share_earliest/latest/delta_abs, fcf_inflection,
    fcf_inflection_years -- is assembled from the per-year fcf_per_share this
    function produces, so a latest-row resolution put TWO CONVENTIONS INSIDE
    ONE ROW: a level computed with an SBC term of zero beside a CAGR built
    from years that withheld. That is worse than the annual-vs-TTM asymmetry
    it was meant to close, because both halves sit in the same path and a
    reader comparing a level against its own growth rate cannot see it.

    ``sbc_evidence`` is an optional per-cik frame carrying
    ``sbc_ever_reported``, unioned with what this frame's own history shows.
    It exists because one leg of the discriminator -- a positive ``ttm_sbc``
    -- is built outside this function. Omit it and the test is annual history
    alone, which is what every direct caller and every unit test gets.

    ``resolve_sbc_zero`` is the convention flip and defaults to FALSE. See
    ``resolve_annual_sbc`` for why it does not ship.
    """
    frame = frame.sort(["cik", "fiscal_year"])
    frame = _classify_annual_sbc(frame, sbc_evidence)

    goodwill_raw, intangibles_raw = pl.col("goodwill"), pl.col("intangibles")
    total_debt_raw = pl.col("total_debt")
    goodwill, intangibles, total_debt = goodwill_raw, intangibles_raw, total_debt_raw
    imputed_parts = []
    if assume_absent_zero:
        # Opt-in only. Off by default because an absent tag and a reported zero
        # are different statements, and conflating them manufactures
        # tangible-book passes and flatters net cash. Debt is included here
        # because a genuinely debt-free filer (Monarch Casino, for one) reports
        # no debt tag at all, which strictly leaves net cash unknowable.
        goodwill = goodwill_raw.fill_null(0.0)
        intangibles = intangibles_raw.fill_null(0.0)
        total_debt = total_debt_raw.fill_null(0.0)
        imputed_parts = [
            pl.when(goodwill_raw.is_null()).then(pl.lit("goodwill")).otherwise(None),
            pl.when(intangibles_raw.is_null()).then(pl.lit("intangibles")).otherwise(None),
            pl.when(total_debt_raw.is_null()).then(pl.lit("total_debt")).otherwise(None),
        ]

    # 🔴 Capex integrity guard (added 2026-08-19). A capex figure that is
    # negative, or zero against real revenue, is not a low-capex company -- it
    # is a broken extraction, and it fails OPEN: understated capex overstates
    # FCF, so the very test meant to catch a cash-burning business (fail_fcf)
    # is the test the defect disables. SAH resolved to capex of -$149.9M,
    # which ADDED $150M to its free cash flow.
    #
    # Hard cases null out fcf/fcf_after_sbc, so the existing NOT_EVALUABLE
    # machinery blocks the verdict rather than passing a flattered one -- an
    # unknown FCF is a materially different statement from a large one.
    #
    # capex_suspect is the softer signal and only WARNS. Two independent
    # tests, because the first one alone was not enough:
    #
    #   (a) capex under CAPEX_SUSPECT_RATIO of revenue while OCF runs above
    #       CAPEX_SUSPECT_OCF_RATIO of it. Catches the extreme cases (LRN at
    #       0.02% of revenue).
    #   (b) capex under CAPEX_VS_DA_FLOOR of depreciation & amortisation.
    #       🔴 THIS IS THE SHARPER TEST and it exists because (a) missed
    #       SKYW on the first patched run: an airline reporting $32.0M of
    #       capex against $940M of OCF, whose aircraft purchases are tagged
    #       outside the capex chain entirely. capex/revenue was 0.79% --
    #       above the 0.5% threshold, so nothing fired, and SKYW went
    #       STRAIGHT TO THE TOP of the shortlist at a 4.6x P/FCF.
    #
    #       capex/revenue is industry-dependent and needs a different
    #       threshold for a software firm than for a railroad. capex/D&A is
    #       not: D&A is the company's own statement of how fast it is
    #       consuming its asset base, so the ratio is near 1 for a going
    #       concern in ANY industry. A business replacing its assets at
    #       under a quarter of the rate it depreciates them is either
    #       liquidating or mis-extracted. Measured over 76 post-quality rows
    #       the two populations do not overlap: the broken names sit at 0.00
    #       and 0.09, the next real one at 0.29, and everything genuine
    #       clusters 0.6-1.8.
    #
    # Neither test rejects. This file cannot distinguish a mis-extraction
    # from a genuinely asset-light filer, so it flags for a human to check
    # against the cash-flow statement.
    capex_broken = (pl.col("capex") < 0) | (
        (pl.col("capex") == 0) & pl.col("revenue").is_not_null() & (pl.col("revenue") > 0)
    )
    capex_ratio = _safe_div(pl.col("capex"), pl.col("revenue"))
    # lease_unmeasured: Finance-lease principal payment not captured in financing section.
    # Set where lease_payments is null (unresolved). Blocks FCF calculation.
    lease_unmeasured = pl.col("lease_payments").is_null().fill_null(False)
    # lease_heavy: Set where finance-lease payments >= 0.25 × OCF. Bounded at both ends
    # (near-zero and negative OCF) to avoid spurious ratios. Signals lease-dependent business.
    lease_heavy = _safe_div(pl.col("lease_payments"), pl.col("ocf")) >= 0.25
    ocf_ratio = _safe_div(pl.col("ocf"), pl.col("revenue"))
    capex_vs_da = _safe_div(pl.col("capex"), pl.col("dep_amort"))
    # 🔴 income_quality has no CEILING -- see INCOME_QUALITY_CEILING. This
    # flags the top end and carries the two numbers that tell a reader WHICH
    # of the two causes it is, because they need opposite responses:
    #
    #   NOG  -- op margin 9.9%, net margin 1.57%, ni/oi 15.8%. Both margins
    #           depressed and consistent with each other: a REAL earnings
    #           collapse. The 38.8x is a symptom of it, not a strength.
    #   TSM  -- op margin 45.7%, net margin 1.22%, ni/oi 2.7%. A world-class
    #           operating margin next to a rounding-error net margin is not a
    #           collapse, it is a MIS-EXTRACTED net income (TSMC does not earn
    #           1.2% of revenue). Same symptom, different disease: one needs
    #           diagnosis, the other needs the tag fixed.
    #
    # A healthy operating margin beside a vanishing net margin points to the
    # extraction; both depressed together points to the business. The flag
    # does not try to classify it -- it states both and routes to a human,
    # because guessing which is exactly the Fix 1 mistake.
    frame = frame.with_columns(
        capex_broken.fill_null(False).alias("capex_broken"),
        lease_unmeasured.alias("lease_unmeasured"),
        lease_heavy.fill_null(False).alias("lease_heavy"),
        (
            capex_broken.fill_null(False)
            | (
                capex_ratio.is_not_null()
                & (capex_ratio < CAPEX_SUSPECT_RATIO)
                & ocf_ratio.is_not_null()
                & (ocf_ratio > CAPEX_SUSPECT_OCF_RATIO)
            )
            | (
                pl.col("dep_amort").is_not_null()
                & (pl.col("dep_amort") > 0)
                & capex_vs_da.is_not_null()
                & (capex_vs_da < CAPEX_VS_DA_FLOOR)
            )
        ).alias("capex_suspect"),
        capex_vs_da.alias("capex_vs_dep_amort"),
        # 🔴 SHARE COUNTS ARRIVE IN THOUSANDS UNDER A UNIT THAT SAYS "shares".
        # (Added 2026-09-08.) HUB GROUP tags WeightedAverageNumberOfDiluted...
        # as 61,104 with unit="shares"; the truth is 61,104 THOUSAND. The unit
        # field cannot discriminate -- all 218,668 shares_diluted facts in the
        # store declare "shares" -- so scale has to be inferred from what the
        # count implies. Measured on the 2026-08-30 store: 53 rows imply an EPS
        # above $1,000 and 33 of them publish a non-null fcf_per_share, which
        # is the MASTER METRIC of the Growth screen (§D#2). McDonald's reads
        # 716 shares and $9,800,390 of FCF per share; Spire 59; Valhi 28.
        #
        # Direction: it UNDERSTATES the denominator, so it OVERSTATES
        # fcf_per_share and understates p_fcf_after_sbc -- it fails OPEN, in
        # the company's favour, exactly like capex, the TTM sums and the
        # dual-class share count before it, and it pulls the affected name UP a
        # cheapness-ranked queue.
        #
        # Negative control, run before trusting the threshold: the genuine
        # high-EPS US filers sit far below it -- SEB $520, NVR $437, BKNG $166,
        # AZO $145 -- so the flag separates a scale error from a real small
        # share count rather than just flagging whoever is expensive.
        #
        # It is a FLAG, not a correction: some hits (PKX, BANCOLOMBIA, GRVY)
        # are a reporting-CURRENCY mismatch rather than a share scale, and the
        # per-share figure is unusable either way. Naming the obligation is
        # right; guessing the factor of 1,000 and silently applying it is not.
        (
            pl.col("shares_diluted").is_not_null()
            & (pl.col("shares_diluted") > 0)
            & pl.col("net_income").is_not_null()
            & (
                pl.col("net_income").abs() / pl.col("shares_diluted")
                > SHARES_SCALE_EPS_CEILING
            )
        ).alias("shares_scale_suspect"),
    )
    capex_usable = pl.when(pl.col("capex_broken")).then(None).otherwise(pl.col("capex"))
    # Null policy: Option (a) — deduct lease_payments where present; set lease_unmeasured=True where null.
    # Rationale: IFRS 16 finance-lease principal payments are financing-section cash flows, not operating.
    # Omitting them overstates FCF by 3-5x for lease-heavy businesses. Where unresolved, flag for manual review.
    lease_usable = pl.when(pl.col("lease_unmeasured")).then(None).otherwise(pl.col("lease_payments"))

    frame = frame.with_columns(
        (pl.col("equity") - goodwill - intangibles).alias("tangible_book"),
        (pl.col("ocf") - capex_usable - lease_usable).alias("fcf"),
        # The SBC term, not the sbc column: on an assumable year under the
        # flip it is a literal zero, and it is pl.col("sbc") -- null and all
        # -- in every other case, which is the shipped behaviour.
        (pl.col("ocf") - capex_usable - lease_usable - _annual_sbc_term(resolve_sbc_zero)).alias(
            "fcf_after_sbc"
        ),
        (pl.col("cash") - total_debt).alias("net_cash"),
        _safe_div(pl.col("ocf"), pl.col("net_income")).alias("income_quality"),
        _safe_div(pl.col("sbc"), pl.col("revenue")).alias("sbc_pct_revenue"),
        _safe_div(pl.col("tax_expense"), pl.col("pretax_income")).alias("effective_tax"),
        _safe_div(pl.col("net_income"), pl.col("operating_income")).alias("ni_vs_oi"),
        _safe_div(pl.col("acquisitions"), pl.col("revenue")).alias("acq_intensity"),
        _safe_div(pl.col("operating_income"), pl.col("revenue")).alias(
            "operating_margin"
        ),
    )

    # Balance-sheet cross-check for inorganic growth: YoY change in reported
    # (never imputed) goodwill + intangibles, independent of the frequently
    # absent acquisitions cash-flow tag. Uses the raw (unfilled) columns even
    # under --assume-absent-zero -- this signal exists to catch understated
    # acquisitiveness, so it must never be flattered by treating an absent
    # figure as zero growth.
    gi_total = goodwill_raw + intangibles_raw
    frame = frame.with_columns(gi_total.alias("_gi_total")).with_columns(
        (pl.col("_gi_total") - pl.col("_gi_total").shift(1).over("cik")).alias(
            "gi_delta"
        )
    )
    frame = frame.with_columns(
        _safe_div(pl.col("gi_delta"), pl.col("revenue")).alias("bs_acq_intensity")
    ).drop("_gi_total")
    frame = frame.with_columns(
        _acq_disagreement(pl.col("acq_intensity"), pl.col("bs_acq_intensity")).alias(
            "acq_disagreement"
        )
    )

    if imputed_parts:
        frame = frame.with_columns(
            pl.concat_list(imputed_parts)
            .list.drop_nulls()
            .list.join(",")
            .alias("imputed_fields")
        )
    else:
        frame = frame.with_columns(pl.lit("").alias("imputed_fields"))

    return frame.with_columns(
        _safe_div(pl.col("buybacks"), pl.col("fcf_after_sbc")).alias("buyback_pct_fcf"),
        _safe_div(pl.col("fcf_after_sbc"), pl.col("shares_diluted")).alias(
            "fcf_per_share"
        ),
        _safe_div(pl.col("net_income"), pl.col("revenue")).alias("net_margin"),
        (
            pl.col("income_quality").is_not_null()
            & (pl.col("income_quality") > INCOME_QUALITY_CEILING)
        )
        .fill_null(False)
        .alias("income_quality_suspect"),
    )


def _add_investing_reconciliation(frame):
    """investing_unreconciled (bool) + investing_residual (signed $).

    The identity is:

        investing_cf + investing_outflows - investing_inflows == 0

    i.e. does the investing statement CLOSE using only tags this pipeline can
    read? A non-zero residual means a leg is reported under a tag we never see
    -- in practice a COMPANY EXTENSION TAG, which the SEC companyfacts archive
    excludes entirely. OMNICELL is the worked case: its external-use software
    development costs ($17.5M FY2025) carry omcl:PaymentsForSoftwareForExternalUse
    and land in the residual, leaving its capex understated by 43%.

    🔴 THIS IS A FLAG, NOT A CORRECTION -- the same posture as capex_suspect,
    shares_scale_suspect and shares_identity_suspect. The residual is a signed
    dollar amount and an obligation to read the filing. It is NOT added to
    capex, and nothing downstream consumes it as a number. A measurement of
    this residual across the universe found it contaminated in BOTH directions
    by parent/component double-counting, and establishing which side is wrong
    needs each filing's calculation linkbase, which is not in the store. So the
    honest output is "this does not close, go and look" -- publishing a
    corrected capex from an arithmetic we know to be unreliable would be the
    opposite of what the measurement supports.

    🔴 NULL, NEVER FALSE, wherever it could not be evaluated. Three ways:

      * the filer reports securities or lending activity (investing_portfolio
        is non-null). Those statements tag a parent total AND its components,
        so a flat sum double-counts -- PayPal's named legs came to +$61.0bn
        against a +$0.80bn investing total. This is the single biggest reason
        the flag abstains, and it is the right abstention: False here would
        assert a reconciliation nobody performed.
      * investing_cf itself is absent -- no total, no identity.
      * reporting_currency is not USD. investing_cf can resolve through the
        any-currency ifrs_chain while the leg components are USD-gated, and a
        residual mixing EUR against USD is arithmetic about nothing.

    Coverage, measured on the 2026-08-25 store: of 272 filers carrying the
    OMNICELL shape (simple statement, PP&E-only capex tag, unexplained
    outflow), the flag catches 137. Of the 135 it misses, 111 carry a
    portfolio tag and go NULL and 24 sit under the tolerance. Half the class,
    stated -- not a detector anyone should mistake for complete.
    """
    for column in ("investing_cf", "investing_outflows", "investing_inflows",
                   "investing_portfolio"):
        if column not in frame.columns:
            # An older store built before these concepts existed. A missing
            # INPUT is not a passing test: the flag goes NULL for every row
            # rather than False, and the residual with it.
            return frame.with_columns(
                pl.lit(None, dtype=pl.Float64).alias("investing_residual"),
                pl.lit(None, dtype=pl.Boolean).alias("investing_unreconciled"),
            )

    # 🔴 Cast before arithmetic. A column that is entirely null carries polars
    # dtype Null, where .abs() raises rather than returning null -- and "every
    # value is null" is exactly the shape of a store built before these
    # concepts existed, i.e. the case this function most needs to survive.
    total = pl.col("investing_cf").cast(pl.Float64, strict=False)
    portfolio = pl.col("investing_portfolio").cast(pl.Float64, strict=False)
    # An absent leg is a line the filer does not have, so it contributes
    # nothing -- but an absent TOTAL is a missing input and voids the test.
    outflows = pl.col("investing_outflows").cast(pl.Float64, strict=False).fill_null(0.0)
    inflows = pl.col("investing_inflows").cast(pl.Float64, strict=False).fill_null(0.0)
    residual = total + outflows - inflows

    evaluable = (
        total.is_not_null()
        & portfolio.is_null()
        & (pl.col("reporting_currency") == "USD")
    )
    residual_evaluable = pl.when(evaluable).then(residual).otherwise(None)
    return frame.with_columns(
        residual_evaluable.alias("investing_residual"),
        pl.when(evaluable)
        .then(
            residual.abs() > INVESTING_RESIDUAL_ABS
        )
        .otherwise(None)
        .alias("investing_unreconciled"),
    )


def add_flags(frame):
    """Attach the boolean Gate 0 tests. A null input yields a null flag.

    fail_fcf tests plain FCF (ocf - capex), the load-bearing claim that a
    company generates real cash. fail_fcf_after_sbc tests the SBC-adjusted
    figure but is informational only: a number of large, old-economy filers
    (Exxon among them) genuinely never tag share-based compensation as a
    cash-flow line, and blocking the whole screen on an absent SBC figure
    would discard companies where OCF and capex are both known and FCF is
    exactly computable. sbc_unverified flags that case for a human to check
    at underwriting time -- it is a warning, not a silent pass.
    """
    return frame.with_columns(
        (pl.col("tangible_book") < 0).alias("fail_tangible_book"),
        (pl.col("income_quality") < INCOME_QUALITY_FLOOR).alias("fail_income_quality"),
        (pl.col("fcf") <= 0).alias("fail_fcf"),
        (pl.col("fcf_after_sbc") <= 0).alias("fail_fcf_after_sbc"),
        pl.col("sbc").is_null().alias("sbc_unverified"),
        (pl.col("sbc_pct_revenue") > SBC_FAIL).alias("fail_sbc"),
        (pl.col("sbc_pct_revenue") > SBC_WARN).alias("warn_sbc"),
        (pl.col("net_income") > pl.col("operating_income")).alias("fail_ni_over_oi"),
        (pl.col("effective_tax") <= EFFECTIVE_TAX_FLOOR).alias("fail_tax_anomaly"),
    )


def _cagr(latest, earliest, years):
    """Compound growth, null unless both endpoints are known and positive."""
    return (
        pl.when(latest.is_null() | earliest.is_null() | (earliest <= 0) | (latest <= 0))
        .then(None)
        .otherwise((latest / earliest) ** (1.0 / years) - 1.0)
    )


def _nth_back(column, offset):
    """Value from ``offset`` fiscal years before the latest, within a cik group."""
    return column.shift(offset).last()


def build_trends(frame):
    """Per-company trend columns: direction matters more than level."""
    frame = frame.sort(["cik", "fiscal_year"])
    latest_year = pl.col("fiscal_year").last()

    trends = frame.group_by("cik").agg(
        latest_year.alias("latest_fiscal_year"),
        pl.col("tangible_book")
        .filter(pl.col("fiscal_year") > latest_year - TREND_YEARS)
        .lt(0)
        .sum()
        .alias("tangible_book_yrs_negative"),
        _cagr(
            pl.col("revenue").last(),
            _nth_back(pl.col("revenue"), CAGR_SHORT_YEARS),
            CAGR_SHORT_YEARS,
        ).alias("revenue_cagr_3y"),
        _cagr(
            pl.col("revenue").last(),
            _nth_back(pl.col("revenue"), CAGR_LONG_YEARS),
            CAGR_LONG_YEARS,
        ).alias("revenue_cagr_5y"),
        _cagr(
            pl.col("fcf_per_share").last(),
            _nth_back(pl.col("fcf_per_share"), CAGR_SHORT_YEARS),
            CAGR_SHORT_YEARS,
        ).alias("fcf_per_share_cagr_3y"),
        _cagr(
            pl.col("fcf_per_share").last(),
            _nth_back(pl.col("fcf_per_share"), CAGR_LONG_YEARS),
            CAGR_LONG_YEARS,
        ).alias("fcf_per_share_cagr_5y"),
        # 🔴 The endpoints the CAGRs actually rest on, published beside them.
        # (Added 2026-09-08.) fcf_per_share_earliest/_latest are series[0] and
        # series[-1] -- the WHOLE history, which for ALG is 7 years, not 5. A
        # 2026-09-08 run read the two pairs as one and reported the row as
        # self-contradictory: earliest $4.59 -> latest $11.34 implies +19.8%
        # against a stored -3.71%. Both numbers were right and the reader was
        # wrong -- the real 5-year base is FY2020's $13.71, and -3.71% is exact.
        # A field that invites that misreading is a defect in the OUTPUT even
        # when the arithmetic behind it is sound, so the base is now in the row.
        _nth_back(pl.col("fcf_per_share"), CAGR_SHORT_YEARS).alias(
            "fcf_per_share_3y_ago"
        ),
        _nth_back(pl.col("fcf_per_share"), CAGR_LONG_YEARS).alias(
            "fcf_per_share_5y_ago"
        ),
        # Whether a fiscal year CAGR_LONG_YEARS back from the latest even
        # exists -- the precise, structural test for "not enough history",
        # using the same shift the CAGRs themselves use. A CAGR can also be
        # null with plenty of history (a negative endpoint 5 years ago is not
        # a data gap), which short_history must not claim.
        _nth_back(pl.col("fiscal_year"), CAGR_LONG_YEARS).is_null().alias(
            "short_history"
        ),
        pl.col("operating_margin").last().alias("operating_margin_latest"),
        _nth_back(pl.col("operating_margin"), CAGR_LONG_YEARS).alias(
            "operating_margin_5y_ago"
        ),
        # 🔴 A TWO-YEAR window as well as the five (added 2026-08-26).
        # The 5-year comparison is structurally blind to a turn that started
        # recently: a company whose margin collapsed four years ago and has
        # been recovering hard for two still reads as "margin down vs 5y ago"
        # and is rejected by the expansion leg. That is the same blind spot
        # growth_basis already documents for spin-offs, one metric over.
        # Coverage is BETTER than the 5-year column, not worse -- 5,150
        # companies have a 2-year window against 3,362 with a 5-years-ago
        # figure -- because two years of history is a far lower bar.
        _nth_back(pl.col("operating_margin"), MARGIN_SHORT_YEARS).alias(
            "operating_margin_2y_ago"
        ),
        pl.col("income_quality")
        .filter(pl.col("fiscal_year") > latest_year - INCOME_QUALITY_WINDOW)
        .mean()
        .alias("income_quality_3y_avg"),
        pl.col("income_quality").last().alias("_iq_latest"),
        _nth_back(pl.col("income_quality"), INCOME_QUALITY_WINDOW - 1).alias(
            "_iq_earliest"
        ),
        pl.col("bs_acq_intensity")
        .filter(pl.col("fiscal_year") > latest_year - INORGANIC_LOOKBACK_YEARS)
        .max()
        .alias("_acq_max_3y"),
        pl.col("acq_disagreement")
        .fill_null(False)
        .filter(pl.col("fiscal_year") > latest_year - INORGANIC_LOOKBACK_YEARS)
        .any()
        .alias("acq_cf_bs_disagreement"),
    )

    return trends.with_columns(
        (pl.col("operating_margin_latest") - pl.col("operating_margin_5y_ago")).alias(
            "operating_margin_delta"
        ),
        (pl.col("_acq_max_3y") > ACQUISITION_INTENSITY_WARN).alias("warn_inorganic"),
        # Real column, not something every caller re-derives: which CAGR
        # window fcf_per_share_cagr actually rests on. The 5-year requirement
        # systematically excludes spin-offs and recent IPOs -- exactly the
        # names where thin coverage most often creates mispricing.
        pl.when(pl.col("fcf_per_share_cagr_5y").is_not_null())
        .then(pl.lit("5y"))
        .when(pl.col("fcf_per_share_cagr_3y").is_not_null())
        .then(pl.lit("3y"))
        .otherwise(pl.lit("insufficient"))
        .alias("growth_basis"),
        pl.when(pl.col("_iq_latest").is_null() | pl.col("_iq_earliest").is_null())
        .then(None)
        .when(pl.col("_iq_latest") > pl.col("_iq_earliest"))
        .then(pl.lit("rising"))
        .when(pl.col("_iq_latest") < pl.col("_iq_earliest"))
        .then(pl.lit("falling"))
        .otherwise(pl.lit("flat"))
        .alias("income_quality_direction"),
    ).drop("_iq_latest", "_iq_earliest", "_acq_max_3y")


def _positive_streak(values):
    """Consecutive years, counting back from the most recent, with fcf/share > 0."""
    streak = 0
    for value in reversed(values):
        if value is None or not (value > 0):
            break
        streak += 1
    return streak


def build_fcf_inflection(annual):
    """FCF-per-share turnaround signals a CAGR is structurally blind to.

    _cagr requires both endpoints positive by design (a negative-to-positive
    move has no defined growth rate, correctly -- round 3 confirmed most of
    the "missing" 5-year CAGRs are exactly this, not a data gap). But that is
    precisely the turnaround category a CAGR screen should never claim to
    rule out. fcf_per_share_delta_abs is defined whenever both endpoints are
    known, positive or not; fcf_inflection flags the negative-to-positive
    case explicitly; fcf_inflection_years is the trailing count of positive
    years, so a single fluke year is visible rather than indistinguishable
    from a sustained turn.
    """
    per_company = (
        annual.sort(["cik", "fiscal_year"])
        .group_by("cik", maintain_order=True)
        .agg(pl.col("fcf_per_share").alias("_fcf_per_share_series"))
    )
    rows = []
    for row in per_company.to_dicts():
        series = row["_fcf_per_share_series"]
        earliest = series[0] if series else None
        latest = series[-1] if series else None
        rows.append(
            {
                "cik": row["cik"],
                "fcf_per_share_earliest": earliest,
                "fcf_per_share_latest": latest,
                "fcf_per_share_delta_abs": (
                    latest - earliest if earliest is not None and latest is not None else None
                ),
                "fcf_inflection": (
                    earliest is not None
                    and latest is not None
                    and earliest < 0
                    and latest > 0
                ),
                "fcf_inflection_years": _positive_streak(series),
            }
        )
    schema = {
        "cik": pl.Int64,
        "fcf_per_share_earliest": pl.Float64,
        "fcf_per_share_latest": pl.Float64,
        "fcf_per_share_delta_abs": pl.Float64,
        "fcf_inflection": pl.Boolean,
        "fcf_inflection_years": pl.Int64,
    }
    return pl.DataFrame(rows, schema=schema)


def latest_rows(frame):
    """The latest reported fiscal year per company."""
    return (
        frame.sort(["cik", "fiscal_year"])
        .group_by("cik")
        .last()
        .rename({"fiscal_year": "latest_fiscal_year"})
    )


def _goodwill_intangibles_history(annual):
    """Per-company filing-history facts needed to resolve a missing latest value.

    Whether goodwill/intangibles were EVER reported, whether any acquisition
    was ever genuinely nonzero, and the most recent reported value of each
    (with its period end, for carry-forward aging).
    """
    return annual.sort(["cik", "fiscal_year"]).group_by("cik", maintain_order=True).agg(
        pl.col("goodwill").is_not_null().any().alias("_ever_goodwill"),
        pl.col("intangibles").is_not_null().any().alias("_ever_intangibles"),
        ((pl.col("acquisitions").is_not_null()) & (pl.col("acquisitions") != 0))
        .any()
        .alias("_ever_nonzero_acquisition"),
        pl.col("goodwill").filter(pl.col("goodwill").is_not_null()).last().alias(
            "_last_goodwill_value"
        ),
        pl.col("period_end").filter(pl.col("goodwill").is_not_null()).last().alias(
            "_last_goodwill_period_end"
        ),
        pl.col("intangibles").filter(pl.col("intangibles").is_not_null()).last().alias(
            "_last_intangibles_value"
        ),
        pl.col("period_end").filter(pl.col("intangibles").is_not_null()).last().alias(
            "_last_intangibles_period_end"
        ),
    )


def _rank_basis(basis_col):
    """Severity rank of a resolution basis: higher is a weaker claim on truth."""
    return (
        pl.when(basis_col == BASIS_UNRESOLVED)
        .then(4)
        .when(basis_col == BASIS_CARRIED_FORWARD)
        .then(3)
        .when(basis_col.is_in([BASIS_NEVER_ACQUIRED, BASIS_STRUCTURED_ABSENCE]))
        .then(2)
        .otherwise(1)
    )


def _resolve_component(raw_value, present, ever_reported, other_present, ever_nonzero_acq, last_value, age):
    """Resolve one of goodwill/intangibles independently, in priority order.

    1. Reported at the scored period end -- nothing to resolve.
    2. Never acquired: this component was never filed in ANY year, and no
       acquisition was ever genuinely nonzero. A company with any nonzero
       acquisition ever recorded is excluded even if this specific tag is
       missing now -- that is a missing tag on a real acquirer, not evidence
       of never having acquired anything.
    3. Structured absence (ASC 350): still absent, but the OTHER component
       (goodwill or intangibles) is reported at the scored date. A filer
       disclosing one but not the other has stated the other is immaterial --
       a reported fact, not an assumption. Gated by the SAME serial-acquirer
       guard as never_acquired: a real acquirer's missing tag is not
       "immaterial," it is a missing tag, and zero-filling it here would be
       the same understatement the guard exists to prevent just reached by a
       different path.
    4. Carry-forward: reported in an earlier year, absent now. The most
       recent value stands in, but only within CARRY_FORWARD_MAX_AGE_DAYS.
    5. Otherwise: unresolved.
    """
    never_acquired = ~present & ~ever_reported & ~ever_nonzero_acq
    structured_absence = ~present & ~never_acquired & other_present & ~ever_nonzero_acq
    carry_ok = (
        ~present
        & ~never_acquired
        & ~structured_absence
        & ever_reported
        & (age <= CARRY_FORWARD_MAX_AGE_DAYS)
    )
    resolved = (
        pl.when(present)
        .then(raw_value)
        .when(never_acquired | structured_absence)
        .then(0.0)
        .when(carry_ok)
        .then(last_value)
        .otherwise(None)
    )
    basis = (
        pl.when(present)
        .then(pl.lit(BASIS_REPORTED))
        .when(never_acquired)
        .then(pl.lit(BASIS_NEVER_ACQUIRED))
        .when(structured_absence)
        .then(pl.lit(BASIS_STRUCTURED_ABSENCE))
        .when(carry_ok)
        .then(pl.lit(BASIS_CARRIED_FORWARD))
        .otherwise(pl.lit(BASIS_UNRESOLVED))
    )
    return resolved, basis, carry_ok


def resolve_goodwill_intangibles(annual, latest):
    """Resolve goodwill and intangibles for the latest fiscal year, independently.

    Goodwill and intangibles are separate line items: a company can genuinely
    have never acquired a business (zero goodwill) while carrying licensed,
    purchased or developed intangibles, or vice versa. Each is resolved on
    its own history via _resolve_component; see that docstring for the
    priority order. The combined resolution_basis (used for reporting) is
    the weaker of the two component bases -- unresolved > carried_forward >
    never_acquired / structured_absence_asc350 > reported.

    Anything left null falls through as unresolved: tangible_book stays null
    and the company remains NOT_EVALUABLE on that test, exactly as under the
    strict default.
    """
    history = _goodwill_intangibles_history(annual)
    frame = latest.join(history, on="cik", how="left")

    equity_present = pl.col("equity").is_not_null()
    goodwill_present = pl.col("goodwill").is_not_null()
    intangibles_present = pl.col("intangibles").is_not_null()
    ever_nonzero_acq = pl.col("_ever_nonzero_acquisition")

    goodwill_age = (pl.col("period_end") - pl.col("_last_goodwill_period_end")).dt.total_days()
    intangibles_age = (
        pl.col("period_end") - pl.col("_last_intangibles_period_end")
    ).dt.total_days()

    resolved_goodwill, basis_goodwill, goodwill_carry_ok = _resolve_component(
        pl.col("goodwill"),
        goodwill_present,
        pl.col("_ever_goodwill"),
        intangibles_present,
        ever_nonzero_acq,
        pl.col("_last_goodwill_value"),
        goodwill_age,
    )
    resolved_intangibles, basis_intangibles, intangibles_carry_ok = _resolve_component(
        pl.col("intangibles"),
        intangibles_present,
        pl.col("_ever_intangibles"),
        goodwill_present,
        ever_nonzero_acq,
        pl.col("_last_intangibles_value"),
        intangibles_age,
    )

    non_blocking_basis = [BASIS_NEVER_ACQUIRED, BASIS_STRUCTURED_ABSENCE]
    imputed_fields = (
        pl.concat_list(
            [
                pl.when(basis_goodwill.is_in(non_blocking_basis))
                .then(pl.lit("goodwill"))
                .otherwise(None),
                pl.when(basis_intangibles.is_in(non_blocking_basis))
                .then(pl.lit("intangibles"))
                .otherwise(None),
            ]
        )
        .list.drop_nulls()
        .list.join(",")
    )
    carried_forward_fields = (
        pl.concat_list(
            [
                pl.when(goodwill_carry_ok).then(pl.lit("goodwill")).otherwise(None),
                pl.when(intangibles_carry_ok).then(pl.lit("intangibles")).otherwise(None),
            ]
        )
        .list.drop_nulls()
        .list.join(",")
    )
    carry_forward_age_days = (
        pl.when(goodwill_carry_ok & intangibles_carry_ok)
        .then(pl.max_horizontal(goodwill_age, intangibles_age))
        .when(goodwill_carry_ok)
        .then(goodwill_age)
        .when(intangibles_carry_ok)
        .then(intangibles_age)
        .otherwise(None)
    )

    frame = frame.with_columns(
        resolved_goodwill.alias("goodwill"),
        resolved_intangibles.alias("intangibles"),
        basis_goodwill.alias("resolution_basis_goodwill"),
        basis_intangibles.alias("resolution_basis_intangibles"),
        imputed_fields.alias("imputed_fields"),
        carried_forward_fields.alias("carried_forward_fields"),
        carry_forward_age_days.alias("carry_forward_age_days"),
    )
    frame = frame.with_columns(
        pl.when(
            equity_present
            & pl.col("goodwill").is_not_null()
            & pl.col("intangibles").is_not_null()
        )
        .then(pl.col("equity") - pl.col("goodwill") - pl.col("intangibles"))
        .otherwise(None)
        .alias("tangible_book")
    )

    rank_goodwill = _rank_basis(pl.col("resolution_basis_goodwill"))
    rank_intangibles = _rank_basis(pl.col("resolution_basis_intangibles"))
    combined_basis = (
        pl.when(~equity_present)
        .then(pl.lit(BASIS_UNRESOLVED))
        .when(rank_goodwill >= rank_intangibles)
        .then(pl.col("resolution_basis_goodwill"))
        .otherwise(pl.col("resolution_basis_intangibles"))
    )
    return frame.with_columns(combined_basis.alias("resolution_basis")).drop(
        "_ever_goodwill",
        "_ever_intangibles",
        "_ever_nonzero_acquisition",
        "_last_goodwill_value",
        "_last_goodwill_period_end",
        "_last_intangibles_value",
        "_last_intangibles_period_end",
    )


def _three_state(flag_col):
    """PASS / FAIL / NOT_EVALUABLE from a nullable boolean fail_ flag.

    A null flag means the inputs needed to run the test were missing -- the
    test did not run, which is a different statement from it passing.
    """
    return (
        pl.when(flag_col.is_null())
        .then(pl.lit("NOT_EVALUABLE"))
        .when(flag_col)
        .then(pl.lit("FAIL"))
        .otherwise(pl.lit("PASS"))
    )


def add_tangible_book_vintage(frame):
    """Re-test the tangible-book leg on the LATER of the two balance sheets.

    See TANGIBLE_BOOK_BASIS_* for why this exists (TASK, and 244 sign flips on
    the 2026-09-09 store). Four columns come out of it:

      tangible_book_latest_q         -- the quarterly-vintage figure. NEW; the
          fiscal-year ``tangible_book`` is untouched, because screen.py and
          several logged watchlist conditions read it by name.
      tangible_book_basis            -- latest_q / fiscal_year / none: which
          vintage ACTUALLY decided the leg on this row.
      tangible_book_vintage_conflict -- the two vintages exist and disagree in
          SIGN, either direction. A flag for the analyst, NOT an automatic
          fail: it speaks the same dialect as capex_suspect and
          ttm_unavailable -- "these two disagree, go look" -- because some hits
          are the FISCAL-YEAR row being the artefact. GPN reads +1.58B FY
          against -23.28B latest-quarter; whatever that is, no rule here gets
          to decide it from a sign.
      fail_tangible_book             -- OVERWRITTEN to test the chosen vintage.
          add_flags already set it from the fiscal year; this is the same test
          on the right balance sheet, so add_verdict's three-state derivation
          and gate0_status need no change at all.

    🔴 goodwill and intangibles fall back to the RESOLVED FISCAL-YEAR figures
    when the quarter does not tag them; equity never does. Measured on the
    2026-09-09 store: of 5,391 rows carrying quarterly equity, 2,505 have no
    quarterly goodwill tag -- but 2,295 of those resolved to a fiscal-year
    goodwill of ZERO (never_acquired / structured ASC 350 absence), where the
    fallback is not inference at all, and 160 are unresolved at the fiscal year
    too, so they stay null and the row falls back to the fiscal-year basis.
    That leaves 50 rows genuinely carrying a goodwill balance one or two
    quarters older than the equity beside it. Requiring all three quarterly
    tags instead would have left 2,834 rows still tested on the stale vintage
    -- the bug -- to spare those 50. Those 50 are identifiable from the shipped
    row: tangible_book_basis is latest_q, latest_q_goodwill is blank, and
    goodwill is not.
    """
    for column in ("latest_q_equity", "latest_q_goodwill", "latest_q_intangibles"):
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    if "latest_q_period_end" not in frame.columns:
        frame = frame.with_columns(
            pl.lit(None, dtype=pl.Date).alias("latest_q_period_end")
        )

    quarterly_goodwill = pl.coalesce("latest_q_goodwill", "goodwill")
    quarterly_intangibles = pl.coalesce("latest_q_intangibles", "intangibles")
    frame = frame.with_columns(
        pl.when(
            pl.col("latest_q_equity").is_not_null()
            & quarterly_goodwill.is_not_null()
            & quarterly_intangibles.is_not_null()
        )
        .then(
            pl.col("latest_q_equity") - quarterly_goodwill - quarterly_intangibles
        )
        .otherwise(None)
        .alias("tangible_book_latest_q")
    )

    # 🔴 DATES, not mere presence. KE's latest interim (31-Mar-26) predates its
    # own fiscal year end (30-Jun-26); preferring the quarterly there would test
    # a STALER balance sheet than the one already on the row.
    quarter_is_newer = (
        pl.col("latest_q_period_end").is_not_null()
        & pl.col("period_end").is_not_null()
        & (pl.col("latest_q_period_end") > pl.col("period_end"))
    )
    # 🔴 AND the fiscal-year vintage must itself be computable. This function
    # changes WHICH balance sheet the leg is tested on; it does not change WHICH
    # ROWS the leg runs on. Dropping this condition lets a quarterly balance
    # sheet make the leg evaluable where the fiscal year could not resolve
    # goodwill or intangibles -- measured at 31 rows on the 2026-09-09 store,
    # all 31 moving from gate0_status `unknown` straight to `pass`. Two reasons
    # not to take that here: it is a different change from the one this fixes,
    # and those rows keep resolution_basis = `unresolved`, so the shipped row
    # would say the tangible-book inputs could not be resolved AND report a
    # tangible-book PASS. A row must not contradict itself. The quarterly figure
    # is still published in tangible_book_latest_q beside a
    # tangible_book_basis of `none`, which is what that opportunity looks like
    # from the outside.
    use_quarter = (
        quarter_is_newer
        & pl.col("tangible_book_latest_q").is_not_null()
        & pl.col("tangible_book").is_not_null()
    )
    tested = (
        pl.when(use_quarter)
        .then(pl.col("tangible_book_latest_q"))
        .otherwise(pl.col("tangible_book"))
    )
    return frame.with_columns(
        # A null on the chosen vintage stays null: NOT_EVALUABLE, never a pass.
        (tested < 0).alias("fail_tangible_book"),
        pl.when(use_quarter)
        .then(pl.lit(TANGIBLE_BOOK_BASIS_LATEST_Q))
        .when(pl.col("tangible_book").is_not_null())
        .then(pl.lit(TANGIBLE_BOOK_BASIS_FISCAL_YEAR))
        .otherwise(pl.lit(TANGIBLE_BOOK_BASIS_NONE))
        .alias("tangible_book_basis"),
        (
            pl.col("tangible_book").is_not_null()
            & pl.col("tangible_book_latest_q").is_not_null()
            & ((pl.col("tangible_book") < 0) != (pl.col("tangible_book_latest_q") < 0))
        ).alias("tangible_book_vintage_conflict"),
    )


def add_ttm_window_alignment(frame):
    """State whether the TTM window reaches the fiscal year sitting beside it.

    See TTM_WINDOW_MISALIGN_MIN_DAYS and the KE case. False here means MEASURED
    AND ALIGNED; a filer with no buildable TTM has null window dates and False,
    which ``ttm_unavailable`` already distinguishes -- the two columns must be
    read together, exactly as ttm_unavailable and ttm_stale_concepts are.
    """
    for column in ("ttm_window_start", "ttm_window_end"):
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Date).alias(column))
    lag_days = (pl.col("period_end") - pl.col("ttm_window_end")).dt.total_days()
    return frame.with_columns(
        (
            pl.col("ttm_window_end").is_not_null()
            & pl.col("period_end").is_not_null()
            & (lag_days >= TTM_WINDOW_MISALIGN_MIN_DAYS)
        ).alias("ttm_window_misaligned")
    )


def add_verdict(frame, allow_imputed=False):
    """Combine the per-test PASS/FAIL/NOT_EVALUABLE results into one verdict.

    gate0_pass depends on the load-bearing tests (tangible_book,
    income_quality, fcf): a NOT_EVALUABLE result there blocks the verdict
    exactly like a FAIL, because an untestable core claim is not a pass. The
    other tests (sbc, ni_vs_oi, tax_anomaly) are reported but do not gate the
    verdict -- some filers (banks, insurers, REITs) genuinely cannot produce
    them, and that is not a defect in the company.

    fcf_after_sbc is a partial exception: its FAIL still blocks a pass (SBC
    materiality is checked whenever it is available, same as always), but its
    NOT_EVALUABLE does not -- see sbc_unverified, which flags that case
    instead of silently passing it.

    A row whose imputed_fields is non-empty can still reach gate0_pass without
    --allow-imputed when it came from resolve_goodwill_intangibles's inference
    from reported facts (structured ASC 350 absence, or a genuine
    no-acquisition history) rather than an assumption. Any other non-empty
    imputed_fields (e.g. from --assume-absent-zero), or a non-empty
    carried_forward_fields, still blocks the verdict by default: a row using
    a carried-forward balance is relabelled pass_stale/fail_stale rather than
    a plain pass/fail either way.
    """
    frame = frame.with_columns(
        [
            _three_state(pl.col(flag_col)).alias(f"test_{name}")
            for flag_col, name in TEST_LABELS.items()
        ]
    )
    has_fcf_after_sbc_test = "fail_fcf_after_sbc" in frame.columns
    if has_fcf_after_sbc_test:
        frame = frame.with_columns(
            _three_state(pl.col("fail_fcf_after_sbc")).alias("test_fcf_after_sbc")
        )

    load_bearing_cols = [f"test_{name}" for name in LOAD_BEARING_TESTS]
    any_fail = pl.any_horizontal([pl.col(c) == "FAIL" for c in load_bearing_cols])
    any_not_evaluable = pl.any_horizontal(
        [pl.col(c) == "NOT_EVALUABLE" for c in load_bearing_cols]
    )
    if has_fcf_after_sbc_test:
        # fcf_after_sbc is load-bearing too, but asymmetrically: a genuine
        # FAIL (SBC is known and FCF-after-SBC is negative) still blocks a
        # pass exactly as before -- SBC materiality is not optional to check
        # once it is available. Only its NOT_EVALUABLE case (sbc_unverified,
        # e.g. Exxon, which never tags SBC as a cash-flow line at all) is
        # exempted, via sbc_unverified rather than silently passing.
        any_fail = any_fail | (pl.col("test_fcf_after_sbc") == "FAIL")

    all_test_cols = [f"test_{name}" for name in TEST_LABELS.values()]
    not_evaluable_list = (
        pl.concat_list(
            [
                pl.when(pl.col(c) == "NOT_EVALUABLE").then(pl.lit(name)).otherwise(None)
                for c, name in zip(all_test_cols, TEST_LABELS.values())
            ]
        )
        .list.drop_nulls()
        .list.join(",")
    )

    frame = frame.with_columns(
        not_evaluable_list.alias("gate0_not_evaluable"),
        (~any_fail & ~any_not_evaluable).alias("_gate0_pass_raw"),
        pl.when(any_fail)
        .then(pl.lit("fail"))
        .when(any_not_evaluable)
        .then(pl.lit("unknown"))
        .otherwise(pl.lit("pass"))
        .alias("gate0_status"),
    )

    if "carried_forward_fields" in frame.columns:
        is_stale = pl.col("carried_forward_fields") != ""
        frame = frame.with_columns(
            pl.when(is_stale & (pl.col("gate0_status") == "pass"))
            .then(pl.lit("pass_stale"))
            .when(is_stale & (pl.col("gate0_status") == "fail"))
            .then(pl.lit("fail_stale"))
            .otherwise(pl.col("gate0_status"))
            .alias("gate0_status")
        )

    if "imputed_fields" in frame.columns:
        # resolve_goodwill_intangibles only ever adds entries to imputed_fields
        # for a NON_BLOCKING_IMPUTATION_BASES basis (never_acquired /
        # structured_absence_asc350); carried-forward values go in the
        # separate carried_forward_fields column instead, gated below. So
        # whenever that resolver ran (signalled by carried_forward_fields
        # being present at all), imputed_fields never needs to block a pass.
        # When it didn't run (e.g. --assume-absent-zero), imputed_fields comes
        # from compute_metrics instead and keeps blocking as before.
        bucket_resolution_ran = "carried_forward_fields" in frame.columns
        non_blocking = pl.lit(bucket_resolution_ran)
        imputed_blocks = (pl.col("imputed_fields") != "") & ~non_blocking
        if "carried_forward_fields" in frame.columns:
            carried_blocks = pl.col("carried_forward_fields") != ""
        else:
            carried_blocks = pl.lit(False)
        blocked = (imputed_blocks | carried_blocks) & pl.lit(not allow_imputed)
        frame = frame.with_columns(
            (pl.col("_gate0_pass_raw") & ~blocked).alias("gate0_pass")
        )
    else:
        frame = frame.with_columns(pl.col("_gate0_pass_raw").alias("gate0_pass"))

    return add_framework_verdict(frame.drop("_gate0_pass_raw"))


# The three legs add_verdict deliberately reports without gating on. Its stated
# reason -- "some filers (banks, insurers, REITs) genuinely cannot produce them"
# -- is sound for the class it names and over-broad for everyone else.
# name -> the flag column that carries it. Spelled out because the flag column
# for ni_vs_oi is fail_ni_OVER_oi, and deriving one from the other by f-string
# is exactly the kind of near-miss that returns an empty result rather than an
# error.
FRAMEWORK_ONLY_LEGS = (
    ("tax_anomaly", "fail_tax_anomaly"),
    ("sbc", "fail_sbc"),
    ("ni_vs_oi", "fail_ni_over_oi"),
)
FRAMEWORK_EXEMPT_SIC = (6000, 6799)  # the class the add_verdict docstring names


def add_framework_verdict(frame):
    """gate0_pass answers a NARROWER question than its name suggests.

    It means "the three load-bearing legs cleared", not "Gate 0 cleared". The
    Framework's Gate 0 also disqualifies a zero-or-negative effective tax rate
    and SBC above 15% of revenue, and add_verdict reports both without gating.

    Measured on the 2026-08-30 store, 6,031 rows: 100 reach gate0_pass=true
    while failing tax_anomaly (55 of them at a zero or negative effective
    rate), 25 while failing sbc -- and 16 of those 25 are software or biotech
    (DDOG 21.9%, LSCC 22.1%, PINS 20.9%, OKTA 18.6%, ...), which the
    banks/insurers/REITs rationale does not reach. screen.py never re-applies
    any of them, so a row that failed a Framework leg travels into a queue slot
    with no surviving marker but the test_ column. GLP arrived that way on
    2026-09-08 at a 1.07% effective tax rate.

    So: keep gate0_pass exactly as it is -- it is load-bearing for the lanes and
    changing it would silently move every historical comparison -- and publish
    the Framework's answer BESIDE it, with the exemption applied to the
    population the rationale was written for and to nobody else.
    """
    if "sic" not in frame.columns:
        return frame
    sic_num = pl.col("sic").cast(pl.Int64, strict=False)
    exempt = sic_num.is_between(*FRAMEWORK_EXEMPT_SIC) & sic_num.is_not_null()
    present = [(col, leg) for leg, col in FRAMEWORK_ONLY_LEGS if col in frame.columns]
    if not present:
        return frame
    any_leg_failed = pl.any_horizontal(
        [pl.col(c).fill_null(False) for c, _ in present]
    )
    failed_list = (
        pl.concat_list(
            [
                pl.when(pl.col(c).fill_null(False)).then(pl.lit(leg)).otherwise(None)
                for c, leg in present
            ]
        )
        .list.drop_nulls()
        .list.join(",")
    )
    return frame.with_columns(
        pl.when(exempt).then(pl.lit("")).otherwise(failed_list).alias(
            "framework_leg_failed"
        ),
        (pl.col("gate0_pass") & (exempt | ~any_leg_failed)).alias(
            "gate0_framework_pass"
        ),
    )


# 🔴 A TTM IS FOUR QUARTERS THAT TILE A YEAR, NOT FOUR ROWS TAGGED "Q".
# (Added 2026-08-27.) The previous build counted rows -- n_quarters == 4 --
# and summed whatever the last four were. For every cash-flow concept that is
# four FIRST quarters from four different fiscal years, because _duration_kind
# in build_facts.py deliberately drops 6- and 9-month year-to-date spans so
# they cannot be mistaken for a quarter, and filers report operating cash flow
# only as YTD. Q1 (a genuine 3-month span) is therefore the ONLY quarter that
# survives for most filers: 113,702 Q1 rows against 22,774 Q2 and 20,644 Q3.
#
# Measured on the 2026-08-27 store: of 91,867 concept-level four-row sums,
# 1,982 tiled a year -- 2.2%. For ocf, 3 of 6,050. ATRO's "TTM OCF" of
# $14.104M was Q1-2023 (-$19.181M) + Q1-2024 ($2.037M) + Q1-2025 ($20.642M)
# + Q1-2026 ($10.606M), a four-YEAR stack of one quarter. FIGS was the same
# shape. Both then tripped ttm_fcf_divergence against a correct FY figure, so
# the flag routed a human to diagnose a business that had not changed.
#
# The row count was never the test. These four bounds are:
TTM_SPAN_DAYS = (330, 400)  # first start to last end must cover ~a year
TTM_TILE_TOLERANCE_DAYS = 10  # summed durations must equal the span: no gap, no overlap

# 🔴 AND VALIDITY IS NOT ADDITIVITY. Four quarters can tile a year perfectly and
# still not be summable. shares_diluted is a weighted AVERAGE share count over
# its period, not a flow: summing four quarters gives four times the average,
# not a trailing-twelve-month count, and it quadruples the denominator of every
# per-share figure built on it. Measured after the tiling fix alone, the
# ttm/FY ratio was still 3.976. Averaging is the correct reduction for a rate.
TTM_AVERAGED_CONCEPTS = frozenset({"shares_diluted", "shares_basic"})


# The rollforward identity. TTM = FY(prior) - YTD(prior) + YTD(current), where
# the two interims are the SAME cumulative span one year apart and the annual
# closed between them. It needs only ONE interim per year, which is why it
# reaches almost every filer where the four-quarter tiling path reaches three.
TTM_INTERIM_PERIODS = ("Q1", "YTD2", "YTD3")  # all cumulative from year start
TTM_SPAN_MATCH_DAYS = 10   # the two interims must be the same length
TTM_YEAR_APART_DAYS = (330, 400)  # ...and one year apart

# 🔴 AND A VALID IDENTITY IS NOT A CURRENT ONE. (Added 2026-09-08.) Every leg of
# the rollforward can validate perfectly and still describe a window that closed
# years before the row it is published on, because nothing in the identity ties
# the window to the filer's most recent reporting date. Measured on the
# 2026-08-30 store: KOHL'S (CIK 885639) published no buyback fact after FY2022,
# so the identity legitimately paired FY2021 (1,355) - YTD3-2021 (807) +
# YTD3-2022 (658) and emitted ttm_buybacks = 1,206,000,000 for a window ending
# 2022-10-29 -- onto a row whose period_end is 2026-01-31, against $5M actually
# repurchased in FY2026. Every validation passed. The number was four years old.
#
# This is the 2026-08-27 lesson in a new place: a test that cannot see the
# defect has not tested anything. The span checks compare the interims to EACH
# OTHER and never to the present, so they return the same answer whether the
# window is current or ancient. A stale window is NOT MEASURED: it is nulled and
# NAMED in ttm_stale_concepts, never published as a trailing-twelve-month figure.
TTM_RECENCY_MAX_DAYS = 400  # window end vs the filer's latest reported period_end


def build_ttm_rollforward(facts):
    """TTM = FY(prior) - YTD(prior) + YTD(current). Additive flow concepts only.

    Every leg is validated rather than assumed: the two interims must match in
    length within TTM_SPAN_MATCH_DAYS and sit TTM_YEAR_APART_DAYS apart, and the
    annual period must CLOSE BETWEEN THEM. That last condition is what makes the
    identity true, and it is checked, not inferred from labels.
    """
    interim = facts.filter(
        pl.col("fiscal_period").is_in(list(TTM_INTERIM_PERIODS))
        & pl.col("period_start").is_not_null()
        & ~pl.col("concept").is_in(list(TTM_AVERAGED_CONCEPTS))
    ).with_columns(
        (pl.col("period_end") - pl.col("period_start")).dt.total_days().alias("_days")
    )
    annual = facts.filter(
        (pl.col("fiscal_period") == "FY") & pl.col("period_start").is_not_null()
    ).select(["cik", "concept", "period_end", "value"])
    if interim.is_empty() or annual.is_empty():
        return pl.DataFrame(schema={"cik": pl.Int64})

    current = (
        interim.sort(["cik", "concept", "period_end"])
        .group_by(["cik", "concept"])
        .last()
        .select(["cik", "concept", "period_end", "_days", "value"])
        .rename({"period_end": "_cur_end", "_days": "_cur_days", "value": "_cur"})
    )
    prior = interim.select(
        ["cik", "concept", "period_end", "_days", "value"]
    ).rename({"period_end": "_pri_end", "_days": "_pri_days", "value": "_pri"})

    paired = (
        current.join(prior, on=["cik", "concept"], how="inner")
        .filter(
            ((pl.col("_cur_days") - pl.col("_pri_days")).abs() <= TTM_SPAN_MATCH_DAYS)
            & ((pl.col("_cur_end") - pl.col("_pri_end")).dt.total_days()
               >= TTM_YEAR_APART_DAYS[0])
            & ((pl.col("_cur_end") - pl.col("_pri_end")).dt.total_days()
               <= TTM_YEAR_APART_DAYS[1])
        )
        .sort(["cik", "concept", "_pri_end"])
        .group_by(["cik", "concept"])
        .last()
    )
    if paired.is_empty():
        return pl.DataFrame(schema={"cik": pl.Int64})

    joined = paired.join(
        annual.rename({"period_end": "_fy_end", "value": "_fy"}),
        on=["cik", "concept"],
        how="inner",
    ).filter(
        # 🔴 The annual must CLOSE BETWEEN the two interims. Without this the
        # identity silently pairs an annual from the wrong year and the result
        # looks like a plausible number.
        (pl.col("_fy_end") > pl.col("_pri_end")) & (pl.col("_fy_end") < pl.col("_cur_end"))
    )
    if joined.is_empty():
        return pl.DataFrame(schema={"cik": pl.Int64})

    out = (
        joined.sort(["cik", "concept", "_fy_end"])
        .group_by(["cik", "concept"])
        .last()
        .with_columns((pl.col("_fy") - pl.col("_pri") + pl.col("_cur")).alias("ttm_value"))
        .select(
            ["cik", "concept", "ttm_value",
             # The rollforward covers (_pri_end, _cur_end]: the prior fiscal
             # year, less the part of it already elapsed at _pri_end, plus the
             # same part of the current one. Stated as the FIRST DAY COVERED so
             # it means exactly what the tiling path's _span_start means.
             pl.col("_pri_end").dt.offset_by("1d").alias("ttm_window_start"),
             pl.col("_cur_end").alias("ttm_window_end")]
        )
    )
    return out


def build_latest_quarter(facts):
    """Most recent quarterly value for BALANCES and for non-additive rates.

    A balance is a snapshot and a weighted-average share count is a rate;
    neither is a trailing sum, and both were previously summed into a ttm_*
    column reading ~4x the truth. They are emitted under a DIFFERENT PREFIX on
    purpose -- a balance must never again be reachable under a name that says
    trailing sum.
    """
    interim = facts.filter(
        pl.col("fiscal_period").is_in(["Q1", "Q2", "Q3", "Q4", "YTD2", "YTD3"])
    )
    wanted = interim.filter(
        pl.col("period_start").is_null()
        | pl.col("concept").is_in(list(TTM_AVERAGED_CONCEPTS))
    )
    if wanted.is_empty():
        return pl.DataFrame(schema={"cik": pl.Int64})
    latest = (
        wanted.sort(["cik", "concept", "period_end"])
        .group_by(["cik", "concept"])
        .last()
        .select(["cik", "concept", "period_end", "value"])
    )
    wide = latest.select(["cik", "concept", "value"]).pivot(
        on="concept", index="cik", values="value", aggregate_function="first"
    )
    wide = wide.rename({c: f"latest_q_{c}" for c in wide.columns if c != "cik"})
    # 🔴 The DATE these balances belong to travels WITH them. Without it nothing
    # downstream can tell whether the quarterly vintage is newer than the
    # fiscal-year one, and "newer" is the entire basis on which
    # add_tangible_book_vintage chooses between the two. A balance published
    # without its date is a number that cannot be compared to anything.
    return wide.join(_quarterly_vintage(latest), on="cik", how="left")


def _quarterly_vintage(latest):
    """The balance-sheet date the ``latest_q_*`` columns describe, per company.

    Anchored on equity: it is the term the tangible-book leg cannot do without,
    so its date is the one that decides the vintage. The other balance concepts
    only stand in when equity itself was not tagged that quarter. Duration
    concepts (a weighted-average share count) are excluded deliberately -- they
    are not balance-sheet instants and must not set a balance-sheet date.
    """
    balance = latest.filter(pl.col("concept").is_in(list(QUARTERLY_BALANCE_CONCEPTS)))
    if balance.is_empty():
        return pl.DataFrame(schema={"cik": pl.Int64, "latest_q_period_end": pl.Date})
    return (
        balance.group_by("cik")
        .agg(
            pl.col("period_end")
            .filter(pl.col("concept") == "equity")
            .max()
            .alias("_equity_end"),
            pl.col("period_end").max().alias("_any_end"),
        )
        .with_columns(
            pl.coalesce("_equity_end", "_any_end").alias("latest_q_period_end")
        )
        .select(["cik", "latest_q_period_end"])
    )


def _ttm_binding_window(recent):
    """One TTM window per company: the BINDING one, not the flattering one.

    Per-concept windows can differ. The one worth publishing is the EARLIEST
    end among the FCF chain (TTM_WINDOW_CONCEPTS), because that is how current
    ``ttm_fcf_after_sbc`` -- the figure every multiple downstream rests on --
    really is. A filer carrying none of those three falls back to the earliest
    end across whatever concepts it does have.

    🔴 Never the latest end. Taking the most recent window on the row is
    precisely how a stale figure comes to read as a current one, which is the
    defect ``ttm_window_misaligned`` exists to state.
    """

    def earliest(rows):
        return (
            rows.sort(["cik", "ttm_window_end", "concept"])
            .group_by("cik")
            .first()
            .select(["cik", "ttm_window_start", "ttm_window_end"])
        )

    fallback = earliest(recent)
    chain = recent.filter(pl.col("concept").is_in(list(TTM_WINDOW_CONCEPTS)))
    if chain.is_empty():
        return fallback
    primary = earliest(chain)
    return pl.concat(
        [primary, fallback.join(primary.select("cik"), on="cik", how="anti")],
        how="vertical_relaxed",
    )


def build_ttm(facts):
    """Trailing-twelve-month sums for additive FLOW concepts.

    Two paths, in priority order:

      1. ROLLFORWARD (primary) -- FY(prior) - YTD(prior) + YTD(current). Needs
         only one interim per fiscal year, so it reaches almost every filer.
      2. TILING (fallback) -- four discrete quarters that genuinely cover a
         year: span inside TTM_SPAN_DAYS AND summed durations equal to that
         span within TTM_TILE_TOLERANCE_DAYS. For filers who really do publish
         discrete quarters.

    Balances and rate concepts are excluded entirely; they belong to
    build_latest_quarter under the latest_q_ prefix.

    🔴 Where neither path holds, the TTM columns are NULL and
    ``ttm_unavailable`` is TRUE. A null here means NOT MEASURED. It does not
    mean measured and fine, and nothing downstream may read it as agreement.
    """
    empty = pl.DataFrame(
        schema={
            "cik": pl.Int64,
            "concept": pl.Utf8,
            "ttm_value": pl.Float64,
            "ttm_window_start": pl.Date,
            "ttm_window_end": pl.Date,
        }
    )
    quarters = facts.filter(
        pl.col("fiscal_period").is_in(["Q1", "Q2", "Q3", "Q4"])
        & pl.col("period_start").is_not_null()
        & ~pl.col("concept").is_in(list(TTM_AVERAGED_CONCEPTS))
    )
    if quarters.is_empty():
        recent = empty
    else:
        last_four = (
            quarters.sort(["cik", "concept", "period_end"])
            .group_by(["cik", "concept"])
            .tail(4)
        )
        recent = (
            last_four.group_by(["cik", "concept"])
            .agg(
                pl.col("value").sum().alias("ttm_value"),
                pl.len().alias("n_quarters"),
                pl.col("period_start").min().alias("_span_start"),
                pl.col("period_end").max().alias("_span_end"),
                (pl.col("period_end") - pl.col("period_start"))
                .dt.total_days()
                .sum()
                .alias("_covered_days"),
            )
            .with_columns(
                (pl.col("_span_end") - pl.col("_span_start"))
                .dt.total_days()
                .alias("_span_days")
            )
            .filter(
                (pl.col("n_quarters") == 4)
                & (pl.col("_span_days") >= TTM_SPAN_DAYS[0])
                & (pl.col("_span_days") <= TTM_SPAN_DAYS[1])
                & (
                    (pl.col("_covered_days") - pl.col("_span_days")).abs()
                    <= TTM_TILE_TOLERANCE_DAYS
                )
            )
            .select(
                ["cik", "concept", "ttm_value",
                 pl.col("_span_start").alias("ttm_window_start"),
                 pl.col("_span_end").alias("ttm_window_end")]
            )
        )

    # 🔴 ROLLFORWARD WINS where both are available. Tiling is the fallback, not
    # the primary path it used to be: on the 2026-08-27 store tiling reached 3
    # companies for ocf and the rollforward reaches 7,749.
    rolled = build_ttm_rollforward(facts)
    if rolled.height:
        recent = pl.concat(
            [
                rolled.select(
                    ["cik", "concept", "ttm_value",
                     "ttm_window_start", "ttm_window_end"]
                ),
                recent.join(
                    rolled.select(["cik", "concept"]),
                    on=["cik", "concept"],
                    how="anti",
                ).select(
                    ["cik", "concept", "ttm_value",
                     "ttm_window_start", "ttm_window_end"]
                ),
            ],
            how="vertical_relaxed",
        )
    if recent.is_empty():
        return pl.DataFrame(schema={"cik": pl.Int64})

    # 🔴 RECENCY GUARD -- see TTM_RECENCY_MAX_DAYS. The reference is the filer's
    # own latest reported period across ALL concepts, so a company that simply
    # stopped reporting one line is caught while a company that stopped filing
    # altogether is not punished twice.
    reference = facts.group_by("cik").agg(
        pl.col("period_end").max().alias("_ref_end")
    )
    recent = recent.join(reference, on="cik", how="left").with_columns(
        (pl.col("_ref_end") - pl.col("ttm_window_end")).dt.total_days().alias("_lag_days")
    )
    is_stale = pl.col("_lag_days") > TTM_RECENCY_MAX_DAYS
    stale = recent.filter(is_stale).select(["cik", "concept"])
    recent = recent.filter(~is_stale.fill_null(False)).select(
        ["cik", "concept", "ttm_value", "ttm_window_start", "ttm_window_end"]
    )
    stale_names = (
        stale.sort(["cik", "concept"])
        .group_by("cik")
        .agg(pl.col("concept").str.join(",").alias("ttm_stale_concepts"))
        if stale.height
        else pl.DataFrame(schema={"cik": pl.Int64, "ttm_stale_concepts": pl.Utf8})
    )
    if recent.is_empty():
        # Every window this filer had was stale. The names still have to travel:
        # a row that lost its whole TTM set to staleness must not look identical
        # to one that never had a TTM at all.
        return stale_names if stale_names.height else pl.DataFrame(schema={"cik": pl.Int64})

    wide = recent.select(["cik", "concept", "ttm_value"]).pivot(
        on="concept", index="cik", values="ttm_value", aggregate_function="first"
    )
    wide = wide.rename({c: f"ttm_{c}" for c in wide.columns if c != "cik"})
    # 🔴 The window these sums actually describe, published beside them. See
    # TTM_WINDOW_MISALIGN_MIN_DAYS: a window can pass every validity check on
    # this function AND still stop short of the fiscal year on the same row, and
    # until the dates are ON the row nothing can tell that case from a clean one.
    wide = wide.join(_ttm_binding_window(recent), on="cik", how="left")
    # STATE the obligation rather than silently nulling -- same pattern as
    # capex_suspect and ttm_unavailable. A named stale concept is a work item.
    # 🔴 FULL join, not left. A filer ALL of whose windows are stale drops out of
    # the pivot entirely, and a left join would then null every ttm_* on that row
    # while leaving ttm_stale_concepts blank -- silently, which is the exact
    # failure this guard exists to prevent, reintroduced one line later. It would
    # also read as ttm_unavailable ("no TTM could be built") when the truth is
    # "a TTM was built and it was out of date". Those are different findings and
    # the store must not conflate them. Caught by verify_fixes.py on 513 rows.
    if stale.height:
        wide = wide.join(
            stale.sort(["cik", "concept"])
            .group_by("cik")
            .agg(pl.col("concept").str.join(",").alias("ttm_stale_concepts")),
            on="cik",
            how="full",
            coalesce=True,
        ).with_columns(pl.col("ttm_stale_concepts").fill_null(""))
    else:
        wide = wide.with_columns(pl.lit("").alias("ttm_stale_concepts"))
    for needed in ("ttm_ocf", "ttm_capex", "ttm_sbc"):
        if needed not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(needed))
    # Same integrity guard as the annual path: a negative TTM capex is a
    # broken extraction, not a capex-free year, and it would flatter
    # ttm_fcf_after_sbc in exactly the same direction.
    #
    # 🔴 The guard must SAY it fired (added 2026-09-11). Nulling alone was
    # silent: ttm_ocf builds fine, so ttm_unavailable reads FALSE, and
    # ttm_stale_concepts names nothing because staleness is not what went
    # wrong. The row then published a raw ttm_capex beside an absent
    # ttm_fcf_after_sbc and nothing on it distinguished "a window was built
    # and one input is unusable" from "a window was built and is clean".
    # 64 rows in the 2026-09-10 store are that shape. Same defect class as
    # the null market cap at the band gate -- a null that reads as clean.
    #
    # A DEDICATED column, not a name appended to ttm_stale_concepts: that
    # column is a list of CONCEPT NAMES whose window was out of date, and a
    # pseudo-concept `capex_negative` inside it would make one column mean
    # two things -- which is the conflation ttm_unavailable and
    # ttm_stale_concepts were split apart to prevent. It is also not
    # ttm_unavailable: a TTM WAS built here, and saying otherwise would lose
    # the ttm_ocf and ttm_revenue that are on the row and usable.
    #
    # 🔴 It states the sign; it does not correct it. build_ttm must not
    # compute through a negative capex -- until the capex chain's sign
    # convention is audited, one is as likely to be an extraction bug as a
    # real disposal, and computing through it would flatter FCF by the full
    # amount in every SAH-shaped case. Routing that to a human is the whole
    # point, exactly as with capex_broken on the annual path.
    ttm_capex_usable = pl.when(pl.col("ttm_capex") < 0).then(None).otherwise(
        pl.col("ttm_capex")
    )
    # TTM lease_payments: null policy matches annual (Option a).
    # Where ttm_lease_payments is null and ttm_ocf exists, the 4-quarter window lacks lease data.
    ttm_lease_usable = pl.when(pl.col("ttm_lease_payments").is_null()).then(None).otherwise(
        pl.col("ttm_lease_payments")
    )
    return wide.with_columns(
        (pl.col("ttm_ocf") - ttm_capex_usable - ttm_lease_usable - pl.col("ttm_sbc")).alias(
            "ttm_fcf_after_sbc"
        ),
        # fill_null(False): an ABSENT ttm_capex is unknown, not negative --
        # the same distinction ttm_unavailable draws for the window itself.
        (pl.col("ttm_capex") < 0).fill_null(False).alias("ttm_capex_negative"),
        # ...and "unknown" is itself a finding worth naming (added 2026-09-11).
        # A window WAS built here and capex is simply not in it, which blanks
        # ttm_fcf_after_sbc on a row where ttm_unavailable reads FALSE and
        # ttm_stale_concepts names nothing -- the largest of the silent
        # populations. Gated on ttm_ocf so it means "the window exists and
        # capex is missing FROM it", not "there is no window", which is
        # ttm_unavailable's statement and must not be duplicated here.
        #
        # 🔴 Never zero-filled. An absent capex zero-filled is a company with
        # no capital expenditure, which overstates FCF by the whole line --
        # the identical failure-open direction capex_broken exists to catch.
        # Mutually exclusive with ttm_capex_negative by construction: a null
        # is not a negative.
        (pl.col("ttm_ocf").is_not_null() & pl.col("ttm_capex").is_null())
        .fill_null(False)
        .alias("ttm_capex_missing"),
    )


def resolve_ttm_sbc(frame):
    """Resolve the TTM SBC gap three ways, and state which one was taken.

    Runs on the JOINED frame, not inside build_ttm, because the deciding
    input is the FISCAL-YEAR ``sbc`` column and that only exists once the
    annual rows and the TTM rows are on the same row. Deriving a second
    "latest FY sbc" inside build_ttm would mean two builders each picking
    their own latest period -- the exact divergence DIAGNOSTIC_CONCEPTS was
    introduced to stop.

    Where a TTM window exists and the SBC term is absent:

      fy sbc > 0        -> WITHHOLD. ``ttm_sbc_window_lost``. The company
          demonstrably pays share-based compensation and the four-quarter
          window lost the line; an SBC term of zero would overstate
          FCF-after-SBC by the whole figure, in the company's favour. That is
          the capex sign-convention trap relocated one column over, and the
          answer is the same one: state it and route it to a human.

      fy sbc null or 0  -> COMPUTE with an SBC term of zero, and SAY SO via
          ``ttm_sbc_assumed_zero``. Nothing is being hidden: the annual row
          agrees there is no SBC to lose. A number of large old-economy
          filers never tag SBC at all, and withholding the master growth
          metric from all of them buys no safety.

    🔴 The assumption is NAMED, never silent -- the posture ``sbc_unverified``
    already set on the annual path. Note the deliberate difference from that
    column: the annual path withholds ``fcf_after_sbc`` whenever FY sbc is
    null, while this recovers the TTM figure in that same case. The
    justification is that only a POSITIVE fy sbc is evidence that an SBC line
    exists to be lost; an absent one is no more informative here than it is
    there, and the flag carries the caveat onto the row either way.

    🔴 An absent or negative ttm_capex is NOT recovered. Only the SBC term is
    ever assumed, and only with the fiscal year's corroboration.
    """
    has_window = pl.col("ttm_ocf").is_not_null()
    capex_usable = pl.col("ttm_capex").is_not_null() & (pl.col("ttm_capex") >= 0)
    sbc_gap = has_window & pl.col("ttm_sbc").is_null()
    fy_sbc_real = pl.col("sbc").is_not_null() & (pl.col("sbc") > 0)
    window_lost = (sbc_gap & fy_sbc_real).fill_null(False)
    # is_null() on the metric is belt and braces: a null ttm_sbc already
    # implies a null ttm_fcf_after_sbc. It is here because this function may
    # only ever turn a NULL into a number -- it must never move one.
    assumed = (
        sbc_gap & ~fy_sbc_real & capex_usable & pl.col("ttm_fcf_after_sbc").is_null()
    ).fill_null(False)
    return frame.with_columns(
        pl.when(assumed)
        .then(pl.col("ttm_ocf") - pl.col("ttm_capex"))
        .otherwise(pl.col("ttm_fcf_after_sbc"))
        .alias("ttm_fcf_after_sbc"),
        assumed.alias("ttm_sbc_assumed_zero"),
        window_lost.alias("ttm_sbc_window_lost"),
    )


def _ttm_sbc_evidence(ttm):
    """The one leg of the SBC evidence test that lives outside the annual frame.

    A filer whose annual sbc tag is missing in every year but whose
    four-quarter sum carries real SBC is reporting it. compute_metrics cannot
    see that -- widen() takes period="FY" -- so the TTM frame is built first
    and this is handed in as ``sbc_evidence``.

    Worth one row on the 2026-09-11 store, and kept anyway: it can only move
    a filer from assumable to withheld, which is the safe direction.
    """
    if ttm is None or "ttm_sbc" not in ttm.columns:
        return None
    return ttm.select(
        "cik",
        (pl.col("ttm_sbc").is_not_null() & (pl.col("ttm_sbc") > 0))
        .fill_null(False)
        .alias("sbc_ever_reported"),
    )


def add_ttm_sbc_evidence_conflict(frame):
    """TTM rows assumed to zero that the stale-concept list CONTRADICTS.

    ``ttm_sbc_assumed_zero`` fires when the fiscal year shows no SBC. But if
    ``ttm_stale_concepts`` names ``sbc``, the TTM builder SAW an sbc window
    and withdrew it for being out of date -- which is positive evidence an
    SBC line exists, against a null FY value. The assumption still errs in
    the company's favour on those rows.

    A dedicated boolean rather than a documented README predicate, on the
    codebase's own standing preference: every other finding here is a column,
    and a reader should never have to string-split ``ttm_stale_concepts`` to
    learn that a published figure is disputed. 27 rows (artifact-level) is a
    small population, but the rows it names are exactly the ones a human
    should re-derive by hand, and a predicate buried in a document is not
    something a screen can filter on.

    🔴 A FLAG, NOT A CORRECTION. ttm_fcf_after_sbc is unchanged on these
    rows: which of the two signals is right needs the quarterly statements,
    and guessing is the mistake this file carries three post-mortems about.
    """
    if "ttm_sbc_assumed_zero" not in frame.columns:
        return frame.with_columns(pl.lit(False).alias("ttm_sbc_evidence_conflict"))
    names = pl.col("ttm_stale_concepts").fill_null("").str.split(",")
    return frame.with_columns(
        (pl.col("ttm_sbc_assumed_zero").fill_null(False) & names.list.contains("sbc"))
        .fill_null(False)
        .alias("ttm_sbc_evidence_conflict")
    )


def add_ttm_divergence(frame):
    """Flag names whose FY and TTM cash generation DISAGREE (added 2026-08-19).

    Every growth and quality leg in screen.py runs on the FY figures. The TTM
    figures are computed here and then never tested, so a company whose cash
    generation inverted over the last four quarters still shortlists as clean.
    Found live: 5 of 19 main-lane survivors -- 26% -- had positive FY
    FCF-after-SBC and NEGATIVE TTM. NOG shortlisted at a clean 11.7x P/FCF
    while its TTM FCF-after-SBC was -$207M on a capex ramp; a scheduled run
    caught it by hand, which is expensive work a column does for free across
    six thousand names.

    TWO flags, because the two cases are genuinely different and conflating
    them would be the same error this file already carries three post-mortems
    about:

      ttm_fcf_divergence -- FY positive, TTM not. A REAL disagreement worth
          diagnosing. NOG (TTM OCF $1,415M against FY $1,505M, capex ramped
          to $1,606M) and ANF (TTM OCF $135M against FY $619M) are this: the
          components are plausible and the business genuinely changed.

      ttm_suspect -- TTM OCF is NEGATIVE while FY OCF is positive. That is
          not a company finding, it is a broken TTM series: KFY came out at
          -$971M of TTM operating cash against +$414M FY on $2.94B of
          revenue, and LRN at -$616M against +$434M. Neither business burns
          that. The four-quarter sum is picking up facts that are not four
          discrete comparable quarters.

    🔴 Neither flag rejects. A divergence is a MISSING DIAGNOSIS, not a
    verdict -- and critically, it does not say WHICH of the two numbers is
    wrong. Deciding that requires the quarterly statements, so this routes a
    human there rather than guessing.
    """
    fy_ocf_positive = pl.col("ocf").is_not_null() & (pl.col("ocf") > 0)
    ttm_ocf_negative = pl.col("ttm_ocf").is_not_null() & (pl.col("ttm_ocf") < 0)
    # 🔴 A company with no buildable TTM is UNMEASURED, not in agreement.
    # Both flags below are False for such a row -- they have to be, they test
    # a disagreement that cannot be evaluated -- so the third column is what
    # carries the distinction. Without it a null TTM and a checked-and-clean
    # TTM report identically, which is the same defect class as the null
    # market cap at the band gate and the capex chain that failed open.
    return frame.with_columns(
        (
            pl.col("fcf_after_sbc").is_not_null()
            & (pl.col("fcf_after_sbc") > 0)
            & pl.col("ttm_fcf_after_sbc").is_not_null()
            & (pl.col("ttm_fcf_after_sbc") <= 0)
        )
        .fill_null(False)
        .alias("ttm_fcf_divergence"),
        (fy_ocf_positive & ttm_ocf_negative).fill_null(False).alias("ttm_suspect"),
        pl.col("ttm_ocf").is_null().alias("ttm_unavailable"),
    )


def _quarter_slope(values):
    """OLS slope of 4 values against x=[1,2,3,4]; null unless all 4 are known.

    Positive means accelerating -- each quarter's YoY growth outran the
    last, not just that growth is positive.
    """
    if len(values) != 4 or any(v is None for v in values):
        return None
    xs = (1.0, 2.0, 3.0, 4.0)
    x_mean, y_mean = 2.5, sum(values) / 4.0
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    return numerator / denominator


def _trailing_streak(values):
    """Consecutive quarters, counting back from the most recent, where YoY
    growth exceeded the quarter before it. Unbounded -- a 6-quarter streak
    is reportable even though the q1..q4 display columns only show 4."""
    streak = 0
    for i in range(len(values) - 1, 0, -1):
        if values[i] is None or values[i - 1] is None or not (values[i] > values[i - 1]):
            break
        streak += 1
    return streak


def _last_four_padded(values):
    """The last 4 entries of a chronological list, left-padded with None."""
    tail = values[-4:]
    return [None] * (4 - len(tail)) + tail


def build_quarterly_acceleration(facts):
    """Revenue and FCF acceleration signals from discrete quarterly facts.

    YoY growth is computed against the same fiscal quarter one year prior
    (via a self-join on cik + a chronological quarter key), not the
    preceding quarter, so seasonal filers are not read as decelerating every
    Q1. quarters_of_accelerating_* looks back as far as the data goes; the
    _q1.._q4 display columns are always just the four most recent.
    """
    quarters = facts.filter(pl.col("fiscal_period").is_in(list(QUARTERS)))
    if quarters.is_empty():
        return pl.DataFrame(schema={"cik": pl.Int64})

    wide = quarters.pivot(
        on="concept",
        index=["cik", "fiscal_year", "fiscal_period"],
        values="value",
        aggregate_function="first",
    )
    for concept in ("revenue", "ocf", "capex"):
        if concept not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(concept))
    wide = wide.with_columns((pl.col("ocf") - pl.col("capex")).alias("fcf"))

    quarter_number = pl.col("fiscal_period").str.slice(1, 1).cast(pl.Int32)
    wide = wide.with_columns((pl.col("fiscal_year") * 4 + quarter_number).alias("_qkey"))

    for metric in ("revenue", "fcf"):
        prior = wide.select(
            "cik",
            (pl.col("_qkey") + 4).alias("_qkey"),
            pl.col(metric).alias(f"_{metric}_prior"),
        )
        wide = wide.join(prior, on=["cik", "_qkey"], how="left")
        wide = wide.with_columns(
            _safe_div(
                pl.col(metric) - pl.col(f"_{metric}_prior"),
                pl.col(f"_{metric}_prior").abs(),
            ).alias(f"_{metric}_yoy")
        )

    per_company = (
        wide.sort(["cik", "_qkey"])
        .group_by("cik", maintain_order=True)
        .agg(
            pl.col("_revenue_yoy").alias("_revenue_yoy_series"),
            pl.col("_fcf_yoy").alias("_fcf_yoy_series"),
        )
    )

    rows = []
    for row in per_company.to_dicts():
        revenue_series = row["_revenue_yoy_series"]
        fcf_series = row["_fcf_yoy_series"]
        revenue_q = _last_four_padded(revenue_series)
        fcf_q = _last_four_padded(fcf_series)
        rows.append(
            {
                "cik": row["cik"],
                "revenue_growth_yoy_q1": revenue_q[0],
                "revenue_growth_yoy_q2": revenue_q[1],
                "revenue_growth_yoy_q3": revenue_q[2],
                "revenue_growth_yoy_q4": revenue_q[3],
                "revenue_accel_4q": _quarter_slope(revenue_q),
                "quarters_of_accelerating_revenue": _trailing_streak(revenue_series),
                "fcf_growth_yoy_q1": fcf_q[0],
                "fcf_growth_yoy_q2": fcf_q[1],
                "fcf_growth_yoy_q3": fcf_q[2],
                "fcf_growth_yoy_q4": fcf_q[3],
                "fcf_accel_4q": _quarter_slope(fcf_q),
                "quarters_of_accelerating_fcf": _trailing_streak(fcf_series),
            }
        )
    schema = {
        "cik": pl.Int64,
        "revenue_growth_yoy_q1": pl.Float64,
        "revenue_growth_yoy_q2": pl.Float64,
        "revenue_growth_yoy_q3": pl.Float64,
        "revenue_growth_yoy_q4": pl.Float64,
        "revenue_accel_4q": pl.Float64,
        "quarters_of_accelerating_revenue": pl.Int64,
        "fcf_growth_yoy_q1": pl.Float64,
        "fcf_growth_yoy_q2": pl.Float64,
        "fcf_growth_yoy_q3": pl.Float64,
        "fcf_growth_yoy_q4": pl.Float64,
        "fcf_accel_4q": pl.Float64,
        "quarters_of_accelerating_fcf": pl.Int64,
    }
    return pl.DataFrame(rows, schema=schema)


# Concepts counted when picking which of several same-named CIKs is the real
# filer: the ones the load-bearing tests actually consume.
DEDUP_CONCEPTS = ("equity", "goodwill", "intangibles", "ocf", "net_income", "capex", "sbc")


def _normalize_company_name(name_col):
    """Uppercase, drop periods/commas, collapse whitespace -- enough to match
    'Exxon Mobil Corporation' filed twice under two CIKs without over-matching
    unrelated companies."""
    return (
        name_col.str.to_uppercase()
        .str.replace_all(r"[.,]", "")
        .str.strip_chars()
        .str.replace_all(r"\s+", " ")
    )


def deduplicate_by_company_name(frame):
    """Collapse CIKs that share a company name onto the one with real filing data.

    Re-registration under a new holding company, and parent / operating-
    partnership pairs, leave SEC's ticker map pointing at a CIK with little or
    no filing history while a sibling CIK carries the actual facts. Left
    uncorrected this silently double-counts a company in every screen total,
    and can attach its ticker to the empty registrant instead of the one with
    real numbers.

    The CIK with the most non-null DEDUP_CONCEPTS in the group survives
    (ties broken by whichever already carries a ticker, then by lowest CIK,
    for a deterministic choice); any ticker held by a dropped sibling is
    reattached to the survivor. Returns (kept, dropped) -- dropped is the
    duplicate_filers.csv report, never written into gate0.csv.
    """
    scored = frame.with_columns(
        _normalize_company_name(pl.col("company_name")).alias("_norm_name"),
        sum(pl.col(c).is_not_null().cast(pl.Int32) for c in DEDUP_CONCEPTS).alias(
            "_dedup_score"
        ),
        pl.col("ticker").is_not_null().alias("_has_ticker"),
    )
    group_size = scored.group_by("_norm_name").agg(pl.len().alias("_group_size"))
    scored = scored.join(group_size, on="_norm_name", how="left")

    solo = scored.filter(pl.col("_group_size") == 1)
    grouped = scored.filter(pl.col("_group_size") > 1)
    drop_cols = ["_norm_name", "_dedup_score", "_has_ticker", "_group_size"]

    if grouped.is_empty():
        return solo.drop(drop_cols).with_columns(
            pl.lit("").alias("merged_from_ciks")
        ), frame.filter(pl.lit(False))

    grouped = grouped.sort(
        ["_norm_name", "_dedup_score", "_has_ticker", "cik"],
        descending=[False, True, True, False],
    )
    primary = grouped.group_by("_norm_name", maintain_order=True).first()
    non_primary = grouped.join(
        primary.select("_norm_name", pl.col("cik").alias("_primary_cik")),
        on="_norm_name",
        how="left",
    ).filter(pl.col("cik") != pl.col("_primary_cik"))

    fallback_ticker = non_primary.group_by("_norm_name").agg(
        pl.col("ticker").drop_nulls().first().alias("_fallback_ticker")
    )
    merged_ciks = non_primary.group_by("_norm_name").agg(
        pl.col("cik").cast(pl.Utf8).str.join(",").alias("merged_from_ciks")
    )
    primary = (
        primary.join(fallback_ticker, on="_norm_name", how="left")
        .join(merged_ciks, on="_norm_name", how="left")
        .with_columns(
            pl.coalesce([pl.col("ticker"), pl.col("_fallback_ticker")]).alias("ticker")
        )
        .drop("_fallback_ticker")
    )
    non_primary = non_primary.rename({"_primary_cik": "merged_into_cik"})

    solo = solo.with_columns(pl.lit("").alias("merged_from_ciks"))
    kept = pl.concat([solo.drop(drop_cols), primary.drop(drop_cols)], how="diagonal_relaxed")
    return kept, non_primary.drop(drop_cols)


DUPLICATE_FILER_COLUMNS = (
    "ticker",
    "cik",
    "company_name",
    "merged_into_cik",
    "latest_fiscal_year",
)


def write_duplicate_filers(frame, path):
    """The dropped siblings, with the CIK that absorbed each one."""
    present = [c for c in DUPLICATE_FILER_COLUMNS if c in frame.columns]
    report = frame.select(present).sort("company_name")
    report.write_csv(path)
    return report.height


def load_market_caps(path):
    """Optional ticker,market_cap CSV. Absent means multiples stay null."""
    if not path:
        return None
    caps = pl.read_csv(path)
    columns = {c.lower().strip(): c for c in caps.columns}
    if "ticker" not in columns or "market_cap" not in columns:
        raise SystemExit(f"{path}: expected columns 'ticker,market_cap'")
    return caps.select(
        pl.col(columns["ticker"]).str.to_uppercase().alias("ticker"),
        pl.col(columns["market_cap"]).cast(pl.Float64).alias("market_cap"),
    ).unique(subset="ticker")


def load_prices(path):
    """Optional ticker,price,ma_200 CSV. market_cap is derived from the
    store's own shares_diluted rather than requiring a pre-computed cap --
    EDGAR has no prices, so this is the one external join every run needs,
    and asking only for price (not price and cap and EV) is what makes
    bulk-pricing a whole candidate list actually tractable. ma_200 is
    optional; its column may be entirely absent or individually null.
    """
    if not path:
        return None
    prices = pl.read_csv(path)
    columns = {c.lower().strip(): c for c in prices.columns}
    if "ticker" not in columns or "price" not in columns:
        raise SystemExit(f"{path}: expected columns 'ticker,price[,ma_200]'")
    select = [
        pl.col(columns["ticker"]).str.to_uppercase().alias("ticker"),
        pl.col(columns["price"]).cast(pl.Float64).alias("price"),
    ]
    if "ma_200" in columns:
        select.append(pl.col(columns["ma_200"]).cast(pl.Float64).alias("ma_200"))
    else:
        select.append(pl.lit(None, dtype=pl.Float64).alias("ma_200"))
    return prices.select(select).unique(subset="ticker")


def apply_filters(frame, args):
    """Ticker list, SIC exclusion, and market-cap band."""
    if args.tickers:
        return _filter_tickers(frame, args.tickers)

    notes = []
    if not args.include_financials:
        for low, high in parse_sic_ranges(args.exclude_sic):
            sic = pl.col("sic").cast(pl.Int32, strict=False)
            # A null SIC is kept: we cannot prove it is a financial, and dropping
            # it would be exactly the silent loss this pipeline exists to avoid.
            frame = frame.filter(sic.is_null() | ~sic.is_between(low, high))
        notes.append(f"excluded_sic={args.exclude_sic}")

    if args.min_mktcap is not None:
        frame = frame.filter(pl.col("market_cap") >= args.min_mktcap)
        notes.append(f"min_mktcap={args.min_mktcap:,.0f}")
    if args.max_mktcap is not None:
        frame = frame.filter(pl.col("market_cap") <= args.max_mktcap)
        notes.append(f"max_mktcap={args.max_mktcap:,.0f}")
    return frame, ", ".join(notes) or "none"


def _filter_tickers(frame, spec):
    """Ad-hoc lookup by ticker or bare CIK, reporting anything not found.

    SEC's ticker map does not cover every filer that reports XBRL facts, so a
    requested ticker can be genuinely absent while the company sits in the store
    under its CIK. Saying so is the point: a lookup that returns nothing without
    comment is the silent-omission failure this pipeline exists to avoid.
    """
    wanted = [item.strip().upper() for item in spec.split(",") if item.strip()]
    ciks = {int(item) for item in wanted if item.isdigit()}
    tickers = [item for item in wanted if not item.isdigit()]

    selected = frame.filter(pl.col("ticker").is_in(tickers) | pl.col("cik").is_in(ciks))
    found = set(selected["ticker"].drop_nulls().to_list()) | {
        str(c) for c in selected["cik"].to_list()
    }
    missing = [item for item in wanted if item not in found]
    if missing:
        print(
            f"  not found in the ticker map: {', '.join(missing)}"
            " -- the filer may still be present by CIK (see meta.parquet)"
        )
    return selected, f"tickers={len(wanted)}, matched={selected.height}"


INACTIVE_COLUMNS = (
    "ticker",
    "cik",
    "company_name",
    "sic",
    "sic_description",
    "latest_fiscal_year",
    "period_end",
)


def split_by_liveness(frame, min_fiscal_year):
    """Split the universe into current filers and dead/delisted ones.

    companyfacts.zip contains every entity that ever filed XBRL, including
    decades of dead, delisted and deregistered companies. A company whose
    latest fiscal year predates the cutoff was never a screening candidate --
    it should not count as unscored, because it was never going to be scored.
    """
    active = frame.filter(pl.col("latest_fiscal_year") >= min_fiscal_year)
    inactive = frame.filter(pl.col("latest_fiscal_year") < min_fiscal_year)
    return active, inactive


def write_inactive_filers(frame, path):
    """Companies excluded by the liveness filter, reported rather than dropped."""
    present = [c for c in INACTIVE_COLUMNS if c in frame.columns]
    frame.select(present).sort("latest_fiscal_year", descending=True).write_csv(path)
    return frame.height


def write_data_quality(frame, path):
    """Every company with a missing required concept or a quality-flag issue."""
    missing = [
        pl.when(pl.col(concept).is_null())
        .then(pl.lit(concept))
        .otherwise(None)
        .alias(concept)
        for concept in REQUIRED_CONCEPTS
    ]
    disagreement = (
        pl.when(pl.col("acq_cf_bs_disagreement").fill_null(False))
        .then(pl.lit("acq_cf_bs_disagreement"))
        .otherwise(None)
    )
    flags = pl.concat_list(missing + [disagreement]).list.drop_nulls()

    report = (
        frame.select(
            "cik",
            "ticker",
            "company_name",
            "latest_fiscal_year",
            flags.alias("missing"),
        )
        .with_columns(pl.col("missing").list.len().alias("n_missing"))
        .filter(pl.col("n_missing") > 0)
        .with_columns(pl.col("missing").list.join(",").alias("missing_concepts"))
        .drop("missing")
        .sort(["n_missing", "ticker"], descending=[True, False], nulls_last=True)
    )
    report.write_csv(path)
    return report.height


OUTPUT_ORDER = (
    "ticker",
    "cik",
    "company_name",
    "merged_from_ciks",
    "exchange",
    "sic",
    "sic_description",
    "taxonomy",
    "filing_form",
    "reporting_currency",
    "fiscal_year_end",
    "latest_fiscal_year",
    "period_end",
    "gate0_pass",
    "gate0_framework_pass",
    "framework_leg_failed",
    "gate0_status",
    "gate0_not_evaluable",
    "imputed_fields",
    "resolution_basis",
    "resolution_basis_goodwill",
    "resolution_basis_intangibles",
    "carried_forward_fields",
    "carry_forward_age_days",
    "sbc_unverified",
    "sbc_assumed_zero",
    "sbc_window_lost",
    "sbc_ever_reported",
    "price",
    "ma_200",
    "pct_vs_200ma",
    "market_cap",
    "ev",
    "fcf_after_sbc_multiple",
    "p_fcf_after_sbc",
    "ev_fcf_after_sbc",
    "tangible_book",
    "tangible_book_latest_q",
    "tangible_book_basis",
    "tangible_book_vintage_conflict",
    "income_quality",
    "fcf",
    "fcf_after_sbc",
    "sbc_pct_revenue",
    "net_cash",
    "effective_tax",
    "ni_vs_oi",
    "acq_intensity",
    "bs_acq_intensity",
    "acq_cf_bs_disagreement",
    "buyback_pct_fcf",
    "fcf_per_share",
    "revenue",
    "net_income",
    "operating_income",
    "ocf",
    "capex",
    "capex_broken",
    "capex_suspect",
    "lease_payments",
    "lease_unmeasured",
    "lease_heavy",
    "shares_scale_suspect",
    "investing_unreconciled",
    "investing_residual",
    "capex_vs_dep_amort",
    "ttm_fcf_divergence",
    "ttm_suspect",
    "ttm_unavailable",
    "ttm_stale_concepts",
    "ttm_window_start",
    "ttm_window_end",
    "ttm_window_misaligned",
    "ttm_lease_payments",
    "net_margin",
    "income_quality_suspect",
    "operating_margin_2y_ago",
    "sbc",
    "equity",
    "goodwill",
    "intangibles",
    "cash",
    "total_debt",
    "shares_diluted",
    "fail_tangible_book",
    "test_tangible_book",
    "fail_income_quality",
    "test_income_quality",
    "fail_fcf",
    "test_fcf",
    "fail_fcf_after_sbc",
    "test_fcf_after_sbc",
    "fail_sbc",
    "test_sbc",
    "warn_sbc",
    "fail_ni_over_oi",
    "test_ni_vs_oi",
    "fail_tax_anomaly",
    "test_tax_anomaly",
    "warn_inorganic",
    "tangible_book_yrs_negative",
    "growth_basis",
    "short_history",
    "revenue_cagr_3y",
    "revenue_cagr_5y",
    "fcf_per_share_cagr_3y",
    "fcf_per_share_cagr_5y",
    "fcf_per_share_3y_ago",
    "fcf_per_share_5y_ago",
    "fcf_per_share_earliest",
    "fcf_per_share_latest",
    "fcf_per_share_delta_abs",
    "fcf_inflection",
    "fcf_inflection_years",
    "operating_margin_latest",
    "operating_margin_5y_ago",
    "operating_margin_delta",
    "income_quality_3y_avg",
    "income_quality_direction",
    "ttm_revenue",
    "ttm_net_income",
    "ttm_ocf",
    "ttm_capex",
    "ttm_capex_negative",
    "ttm_capex_missing",
    "ttm_sbc",
    "ttm_sbc_assumed_zero",
    "ttm_sbc_window_lost",
    "ttm_sbc_evidence_conflict",
    "ttm_fcf_after_sbc",
    # Balances and rate concepts: a snapshot, NOT a trailing sum. Deliberately
    # a different prefix -- two different things must not share a label.
    "latest_q_goodwill",
    "latest_q_intangibles",
    "latest_q_equity",
    "latest_q_cash",
    "latest_q_total_debt",
    "latest_q_shares_diluted",
    "latest_q_period_end",
    "revenue_growth_yoy_q1",
    "revenue_growth_yoy_q2",
    "revenue_growth_yoy_q3",
    "revenue_growth_yoy_q4",
    "revenue_accel_4q",
    "quarters_of_accelerating_revenue",
    "fcf_growth_yoy_q1",
    "fcf_growth_yoy_q2",
    "fcf_growth_yoy_q3",
    "fcf_growth_yoy_q4",
    "fcf_accel_4q",
    "quarters_of_accelerating_fcf",
)


def order_columns(frame):
    """Stable, readable column order; anything extra (source tags) goes last."""
    present = [c for c in OUTPUT_ORDER if c in frame.columns]
    extra = sorted(c for c in frame.columns if c not in present)
    return frame.select(present + extra)


def load_universe(paths, assume_absent_zero=False, allow_imputed=False,
                  resolve_annual_sbc_zero=False):
    """Join facts, trends, TTM and company metadata into one row per company."""
    facts = pl.read_parquet(paths.facts)
    meta = pl.read_parquet(paths.meta)

    # 🔴 Everything below widen() reads the fact table DIRECTLY, and each of
    # those builders picks a "latest" period or summarises which concepts are
    # present. A diagnostic concept must not participate in any of that, so it
    # is stripped here once rather than guarded in four places.
    #
    # Measured when the investing_* concepts were added and this was NOT done:
    # ttm_stale_concepts changed on 1,215 rows (the new concepts were being
    # named as stale work items), latest_q_shares_diluted on 146, the quarterly
    # revenue-acceleration columns on up to 17, and reporting_currency on 3 --
    # none of which has anything to do with the reconciliation flag. A concept
    # added to DESCRIBE the data had begun to change what the data said.
    scoring_facts = facts.filter(~pl.col("concept").is_in(DIAGNOSTIC_CONCEPTS))

    # 🔴 The TTM frame is built BEFORE the annual metrics now: it supplies one
    # leg of the SBC evidence test, and that test has to be settled before
    # compute_metrics resolves any fiscal year. build_ttm reads scoring_facts
    # and nothing from the annual frame, so the order is free to change.
    ttm = build_ttm(scoring_facts)
    annual = compute_metrics(
        widen(facts),
        assume_absent_zero,
        sbc_evidence=_ttm_sbc_evidence(ttm),
        resolve_sbc_zero=resolve_annual_sbc_zero,
    )
    trends = build_trends(annual)
    fcf_inflection = build_fcf_inflection(annual)
    latest = latest_rows(annual)
    if not assume_absent_zero:
        # The bucket-based resolver is the smart default path. It is skipped
        # under --assume-absent-zero deliberately: that flag is a blunt manual
        # override, and a user who explicitly opts into it should get exactly
        # what they asked for, not have it second-guessed by inference.
        latest = resolve_goodwill_intangibles(annual, latest)
    latest = add_flags(latest)
    latest_q = build_latest_quarter(scoring_facts)
    provenance = _company_provenance(scoring_facts)
    acceleration = build_quarterly_acceleration(scoring_facts)

    frame = latest.join(trends, on=["cik", "latest_fiscal_year"], how="left")
    frame = frame.join(fcf_inflection, on="cik", how="left")
    if ttm.width > 1:
        frame = frame.join(ttm, on="cik", how="left")
    if latest_q.width > 1:
        frame = frame.join(latest_q, on="cik", how="left")
    # 🔴 MUST run after the latest_q join and BEFORE add_verdict. It re-tests
    # the tangible-book leg on the later of the two balance-sheet vintages by
    # overwriting fail_tangible_book, and add_verdict is what turns that into
    # test_tangible_book and gate0_status. Run it after add_verdict and the
    # column changes while the verdict does not, which is worse than not
    # fixing it -- the row would then contradict itself in the shipped store.
    frame = add_tangible_book_vintage(frame)
    # Must run AFTER the ttm join -- it reads both the FY and the TTM columns.
    # When no TTM was buildable at all the flags are false, not null: "no TTM
    # series exists" is not a divergence.
    if "ttm_fcf_after_sbc" in frame.columns:
        # The capex flags ride in on a LEFT join, so they are null on every
        # filer that had no TTM row at all. A null boolean is a third state
        # these columns do not mean: each is a flag that either fired or did
        # not, and a row with no TTM capex to test is a row where it did not.
        frame = frame.with_columns(
            pl.col("ttm_capex_negative").fill_null(False),
            pl.col("ttm_capex_missing").fill_null(False),
        )
        # 🔴 MUST run BEFORE add_ttm_divergence. It turns NULLs into numbers,
        # and ttm_fcf_divergence is computed FROM ttm_fcf_after_sbc: run it
        # after, and every recovered row would carry a figure that was never
        # tested against its fiscal year, reading as agreement on a
        # comparison nothing performed. That is the defect class this whole
        # file is a post-mortem of, so the order is load-bearing.
        frame = resolve_ttm_sbc(frame)
        frame = add_ttm_divergence(frame)
        frame = add_ttm_sbc_evidence_conflict(frame)
    else:
        frame = frame.with_columns(
            pl.lit(False).alias("ttm_fcf_divergence"),
            pl.lit(False).alias("ttm_suspect"),
            pl.lit(True).alias("ttm_unavailable"),
            pl.lit(False).alias("ttm_capex_negative"),
            pl.lit(False).alias("ttm_capex_missing"),
            pl.lit(False).alias("ttm_sbc_assumed_zero"),
            pl.lit(False).alias("ttm_sbc_window_lost"),
            pl.lit(False).alias("ttm_sbc_evidence_conflict"),
        )
    # Reads ttm_window_end (from the ttm join) against period_end (from the
    # annual rows), so it can only run once both are on the frame.
    frame = add_ttm_window_alignment(frame)
    # Must run AFTER the provenance join: the reconciliation abstains on a
    # non-USD reporting currency, and reporting_currency arrives here.
    frame = _add_investing_reconciliation(frame.join(provenance, on="cik", how="left"))
    if acceleration.width > 1:
        frame = frame.join(acceleration, on="cik", how="left")
    return add_verdict(frame.join(meta, on="cik", how="left"), allow_imputed=allow_imputed)


def _status_counts(frame):
    if frame.is_empty():
        return {}
    return {
        row["gate0_status"]: f"{row['len']:,}"
        for row in frame.group_by("gate0_status").len().to_dicts()
    }


def _failure_counts(frame):
    """FAIL/True counts for both the legacy boolean flags and the test_ columns.

    sbc_unverified is included explicitly by name (not by prefix): it must
    never pass silently, so it belongs in the summary counts every run.
    """
    if frame.is_empty():
        return {}
    totals = {}
    for column in frame.columns:
        if column.startswith(("fail_", "warn_")):
            totals[column] = f"{frame[column].fill_null(False).sum():,}"
        elif column.startswith("test_"):
            totals[column] = f"{(frame[column] == 'FAIL').sum():,}"
    if "sbc_unverified" in frame.columns:
        totals["sbc_unverified"] = f"{frame['sbc_unverified'].fill_null(False).sum():,}"
    return totals


def _resolution_counts(frame):
    """How much of tangible_book rests on reported facts versus inference."""
    if frame.is_empty() or "resolution_basis" not in frame.columns:
        return {}
    return {
        row["resolution_basis"]: f"{row['len']:,}"
        for row in frame.group_by("resolution_basis").len().to_dicts()
    }


def _tangible_book_vintage_counts(frame):
    """Which balance sheet decided the tangible-book leg, and where they clash."""
    if frame.is_empty() or "tangible_book_basis" not in frame.columns:
        return {}
    counts = {
        f"basis_{row['tangible_book_basis']}": f"{row['len']:,}"
        for row in frame.group_by("tangible_book_basis").len().to_dicts()
    }
    conflict = pl.col("tangible_book_vintage_conflict").fill_null(False)
    fy = pl.col("tangible_book")
    latest_q = pl.col("tangible_book_latest_q")
    counts["vintage_conflict"] = f"{frame.select(conflict.sum()).item():,}"
    counts["flip_fy_pos_to_q_neg"] = "{:,}".format(
        frame.select((conflict & (fy >= 0) & (latest_q < 0)).sum()).item()
    )
    counts["flip_fy_neg_to_q_pos"] = "{:,}".format(
        frame.select((conflict & (fy < 0) & (latest_q >= 0)).sum()).item()
    )
    return counts


def _ttm_window_counts(frame):
    """How many rows carry a TTM window that stops short of the fiscal year,
    and how many carry one whose FCF chain could not be closed.

    The four resolution flags are logged every run for the same reason
    sbc_unverified is named explicitly in _failure_counts: a withheld metric
    must never pass silently. Before they existed, 3,098 rows published a
    blank ttm_fcf_after_sbc with nothing on them to say why, and a run summary
    that does not count them is how that goes unnoticed again.
    """
    if frame.is_empty() or "ttm_window_misaligned" not in frame.columns:
        return {}
    misaligned = pl.col("ttm_window_misaligned").fill_null(False)
    counts = {
        "window_dated": "{:,}".format(
            frame.select(pl.col("ttm_window_end").is_not_null().sum()).item()
        ),
        "window_misaligned": f"{frame.select(misaligned.sum()).item():,}",
    }
    for column, label in (
        ("ttm_capex_negative", "capex_negative"),
        ("ttm_capex_missing", "capex_missing"),
        ("ttm_sbc_window_lost", "sbc_window_lost"),
        ("ttm_sbc_assumed_zero", "sbc_assumed_zero"),
    ):
        if column in frame.columns:
            counts[label] = "{:,}".format(
                frame.select(pl.col(column).fill_null(False).sum()).item()
            )
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="data root directory")
    parser.add_argument("--out", default=None, help="output CSV path")
    parser.add_argument("--min-mktcap", type=float, default=None)
    parser.add_argument("--max-mktcap", type=float, default=None)
    parser.add_argument("--mktcap-csv", default=None, help="ticker,market_cap CSV")
    parser.add_argument(
        "--price-csv",
        default=None,
        help="ticker,price[,ma_200] CSV; market_cap is derived from the "
        "store's own shares_diluted. Mutually exclusive with --mktcap-csv",
    )
    parser.add_argument("--tickers", default=None, help="ad-hoc lookup, e.g. MCRI,SKYW")
    parser.add_argument("--exclude-sic", default=DEFAULT_EXCLUDE_SIC)
    parser.add_argument(
        "--include-financials",
        action="store_true",
        help="keep banks, brokers, insurers and REITs (FCF is meaningless there)",
    )
    parser.add_argument(
        "--resolve-annual-sbc-zero",
        action="store_true",
        help="PHASE B, MEASUREMENT ONLY, DEFAULT OFF. Compute fcf_after_sbc "
        "with an SBC term of zero on sbc_assumed_zero rows, aligning the "
        "annual convention with the TTM one. fcf_after_sbc feeds "
        "fail_fcf_after_sbc, a GATE 0 LEG: this moves verdicts, shortlists "
        "and the review queue, including names already dispositioned on a "
        "not-evaluable leg. Run it into a scratch --out/--root to see what "
        "would change; do not make it the default without the book owner's "
        "decision.",
    )
    parser.add_argument(
        "--assume-absent-zero",
        action="store_true",
        help="treat an absent goodwill/intangibles tag as zero (off by default: "
        "absent and zero are different statements)",
    )
    parser.add_argument(
        "--allow-imputed",
        action="store_true",
        help="allow gate0_pass=True on a row with imputed_fields populated "
        "(off by default: an assumed value should not manufacture a pass)",
    )
    parser.add_argument(
        "--min-fiscal-year",
        type=int,
        default=DEFAULT_MIN_FISCAL_YEAR,
        help="exclude filers whose latest fiscal year predates this "
        f"(default {DEFAULT_MIN_FISCAL_YEAR}: dead/delisted filers are not "
        "unscored, they are out of scope)",
    )
    args = parser.parse_args(argv)

    paths = Paths(args.root).ensure()
    for required in (paths.facts, paths.meta):
        if not required.exists():
            parser.error(f"{required} not found. Run build_facts.py first.")

    started = time.monotonic()
    frame = load_universe(
        paths, args.assume_absent_zero, args.allow_imputed,
        resolve_annual_sbc_zero=args.resolve_annual_sbc_zero,
    )
    universe_size = frame.height

    frame, duplicates = deduplicate_by_company_name(frame)
    duplicate_count = write_duplicate_filers(duplicates, paths.duplicate_filers)

    if args.tickers:
        inactive = frame.filter(pl.lit(False))
    else:
        frame, inactive = split_by_liveness(frame, args.min_fiscal_year)
    active_count = frame.height
    inactive_count = write_inactive_filers(inactive, paths.inactive_filers)

    if args.mktcap_csv and args.price_csv:
        parser.error(
            "--mktcap-csv and --price-csv both supply market_cap; pass one, "
            "not both."
        )

    wants_band = args.min_mktcap is not None or args.max_mktcap is not None
    if wants_band and not args.mktcap_csv and not args.price_csv and not args.tickers:
        parser.error(
            "market caps are not in EDGAR: --min-mktcap/--max-mktcap need "
            "--mktcap-csv ticker,market_cap or --price-csv ticker,price. "
            "Without one every cap is null and the screen would return nothing."
        )

    prices = load_prices(args.price_csv)
    has_market_cap_source = prices is not None
    if prices is not None:
        # market_cap from the store's own share count -- a price-only feed is
        # enough, no separately-computed cap needed for every candidate.
        frame = frame.join(prices, on="ticker", how="left").with_columns(
            (pl.col("price") * pl.col("shares_diluted")).alias("market_cap")
        )
    else:
        caps = load_market_caps(args.mktcap_csv)
        has_market_cap_source = caps is not None
        if caps is not None:
            frame = frame.join(caps, on="ticker", how="left")
        else:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("market_cap"))

    frame = frame.with_columns(
        _safe_div(pl.col("market_cap"), pl.col("fcf_after_sbc")).alias(
            "fcf_after_sbc_multiple"
        ),
        _safe_div(pl.col("market_cap"), pl.col("fcf_after_sbc")).alias(
            "p_fcf_after_sbc"
        ),
        # Null total_debt/cash propagates to a null EV, same as everywhere
        # else in this pipeline -- an unknown debt load is not a zero one.
        (pl.col("market_cap") + pl.col("total_debt") - pl.col("cash")).alias("ev"),
    )
    frame = frame.with_columns(
        _safe_div(pl.col("ev"), pl.col("fcf_after_sbc")).alias("ev_fcf_after_sbc")
    )
    if prices is not None:
        frame = frame.with_columns(
            _safe_div(pl.col("price") - pl.col("ma_200"), pl.col("ma_200")).alias(
                "pct_vs_200ma"
            )
        )
    else:
        frame = frame.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("pct_vs_200ma")
        )

    frame, filter_note = apply_filters(frame, args)
    if has_market_cap_source:
        frame = frame.sort("fcf_after_sbc_multiple", nulls_last=True)
    else:
        frame = frame.sort("fcf_per_share_cagr_5y", descending=True, nulls_last=True)

    out_path = args.out or paths.gate0
    frame = order_columns(frame)
    frame.write_csv(out_path)
    flagged = write_data_quality(frame, paths.data_quality)

    log_stage(
        "gate0:universe",
        companies=f"{universe_size:,}",
        duplicate_filers=f"{duplicate_count:,}",
        active=f"{active_count:,}",
        inactive_filers=f"{inactive_count:,}",
        filters=filter_note,
    )
    log_stage("gate0:verdict", screened=f"{frame.height:,}", **_status_counts(frame))
    log_stage("gate0:resolution_basis", **_resolution_counts(frame))
    log_stage("gate0:tangible_book_vintage", **_tangible_book_vintage_counts(frame))
    log_stage("gate0:ttm_window", **_ttm_window_counts(frame))
    log_stage("gate0:failures", **_failure_counts(frame))
    log_stage(
        "gate0",
        out=str(out_path),
        data_quality=f"{flagged:,} companies",
        elapsed_sec=f"{time.monotonic() - started:.1f}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
