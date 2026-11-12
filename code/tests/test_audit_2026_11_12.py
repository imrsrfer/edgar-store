"""Regression tests for the 2026-11-11 audit, second patch (2026-11-12).

a. gate0_pass_strict: the strict pass beside the frozen gate0_pass.
b. build_eps.py: the forward/trailing P/E file the adjusted-EPS guard needs.
c. fact_pack.parse: free-text columns are never type-guessed.
d. build_facts: a tie between side-by-side tags no longer follows hash order.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import polars as pl
import pytest

import build_eps
import build_facts
import gate0
import screen
from concepts import CONCEPTS_BY_NAME

MILLION = 1e6


# --------------------------------------------------------------------------
# a. gate0_pass_strict
# --------------------------------------------------------------------------


def _flagged(**overrides):
    row = dict(cik=1, fiscal_year=2025, equity=500 * MILLION, goodwill=0.0, intangibles=0.0,
               total_debt=0.0, cash=50 * MILLION, revenue=1000 * MILLION,
               net_income=100 * MILLION, operating_income=140 * MILLION, ocf=120 * MILLION,
               capex=20 * MILLION, sbc=10 * MILLION, acquisitions=0.0, buybacks=0.0,
               dividends=0.0, dep_amort=20 * MILLION, tax_expense=30 * MILLION,
               pretax_income=130 * MILLION, shares_diluted=50 * MILLION)
    row.update(overrides)
    floats = {k: pl.Float64 for k in row if k not in ("cik", "fiscal_year")}
    return gate0.add_flags(gate0.compute_metrics(pl.DataFrame([row], schema_overrides=floats)))


def _verdict(**overrides):
    return gate0.add_verdict(_flagged(**overrides)).row(0, named=True)


def test_strict_pass_requires_the_master_metric_to_have_passed():
    row = _verdict()

    assert row["gate0_pass"] is True
    assert row["test_fcf_after_sbc"] == "PASS"
    assert row["gate0_pass_strict"] is True


def test_missing_sbc_keeps_gate0_pass_but_not_the_strict_pass():
    # Arrange: SBC is absent, so FCF-after-SBC cannot be computed (Exxon shape).
    row = _verdict(sbc=None)

    # Assert: the frozen column is unchanged, the strict one says no.
    assert row["fcf_after_sbc"] is None
    assert row["gate0_pass"] is True and row["gate0_status"] == "pass"
    assert row["gate0_pass_strict"] is False


def test_a_failing_row_is_not_a_strict_pass():
    row = _verdict(ocf=10 * MILLION)

    assert row["gate0_pass"] is False
    assert row["gate0_pass_strict"] is False


def test_strict_pass_is_null_when_the_leg_was_never_built():
    # Arrange: an otherwise passing row from a frame with no fcf_after_sbc test.
    frame = _flagged().drop("fail_fcf_after_sbc")

    row = gate0.add_verdict(frame).row(0, named=True)

    assert row["gate0_pass"] is True
    assert row["gate0_pass_strict"] is None


def test_strict_pass_is_published_next_to_gate0_pass():
    order = list(gate0.OUTPUT_ORDER)
    assert order.index("gate0_pass_strict") == order.index("gate0_pass") + 1


# --------------------------------------------------------------------------
# b. build_eps.py
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, 0, 0.0, -12.5, float("nan"), float("inf"), "n/a", True])
def test_only_a_finite_positive_multiple_is_kept(value):
    assert build_eps.positive_multiple(value) is None


def test_a_positive_multiple_is_kept_as_a_float():
    assert build_eps.positive_multiple("13.1") == 13.1
    assert build_eps.positive_multiple(8.6) == 8.6


def test_fetch_failure_still_writes_an_unmeasured_row():
    def fetch(ticker):
        if ticker == "BROKEN":
            raise RuntimeError("provider down")
        return {"forwardPE": 8.6, "trailingPE": 13.1}

    rows, failed = build_eps.fetch_rows(["GOOD", "BROKEN"], date(2026, 11, 12), fetch)

    assert failed == ["BROKEN"]
    assert rows[0] == {"ticker": "GOOD", "fwd_pe": 8.6, "trail_pe": 13.1,
                       "as_of": "2026-11-12", "source": "yfinance.info"}
    assert rows[1]["fwd_pe"] is None and rows[1]["trail_pe"] is None


def test_eps_file_feeds_the_guard_and_a_negative_forward_never_fires_it(tmp_path):
    # Arrange
    info = {
        "FLAG": {"forwardPE": 5.0, "trailingPE": 10.0},     # 0.50 -> flagged
        "CLEAR": {"forwardPE": 9.0, "trailingPE": 10.0},    # 0.90 -> not flagged
        "LOSS": {"forwardPE": -4.0, "trailingPE": 10.0},    # forecast loss -> not measured
        "NODATA": {},
    }
    rows, _ = build_eps.fetch_rows(sorted(info), date(2026, 11, 12), info.__getitem__)
    path = tmp_path / "eps.csv"
    build_eps.build_frame(rows).write_csv(path)

    # Act: the same path the lanes take.
    eps = screen.load_eps(str(path))
    survivors = pl.DataFrame({"ticker": sorted(info)}).join(eps, on="ticker", how="left")
    guard = dict(screen.apply_eps_guard(survivors, True)
                 .select("ticker", "adjusted_eps_guard").iter_rows())

    # Assert
    assert guard == {"FLAG": True, "CLEAR": False, "LOSS": None, "NODATA": None}


def test_default_output_is_eps_csv_in_the_data_root(tmp_path):
    paths = build_eps.Paths(tmp_path)

    assert build_eps.resolve_out_path(paths, None) == paths.root / "eps.csv"
    assert build_eps.resolve_out_path(paths, "other.csv") == paths.root / "other.csv"


# --------------------------------------------------------------------------
# c. fact_pack.parse
# --------------------------------------------------------------------------


def _fact_pack():
    path = Path(__file__).resolve().parents[2] / ".claude/screening/fact_pack.py"
    if not path.is_file():
        pytest.skip("no .claude/screening beside this code tree")
    spec = importlib.util.spec_from_file_location("audit_fact_pack_1112", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("ticker", ["TRUE", "NAN", "INF", "1E5", "FALSE"])
def test_a_ticker_that_looks_like_a_value_stays_text(ticker):
    fact_pack = _fact_pack()

    assert fact_pack.parse(ticker, "ticker") == ticker


def test_numeric_and_boolean_columns_still_parse():
    fact_pack = _fact_pack()

    assert fact_pack.parse("true", "gate0_pass") is True
    assert fact_pack.parse("12.5", "revenue") == 12.5
    assert fact_pack.parse("", "revenue") is None


def test_row_pack_keeps_a_value_like_ticker_and_shows_the_strict_pass():
    fact_pack = _fact_pack()
    row = {"ticker": "TRUE", "company_name": "NAN", "gate0_pass": "true",
           "gate0_pass_strict": "false"}

    pack = fact_pack.row_pack(list(row), row, None)

    assert pack["identity"]["ticker"] == "TRUE"
    assert pack["identity"]["company_name"] == "NAN"
    assert pack["gate0"]["gate0_pass"] is True
    assert pack["gate0"]["gate0_pass_strict"] is False


# --------------------------------------------------------------------------
# d. build_facts: tag ties do not follow hash order
# --------------------------------------------------------------------------


def _fact(tag, value):
    return {"tag": tag, "taxonomy": "us-gaap", "unit": "USD", "start": date(2025, 1, 1),
            "end": date(2025, 12, 31), "value": value, "accn": "A-1", "fy": 2025,
            "fp": "FY", "form": "10-K", "filed": date(2026, 2, 1),
            "fiscal_year": 2025, "fiscal_period": "FY"}


def _index(tags):
    return {(tag, 2025, "FY"): [_fact(tag, 100.0)] for tag in tags}


def test_sibling_order_does_not_depend_on_the_order_tags_are_iterated():
    concept = CONCEPTS_BY_NAME["revenue_subset_tested"]
    tags = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet")

    # Act: the three orders a frozenset can hand the same tags back in.
    orders = [tags, tags[::-1], (tags[1], tags[2], tags[0])]
    results = [build_facts._siblings_by_accession(o, 2025, "FY", _index(tags), concept)
               for o in orders]

    # Assert
    assert results[0] == results[1] == results[2]
    assert [tag for tag, _ in results[0]["A-1"]] == sorted(tags)


def test_equal_valued_side_by_side_tags_resolve_to_one_source_tag():
    """All three tags report 100: the recorded tag is the same every build."""
    concept = CONCEPTS_BY_NAME["revenue_subset_tested"]
    tags = sorted(concept.subset_tested_of)

    row = build_facts._resolve_subset_tested(concept, 2025, "FY", _index(tags))

    assert row["value"] == 3.0
    assert row["source_tag"] == tags[0]
