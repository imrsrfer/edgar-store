"""Tests for investing_unreconciled / investing_residual.

The flag answers one question: does the investing statement CLOSE using only
tags this pipeline can read? It is an OBLIGATION, not a correction -- nothing
downstream consumes the residual as a number, and no capex is adjusted by it.

Two failure modes have to stay pinned, both found by measuring the real store
before the flag was written:

  * the PYPL shape -- a filer with securities/lending activity tags a parent
    total AND its components, so a flat sum double-counts (+$61.0bn of named
    legs against a +$0.80bn investing total). The flag must go NULL there, not
    False, and certainly not True.
  * the IFRS shape -- IFRS names its investing legs by the suffix
    ClassifiedAsInvestingActivities, not by a Payments/Proceeds prefix. Miss
    them and every IFRS filer shows a residual equal to its whole statement.
"""

from __future__ import annotations

import polars as pl
import pytest

import gate0
from concepts import CONCEPTS_BY_NAME, WANTED_TAGS
from edgar_lib import Paths


MILLION = 1e6


def _frame(**overrides):
    row = {
        "cik": 1,
        "ticker": "TEST",
        "reporting_currency": "USD",
        "investing_cf": None,
        "investing_outflows": None,
        "investing_inflows": None,
        "investing_portfolio": None,
    }
    row.update(overrides)
    return pl.DataFrame([row])


def _flag(**overrides):
    out = gate0._add_investing_reconciliation(_frame(**overrides)).to_dicts()[0]
    return out["investing_unreconciled"], out["investing_residual"]


# --------------------------------------------------------------------------
# The identity itself
# --------------------------------------------------------------------------


def test_statement_that_closes_is_not_flagged():
    """The GIC shape: capex 3.1 + acquisitions 4.0 == 7.1 total. Closes."""
    flag, residual = _flag(investing_cf=-7.1 * MILLION, investing_outflows=7.1 * MILLION)
    assert flag is False
    assert residual == pytest.approx(0.0, abs=1.0)


def test_unexplained_outflow_is_flagged_with_a_signed_residual():
    """The OMCL shape: a real investing leg carried by an extension tag."""
    flag, residual = _flag(
        investing_cf=-60.363 * MILLION, investing_outflows=40.415 * MILLION
    )
    assert flag is True
    # NEGATIVE: unexplained OUTFLOW, the direction hidden capex takes.
    assert residual == pytest.approx(-19.948 * MILLION, rel=1e-6)


def test_residual_is_signed_so_an_unexplained_inflow_is_distinguishable():
    flag, residual = _flag(investing_cf=10.0 * MILLION, investing_outflows=0.0)
    assert flag is True
    assert residual > 0


def test_inflows_are_subtracted_not_added():
    """total = -(outflows) + inflows, so a disposal must close the gap."""
    flag, residual = _flag(
        investing_cf=-5.0 * MILLION,
        investing_outflows=8.0 * MILLION,
        investing_inflows=3.0 * MILLION,
    )
    assert flag is False
    assert residual == pytest.approx(0.0, abs=1.0)


def test_absent_leg_contributes_nothing_rather_than_voiding_the_test():
    """A filer with no disposals genuinely has no disposals line."""
    flag, residual = _flag(investing_cf=-7.0 * MILLION, investing_outflows=7.0 * MILLION)
    assert flag is False
    assert residual == pytest.approx(0.0, abs=1.0)


# --------------------------------------------------------------------------
# Tolerance: both bars, not either
# --------------------------------------------------------------------------


def test_small_absolute_residual_does_not_flag_a_large_filer():
    """$0.5M against $10bn of investing is rounding, not a missing leg."""
    flag, _ = _flag(
        investing_cf=-10_000.0 * MILLION, investing_outflows=10_000.5 * MILLION
    )
    assert flag is False


def test_large_percentage_but_tiny_dollars_does_not_flag():
    """A percentage bar alone screams at micro-caps."""
    flag, _ = _flag(investing_cf=-1.0 * MILLION, investing_outflows=0.5 * MILLION)
    assert flag is False


def test_residual_must_clear_both_bars_to_flag():
    flag, _ = _flag(investing_cf=-100.0 * MILLION, investing_outflows=50.0 * MILLION)
    assert flag is True


# --------------------------------------------------------------------------
# 🔴 NULL, never False, wherever it could not be evaluated
# --------------------------------------------------------------------------


def test_portfolio_filer_is_null_not_false():
    """The PYPL shape. False would assert a reconciliation nobody performed."""
    flag, residual = _flag(
        investing_cf=797.0 * MILLION,
        investing_outflows=873.0 * MILLION,
        investing_portfolio=1.0 * MILLION,
    )
    assert flag is None
    assert residual is None


def test_missing_investing_total_is_null():
    flag, residual = _flag(investing_cf=None, investing_outflows=40.0 * MILLION)
    assert flag is None
    assert residual is None


def test_non_usd_reporting_currency_is_null():
    """investing_cf can resolve in EUR through the any-currency ifrs_chain
    while the legs are USD-gated. That residual is arithmetic about nothing."""
    flag, residual = _flag(
        investing_cf=-60.0 * MILLION,
        investing_outflows=40.0 * MILLION,
        reporting_currency="EUR",
    )
    assert flag is None
    assert residual is None


def test_store_built_before_these_concepts_yields_null_not_false():
    """A missing INPUT is not a passing test. An older facts.parquet has no
    investing_* columns at all; every row must go NULL."""
    older = pl.DataFrame([{"cik": 1, "ticker": "TEST", "reporting_currency": "USD"}])
    out = gate0._add_investing_reconciliation(older)
    assert out["investing_unreconciled"].to_list() == [None]
    assert out["investing_residual"].to_list() == [None]


# --------------------------------------------------------------------------
# Concept wiring
# --------------------------------------------------------------------------


def test_ifrs_legs_are_summed_components_not_a_first_match_chain():
    """ifrs_chain is FIRST-MATCH. An IFRS filer reporting both a PP&E purchase
    and an intangibles purchase would lose the second."""
    outflows = CONCEPTS_BY_NAME["investing_outflows"]
    assert outflows.ifrs_chain == ()
    for tag in (
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
        "CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities",
    ):
        assert tag in outflows.components, f"{tag} must be a summed component"


def test_ifrs_inflow_legs_are_present_and_on_the_inflow_side():
    inflows = CONCEPTS_BY_NAME["investing_inflows"]
    assert (
        "ProceedsFromSalesOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"
        in inflows.components
    )
    outflows = CONCEPTS_BY_NAME["investing_outflows"]
    assert not set(inflows.components) & set(outflows.components), (
        "a tag on both sides would cancel itself out of the identity"
    )


def test_investing_total_reads_both_taxonomies():
    total = CONCEPTS_BY_NAME["investing_cf"]
    assert "NetCashProvidedByUsedInInvestingActivities" in total.chain
    assert "CashFlowsFromUsedInInvestingActivities" in total.ifrs_chain


def test_portfolio_marker_covers_the_pypl_shape():
    """PayPal's securities lines are what made a flat sum double-count."""
    portfolio = CONCEPTS_BY_NAME["investing_portfolio"]
    assert "PaymentsToAcquireMarketableSecurities" in portfolio.components
    assert "PaymentsToAcquireInvestments" in portfolio.components
    assert "ProceedsFromSaleAndMaturityOfMarketableSecurities" in portfolio.components


def test_every_new_tag_reaches_the_parser_whitelist():
    """A tag the parser does not collect resolves to null and the flag would
    fire on filers that are fine."""
    for name in (
        "investing_cf",
        "investing_outflows",
        "investing_inflows",
        "investing_portfolio",
    ):
        for tag in CONCEPTS_BY_NAME[name].all_tags:
            assert tag in WANTED_TAGS, f"{name} -> {tag} not collected"


# --------------------------------------------------------------------------
# Integration: the controls, against the real store
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def universe():
    paths = Paths()
    if not paths.facts.exists() or not paths.meta.exists():
        pytest.skip("facts.parquet/meta.parquet not built; run build_facts.py first")
    frame = gate0.load_universe(paths)
    # Column PRESENCE is not enough: load_universe creates a column for every
    # concept, so a store built before these existed carries them all-null.
    if "investing_cf" not in frame.columns or frame["investing_cf"].is_null().all():
        pytest.skip(
            "store predates the investing_* concepts; re-run build_facts.py to "
            "exercise the reconciliation controls"
        )
    return frame


def _row(universe, ticker):
    match = universe.filter(pl.col("ticker") == ticker)
    assert match.height == 1, f"{ticker}: expected 1 row, got {match.height}"
    return match.to_dicts()[0]


def test_omcl_flags_with_a_residual_equal_to_its_extension_tagged_legs(universe):
    """OMNICELL FY2025, hand-verified against the 10-K investing section:
    net investing (60,363), PP&E purchases 40,415, and two extension-tagged
    lines -- external-use software 17,518 (omcl:PaymentsForSoftwareForExternalUse)
    and an asset acquisition 2,430 -- which sum to the residual exactly."""
    row = _row(universe, "OMCL")
    assert row["investing_unreconciled"] is True
    assert row["investing_residual"] == pytest.approx(-19.948 * MILLION, rel=0.001)


def test_gic_does_not_flag_because_its_statement_closes(universe):
    """Global Industrial: capex 3.1 + acquisitions 4.0 == 7.1 total."""
    row = _row(universe, "GIC")
    assert row["investing_unreconciled"] is False
    assert row["investing_residual"] == pytest.approx(0.0, abs=1000.0)


def test_pypl_does_not_flag_because_it_cannot_be_evaluated(universe):
    """PayPal reports gross securities churn, so the flat sum double-counts.
    The honest answer is NULL -- not False, which would claim it reconciles."""
    row = _row(universe, "PYPL")
    assert row["investing_unreconciled"] is None
    assert row["investing_residual"] is None
    assert row["investing_portfolio"] is not None
