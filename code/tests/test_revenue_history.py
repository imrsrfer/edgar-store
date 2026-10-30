"""Regression tests for revenue_subset_history_suspect (W3b, IONQ shape).

The W3 side-by-side max rule fixes the LATEST fiscal year's revenue, but a
SPAC-era mis-tagged Revenues poisons a filer's HISTORY: IONQ's FY2022 reads
1,235.0M against a true ~11.1M (the subset candidate), Biora's 2020 reads
74.3M against 162k. An inflated base year lowers revenue CAGR (fail-closed)
but SHRINKS base-year operating margin, so the margin-expansion legs
(operating_margin_2y_ago, the margin2y lane, inflection) read BETTER than
truth -- fail-open. This flag names rows whose history rests on a disputed
tag; it changes no value and moves no verdict.
"""

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store_defects  # noqa: E402

FACT_SCHEMA = {
    "cik": pl.Int64,
    "concept": pl.Utf8,
    "fiscal_year": pl.Int32,
    "fiscal_period": pl.Utf8,
    "value": pl.Float64,
}


def _facts(rows):
    """rows: (cik, concept, fiscal_year, value); all fiscal_period=FY."""
    return pl.DataFrame(
        [
            {"cik": cik, "concept": concept, "fiscal_year": fy,
             "fiscal_period": "FY", "value": float(value)}
            for cik, concept, fy, value in rows
        ],
        schema=FACT_SCHEMA,
    )


def _frame(cik, latest_fiscal_year):
    return pl.DataFrame(
        [{"cik": cik, "latest_fiscal_year": latest_fiscal_year}],
        schema={"cik": pl.Int64, "latest_fiscal_year": pl.Int32},
    )


def _flag(facts, frame):
    return store_defects._revenue_subset_history_suspect(facts, frame)


def test_ionq_shape_fires():
    """IONQ: FY2022 revenue 1,235.0M beside a subset candidate of 11.131M --
    a 111x disagreement inside the trend window (latest FY2025)."""
    facts = _facts([
        (1824920, "revenue", 2022, 1_235_000_000),
        (1824920, "revenue_subset_candidate", 2022, 11_131_000),
        (1824920, "revenue", 2023, 22_042_000),
        (1824920, "revenue", 2024, 43_073_000),
        (1824920, "revenue", 2025, 130_016_000),
    ])
    out = _flag(facts, _frame(1824920, 2025))
    assert out["revenue_subset_history_suspect"][0] is True


def test_a_disagreement_on_the_latest_year_only_does_not_fire():
    """SEZL/AER/OSCR shape: the big disagreement sits on the LATEST year,
    where the max rule already ships the correct total and
    revenue_subset_suspect names it. History is clean, so this flag stays
    False -- the two flags answer different questions."""
    facts = _facts([
        (1, "revenue", 2023, 100_000_000),
        (1, "revenue", 2024, 120_000_000),
        (1, "revenue", 2025, 450_279_000),
        (1, "revenue_subset_candidate", 2025, 235_878_000),
    ])
    out = _flag(facts, _frame(1, 2025))
    assert out["revenue_subset_history_suspect"][0] is False


def test_a_small_historical_disagreement_does_not_fire():
    """RUSHA shape in a prior year: 5% is a real subset gap but not the
    mis-tag shape this flag exists for."""
    facts = _facts([
        (2, "revenue", 2023, 7_064_640_000),
        (2, "revenue_subset_candidate", 2023, 6_900_000_000),
        (2, "revenue", 2024, 7_434_195_000),
    ])
    out = _flag(facts, _frame(2, 2024))
    assert out["revenue_subset_history_suspect"][0] is False


def test_a_clean_history_is_measured_false():
    facts = _facts([
        (3, "revenue", 2021, 100_000_000),
        (3, "revenue", 2022, 110_000_000),
        (3, "revenue", 2023, 120_000_000),
        (3, "revenue", 2024, 130_000_000),
        (3, "revenue", 2025, 140_000_000),
    ])
    out = _flag(facts, _frame(3, 2025))
    assert out["revenue_subset_history_suspect"][0] is False


def test_no_revenue_history_is_null_not_false():
    """Missing is never zero: with no revenue facts at all nothing was
    measured, and the flag is null rather than a False that reads as
    checked-and-clean."""
    facts = _facts([(4, "ocf", 2024, 10_000_000)])
    out = _flag(facts, _frame(4, 2024))
    assert out["revenue_subset_history_suspect"][0] is None


def test_a_zero_candidate_with_real_revenue_fires():
    """A reported ZERO subset beside a real total is an infinite ratio --
    the extreme of the mis-tag shape."""
    facts = _facts([
        (5, "revenue", 2023, 4_700_000_000),
        (5, "revenue_subset_candidate", 2023, 0),
        (5, "revenue", 2024, 5_000_000_000),
    ])
    out = _flag(facts, _frame(5, 2024))
    assert out["revenue_subset_history_suspect"][0] is True


def test_a_disagreement_outside_the_window_does_not_fire():
    """The window feeding the CAGRs and the 2-year margin leg is
    latest-5 .. latest-1. A poisoned year older than that feeds nothing."""
    facts = _facts([
        (6, "revenue", 2019, 1_235_000_000),
        (6, "revenue_subset_candidate", 2019, 11_131_000),
        (6, "revenue", 2023, 100_000_000),
        (6, "revenue", 2024, 110_000_000),
        (6, "revenue", 2025, 130_000_000),
    ])
    out = _flag(facts, _frame(6, 2025))
    assert out["revenue_subset_history_suspect"][0] is False


def test_interim_candidates_are_ignored():
    """Only FY rows feed the trend columns; a YTD disagreement must not
    fire an annual-history flag."""
    facts = pl.DataFrame(
        [
            {"cik": 7, "concept": "revenue", "fiscal_year": 2023,
             "fiscal_period": "YTD3", "value": 1_070_000_000.0},
            {"cik": 7, "concept": "revenue_subset_candidate", "fiscal_year": 2023,
             "fiscal_period": "YTD3", "value": 451_000.0},
            {"cik": 7, "concept": "revenue", "fiscal_year": 2023,
             "fiscal_period": "FY", "value": 10_100_000.0},
            {"cik": 7, "concept": "revenue", "fiscal_year": 2024,
             "fiscal_period": "FY", "value": 12_000_000.0},
        ],
        schema=FACT_SCHEMA,
    )
    out = _flag(facts, _frame(7, 2024))
    assert out["revenue_subset_history_suspect"][0] is False
