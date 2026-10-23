"""Frozen regression cases for framework audit F-02/03/05/31/32."""

import importlib.util
from pathlib import Path

import polars as pl
import pytest

import gate0
import rank_queue
from concepts import CONCEPTS_BY_NAME, DIAGNOSTIC_CONCEPT_NAMES


def _dilution_history(years=(2022, 2023, 2024, 2025), shares=(100., 101., 102., 106.)):
    return gate0.build_dilution_history(pl.DataFrame({
        "cik": [1] * len(years), "fiscal_year": years, "shares_diluted": shares,
    }))


def _bridge(**kwargs):
    row = {
        "sic": "3500", "gate0_pass": True, "fail_tax_anomaly": False,
        "fail_sbc": False, "fail_ni_over_oi": False, "dilution_yoy": .04,
        "dilution_cagr_3y": .03, "sbc": 2., "acquisition_shares": 0.,
        "shares_diluted_prior_fy": 100.,
        **{f"{c}_measured": True for c in gate0.DILUTION_ISSUANCE_CONCEPTS},
        "gate0_not_evaluable": "adjusted_vs_gaap",
    }
    row.update(kwargs)
    return gate0.add_dilution_verdict(pl.DataFrame([row])).row(0, named=True)


def test_f02_latest_fy_growth_and_three_year_context():
    row = _dilution_history().row(0, named=True)
    assert row["dilution_yoy"] == pytest.approx(106 / 102 - 1)
    assert row["dilution_cagr_3y"] == pytest.approx(1.06 ** (1 / 3) - 1)
    assert row["shares_diluted_prior_fy"] == 102.


def test_f02_missing_exact_prior_year_cannot_stretch_the_window():
    row = _dilution_history((2022, 2023, 2025), (100., 101., 106.)).row(0, named=True)
    assert row["dilution_yoy"] is None
    assert row["dilution_cagr_3y"] is not None
    assert _bridge(dilution_yoy=None)["test_dilution"] == "NOT_EVALUABLE"


@pytest.mark.parametrize("shares", [(100., 101., 102., 10600.), (100., 1000., 100., 106.)])
def test_f02_split_or_scale_break_withholds_both_numbers(shares):
    row = _dilution_history(shares=shares).row(0, named=True)
    assert row["dilution_yoy"] is None
    assert row["dilution_cagr_3y"] is None
    result = _bridge(shares_history_scale_break=True)
    assert result["test_dilution"] == "NOT_EVALUABLE"
    assert result["gate0_framework_pass"] is None


@pytest.mark.parametrize("growth,sbc,result", [(.025, 2., "PASS"), (.025001, 2., "FAIL"), (.04, 0., "PASS"), (-.1, 2., "PASS")])
def test_f02_dilution_threshold_and_measured_sbc(growth, sbc, result):
    row = _bridge(dilution_yoy=growth, sbc=sbc)
    assert row["test_dilution"] == result
    assert row["gate0_framework_pass"] is (result == "PASS")
    assert ("dilution" in row["framework_leg_failed"].split(",")) is (result == "FAIL")


@pytest.mark.parametrize("missing", [{"acquisition_shares": 1.}, {"dilution_yoy": None}, {"sbc": None}, {"sbc_assumed_zero": True}])
def test_f02_attribution_missing_shares_and_assumed_sbc_are_owed(missing):
    row = _bridge(**missing)
    assert row["test_dilution"] == "NOT_EVALUABLE"
    assert row["gate0_framework_pass"] is None
    assert row["dilution_attribution_owed"] is True
    assert "dilution" in row["gate0_not_evaluable"].split(",")


def test_f02_dilution_binds_financials_too_and_failure_dominates_unknown():
    assert _bridge(sic="6300")["gate0_framework_pass"] is False
    row = _bridge(dilution_yoy=None, fail_tax_anomaly=True)
    assert row["gate0_framework_pass"] is False
    assert row["framework_leg_failed"] == "tax_anomaly"


def test_f31_missing_tax_input_cannot_count_as_framework_pass():
    assert _bridge(dilution_yoy=.02, fail_tax_anomaly=None)["gate0_framework_pass"] is None


@pytest.mark.parametrize("core_status", ["PASS", "NOT_EVALUABLE", None])
def test_false_legacy_pass_without_failed_core_leg_is_unknown(core_status):
    row = _bridge(gate0_pass=False, dilution_yoy=.02, test_tangible_book=core_status)
    assert row["gate0_framework_pass"] is None
    assert row["framework_leg_failed"] == ""


@pytest.mark.parametrize("leg", [*gate0.LOAD_BEARING_TESTS, "fcf_after_sbc"])
def test_actual_failed_core_leg_dominates_unknown_and_is_named(leg):
    row = _bridge(gate0_pass=False, dilution_yoy=None, **{f"test_{leg}": "FAIL"})
    assert row["gate0_framework_pass"] is False
    assert row["framework_leg_failed"] == leg


def test_f02_acquisition_tags_are_share_unit_diagnostics():
    concept = CONCEPTS_BY_NAME["acquisition_shares"]
    assert concept.unit == "shares"
    assert "acquisition_shares" in DIAGNOSTIC_CONCEPT_NAMES
    assert concept.chain == (
        "StockIssuedDuringPeriodSharesAcquisitions",
        "BusinessAcquisitionEquityInterestsIssuedOrIssuableNumberOfSharesIssued",
    )


@pytest.mark.parametrize("concept", gate0.DILUTION_ISSUANCE_CONCEPTS)
def test_f02_no_measurement_for_filer_is_owed_even_with_registered_chain(concept):
    assert concept in DIAGNOSTIC_CONCEPT_NAMES
    # Cash equity is measured through either shares or proceeds.
    missing = {f"{concept}_measured": False}
    if concept.startswith("equity_issuance"):
        missing.update(equity_issuance_shares_measured=False, equity_issuance_proceeds_measured=False)
    row = _bridge(**missing)
    assert row["test_dilution"] == "NOT_EVALUABLE"
    assert row["dilution_attribution_owed"] is True


@pytest.mark.parametrize("concept", [c for c in gate0.DILUTION_ISSUANCE_CONCEPTS if c.endswith("shares")])
@pytest.mark.parametrize("value,status", [(None, "FAIL"), (0., "FAIL"), (.5, "FAIL"), (.500001, "NOT_EVALUABLE")])
def test_f02_measured_absent_and_half_percent_boundary(concept, value, status):
    assert _bridge(**{concept: value})["test_dilution"] == status


def test_f02_cash_equity_proceeds_require_share_sizing_or_zero():
    assert _bridge(equity_issuance_shares_measured=False, equity_issuance_proceeds=0.)["test_dilution"] == "FAIL"
    assert _bridge(equity_issuance_shares_measured=False, equity_issuance_proceeds=1.)["test_dilution"] == "NOT_EVALUABLE"
    assert _bridge(equity_issuance_proceeds=1.)["test_dilution"] == "NOT_EVALUABLE"
    assert _bridge(equity_issuance_shares=.1, equity_issuance_proceeds=1.)["test_dilution"] == "FAIL"


@pytest.mark.parametrize("sbc", [None, 0., 2.])
def test_f02_net_growth_below_threshold_passes_without_attribution(sbc):
    missing = {f"{c}_measured": False for c in gate0.DILUTION_ISSUANCE_CONCEPTS}
    row = _bridge(dilution_yoy=.025, sbc=sbc, sbc_assumed_zero=True, **missing)
    assert row["test_dilution"] == "PASS"
    assert row["dilution_attribution_owed"] is False


def test_f02_measurement_is_filer_specific_and_uses_any_stored_year():
    facts = pl.DataFrame({"cik": [1, 1, 2, 3], "fiscal_year": [2020, 2024, 2024, 2024],
                          "concept": ["acquisition_shares", "sbc", "sbc", "acquisition_shares"],
                          "value": [0., 2., 2., None]})
    flags = {r["cik"]: r for r in gate0.build_dilution_measurement(facts).to_dicts()}
    assert flags[1]["acquisition_shares_measured"] is True
    assert flags[2]["acquisition_shares_measured"] is False
    assert flags[3]["acquisition_shares_measured"] is False
    assert all(not row["debt_conversion_shares_measured"] for row in flags.values())


def test_f02_missing_entire_extraction_build_cannot_fail_high_growth():
    row = _bridge(**{f"{c}_measured": False for c in gate0.DILUTION_ISSUANCE_CONCEPTS})
    assert row["test_dilution"] == "NOT_EVALUABLE"
    assert row["gate0_framework_pass"] is None


def test_f03_adjusted_reconciliation_is_always_explicitly_owed():
    row = {column: False for column in gate0.FLAG_COLUMNS}
    result = gate0.add_verdict(pl.DataFrame([row])).row(0, named=True)
    assert result["test_adjusted_vs_gaap"] == "NOT_EVALUABLE"
    assert "adjusted_vs_gaap" in result["gate0_not_evaluable"].split(",")


@pytest.mark.parametrize("fy_sbc", [None, 12.])
def test_f05_missing_ttm_sbc_never_manufactures_positive_cash(fy_sbc):
    frame = pl.DataFrame([{
        "ttm_ocf": 100., "ttm_capex": 20., "ttm_sbc": None,
        "sbc": fy_sbc, "ttm_lease_payments": 30., "ttm_fcf_after_sbc": 80.,
    }])
    row = gate0.resolve_ttm_sbc(frame).row(0, named=True)
    assert row["ttm_fcf_after_sbc"] is None
    assert row["ttm_lease_payments"] == 30.
    assert row["ttm_sbc_owed"] is True
    assert row["ttm_sbc_assumed_zero"] is False


@pytest.mark.parametrize("capex,ocf,expected", [(20., 100., 50.), (None, 100., None), (-20., 100., None), (20., None, None)])
def test_f05_reported_zero_annual_sbc_recovers_only_usable_cash_and_retains_leases(capex, ocf, expected):
    row = gate0.resolve_ttm_sbc(pl.DataFrame([{
        "ttm_ocf": ocf, "ttm_capex": capex, "ttm_sbc": None, "sbc": 0.,
        "ttm_lease_payments": 30., "ttm_fcf_after_sbc": None,
    }])).row(0, named=True)
    assert row["ttm_fcf_after_sbc"] == expected
    assert row["ttm_sbc_zero_reported"] is True
    assert row["ttm_sbc_owed"] is False


def test_f05_imputed_annual_zero_is_not_reported_zero():
    row = gate0.resolve_ttm_sbc(pl.DataFrame([{
        "ttm_ocf": 100., "ttm_capex": 20., "ttm_sbc": None, "sbc": 0.,
        "sbc_assumed_zero": True, "ttm_fcf_after_sbc": None,
    }])).row(0, named=True)
    assert row["ttm_fcf_after_sbc"] is None
    assert row["ttm_sbc_zero_reported"] is False
    assert row["ttm_sbc_owed"] is True


def test_f05_measured_sbc_retains_known_lease_deduction():
    frame = pl.DataFrame([{
        "ttm_ocf": 100., "ttm_capex": 20., "ttm_sbc": 5.,
        "sbc": 5., "ttm_lease_payments": 30., "ttm_fcf_after_sbc": 75.,
    }])
    row = gate0.resolve_ttm_sbc(frame).row(0, named=True)
    assert row["ttm_fcf_after_sbc"] == 45.
    assert row["ttm_sbc_owed"] is False


def test_f05_null_ttm_cash_cannot_satisfy_positive_refill_cut():
    frame = pl.DataFrame([{
        "ticker": "OWED", "filing_form": "10-K", "sic": "3500",
        "gate0_framework_pass": True, "revenue": 100e6,
        "ttm_fcf_after_sbc": None, "ttm_sbc_owed": True,
        "revenue_cagr_3y": .05, "fcf_per_share_cagr_3y": .1,
        "fcf_per_share_cagr_5y": .1,
    }])
    rows, funnel = rank_queue.direct_pass(frame, None, set())
    assert rows.is_empty()
    assert dict(funnel)["TTM FCF-after-SBC above 0"] == 0


@pytest.mark.parametrize("tax,failed,warn", [(0., True, False), (-.02, True, False), (.03, False, True), (.05, False, True), (.051, False, False), (None, None, None)])
def test_f31_only_nonpositive_tax_fails(tax, failed, warn):
    frame = pl.DataFrame([{
        "tangible_book": 100., "income_quality": 1., "fcf": 20., "fcf_after_sbc": 18.,
        "sbc": 2., "sbc_pct_revenue": .02, "net_income": 10., "operating_income": 20.,
        "pretax_income": 12., "tax_expense": 2.,
        "effective_tax": tax,
    }])
    row = gate0.add_flags(frame).row(0, named=True)
    assert row["fail_tax_anomaly"] is failed
    assert row["warn_low_tax"] is warn


@pytest.mark.parametrize("ttm,ttm_ocf,status,conflict,expected", [
    (0., 30., "pass", False, True), (-2., 30., "pass_stale", False, True),
    (-2., -10., "pass", False, False), (-2., 30., "pass", True, False),
    (None, 30., "pass", False, False), (-2., 30., "fail", False, False),
])
def test_f32_verified_nonpositive_ttm_makes_annual_pass_provisional(ttm, ttm_ocf, status, conflict, expected):
    frame = pl.DataFrame([{
        "ocf": 100., "fcf_after_sbc": 20., "ttm_ocf": ttm_ocf,
        "ttm_fcf_after_sbc": ttm, "gate0_status": status, "ttm_window_misaligned": conflict,
    }])
    row = gate0.add_gate0_provisional(gate0.add_ttm_divergence(frame)).row(0, named=True)
    assert row["gate0_provisional"] is expected


def test_f02_f03_f22_f32_fact_pack_exposes_numbers_and_obligations():
    path = Path(__file__).resolve().parents[2] / ".claude/screening/fact_pack.py"
    if not path.is_file():
        pytest.skip("no .claude/screening beside this code tree")
    spec = importlib.util.spec_from_file_location("audit_fact_pack", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row = {"dilution_yoy": "0.04", "dilution_cagr_3y": "0.03", "test_dilution": "FAIL",
           "test_adjusted_vs_gaap": "NOT_EVALUABLE", "gate0_provisional": "true"}
    pack = module.row_pack(list(row), row, None)
    assert pack["shares"]["dilution_yoy"] == .04
    assert pack["shares"]["dilution_cagr_3y"] == .03
    assert pack["gate0"]["gate0_provisional"] is True
    assert pack["adjusted_vs_gaap_obligation"] == "adjusted-vs-GAAP §C-1 reconciliation: OWED"
    assert "adjusted_vs_gaap" in pack["gate0"]["blocking_not_evaluable"]
