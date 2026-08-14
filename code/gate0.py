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
    """One row per (cik, fiscal_year) with a column per concept, plus tag witnesses."""
    annual = facts.filter(pl.col("fiscal_period") == period)
    values = annual.pivot(
        on="concept",
        index=["cik", "fiscal_year"],
        values="value",
        aggregate_function="first",
    )
    for concept in ALL_CONCEPTS:
        if concept not in values.columns:
            values = values.with_columns(pl.lit(None, dtype=pl.Float64).alias(concept))

    ends = annual.group_by(["cik", "fiscal_year"]).agg(
        pl.col("period_end").max().alias("period_end")
    )
    frame = values.join(ends, on=["cik", "fiscal_year"], how="left")

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
    """
    return facts.group_by("cik").agg(
        pl.col("taxonomy").mode().first().alias("taxonomy"),
        pl.col("form").mode().first().alias("filing_form"),
        pl.col("unit")
        .filter(pl.col("unit") != "shares")
        .mode()
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


def compute_metrics(frame, assume_absent_zero=False):
    """Add the Gate 0 ratios to a wide per-year frame.

    Every ratio propagates nulls: a company missing ``sbc`` has unknown
    FCF-after-SBC, not higher FCF.
    """
    frame = frame.sort(["cik", "fiscal_year"])

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

    frame = frame.with_columns(
        (pl.col("equity") - goodwill - intangibles).alias("tangible_book"),
        (pl.col("ocf") - pl.col("capex")).alias("fcf"),
        (pl.col("ocf") - pl.col("capex") - pl.col("sbc")).alias("fcf_after_sbc"),
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

    return frame.drop("_gate0_pass_raw")


def build_ttm(facts):
    """Trailing-twelve-month sums, where four discrete quarters actually exist.

    Many filers report year-to-date rather than discrete quarters, and a discrete
    Q4 is never filed on its own. Where four quarters are not available the TTM
    columns stay null rather than being stitched together from mismatched periods.
    """
    quarters = facts.filter(pl.col("fiscal_period").is_in(["Q1", "Q2", "Q3", "Q4"]))
    if quarters.is_empty():
        return pl.DataFrame(schema={"cik": pl.Int64})

    recent = (
        quarters.sort(["cik", "concept", "period_end"])
        .group_by(["cik", "concept"])
        .tail(4)
        .group_by(["cik", "concept"])
        .agg(pl.col("value").sum().alias("ttm_value"), pl.len().alias("n_quarters"))
        .filter(pl.col("n_quarters") == 4)
    )
    if recent.is_empty():
        return pl.DataFrame(schema={"cik": pl.Int64})

    wide = recent.pivot(
        on="concept", index="cik", values="ttm_value", aggregate_function="first"
    )
    wide = wide.rename({c: f"ttm_{c}" for c in wide.columns if c != "cik"})
    for needed in ("ttm_ocf", "ttm_capex", "ttm_sbc"):
        if needed not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(needed))
    return wide.with_columns(
        (pl.col("ttm_ocf") - pl.col("ttm_capex") - pl.col("ttm_sbc")).alias(
            "ttm_fcf_after_sbc"
        )
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
    "gate0_status",
    "gate0_not_evaluable",
    "imputed_fields",
    "resolution_basis",
    "resolution_basis_goodwill",
    "resolution_basis_intangibles",
    "carried_forward_fields",
    "carry_forward_age_days",
    "sbc_unverified",
    "price",
    "ma_200",
    "pct_vs_200ma",
    "market_cap",
    "ev",
    "fcf_after_sbc_multiple",
    "p_fcf_after_sbc",
    "ev_fcf_after_sbc",
    "tangible_book",
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
    "ttm_sbc",
    "ttm_fcf_after_sbc",
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


def load_universe(paths, assume_absent_zero=False, allow_imputed=False):
    """Join facts, trends, TTM and company metadata into one row per company."""
    facts = pl.read_parquet(paths.facts)
    meta = pl.read_parquet(paths.meta)

    annual = compute_metrics(widen(facts), assume_absent_zero)
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
    ttm = build_ttm(facts)
    provenance = _company_provenance(facts)
    acceleration = build_quarterly_acceleration(facts)

    frame = latest.join(trends, on=["cik", "latest_fiscal_year"], how="left")
    frame = frame.join(fcf_inflection, on="cik", how="left")
    if ttm.width > 1:
        frame = frame.join(ttm, on="cik", how="left")
    frame = frame.join(provenance, on="cik", how="left")
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
    frame = load_universe(paths, args.assume_absent_zero, args.allow_imputed)
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
