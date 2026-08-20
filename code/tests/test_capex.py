"""Regression tests for the 2026-08-19 capex defect.

capex was a first-match-wins tag chain over eleven XBRL tags that a filer can
legitimately report SIDE BY SIDE. The first match won and the rest were
dropped, so capex was understated -- which overstates FCF, FCF/share, and
every P/FCF multiple, and disables fail_fcf, the one test that is supposed to
catch a company that does not generate cash. It failed OPEN, in the company's
favour, which is the direction a quality gate must never fail in.

Three shapes are pinned here because all three were found live in the store:
  - a residual tag winning ahead of the industry-primary tags (NOG)
  - a leg reported separately from the PP&E line (SKYW, LRN)
  - a negative capex, which ADDS to free cash flow (SAH)
"""

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import concepts  # noqa: E402
import gate0  # noqa: E402


CAPEX = concepts.CONCEPTS_BY_NAME["capex"]


# --------------------------------------------------------------------------
# concepts.py -- capex is a sum, and the residual tag can never win alone
# --------------------------------------------------------------------------


def test_capex_is_a_component_sum_not_a_first_match_chain():
    assert CAPEX.components, (
        "capex must be summed across its disjoint legs; a first-match chain "
        "silently drops every leg after the first"
    )
    assert CAPEX.partial_ok, (
        "most filers report one or two legs, so a subset is the normal case "
        "and must not be marked (partial)"
    )


def test_residual_other_ppe_tag_is_summed_never_a_lone_winner():
    """NOG: the residual tag sat THIRD in the old chain and matched at $0.76M,
    so the two oil-and-gas development tags never got read at all."""
    residual = "PaymentsToAcquireOtherPropertyPlantAndEquipment"
    assert residual in CAPEX.components
    assert residual not in CAPEX.chain, (
        "the residual 'other PP&E' line is a COMPLEMENT to the PP&E tag, "
        "never a substitute for it"
    )


def test_industry_primary_tags_are_all_components():
    """Every leg an E&P, a railroad, an airline or a software capitaliser
    reports separately has to be inside the sum, not queued behind it."""
    for tag in (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireOilAndGasProperty",
        "PaymentsToExploreAndDevelopOilAndGasProperties",
        "PaymentsToAcquireMachineryAndEquipment",
        "PaymentsToDevelopSoftware",
    ):
        assert tag in CAPEX.components, tag


def test_broad_productive_asset_totals_stay_out_of_the_sum():
    """These tags ALREADY include PP&E for filers that use them. Summing them
    with the PP&E leg double-counts, so they stay a fallback chain."""
    for tag in ("PaymentsToAcquireProductiveAssets", "PaymentsForProceedsFromProductiveAssets"):
        assert tag in CAPEX.chain
        assert tag not in CAPEX.components, tag


# --------------------------------------------------------------------------
# build_facts.py -- the sum itself
# --------------------------------------------------------------------------


def _index(entries):
    """Minimal (tag, fiscal_year, period) -> facts index for _resolve_*."""
    index = {}
    for tag, value in entries:
        index.setdefault((tag, 2025, "FY"), []).append(
            {
                "tag": tag,
                "value": value,
                "unit": "USD",
                "start": "2025-01-01",
                "end": "2025-12-31",
                "fiscal_year": 2025,
                "fiscal_period": "FY",
                "form": "10-K",
                "accn": "0000000000-25-000001",
                "filed": None,
                "taxonomy": "us-gaap",
            }
        )
    return index


def test_two_reported_legs_are_added_together():
    import build_facts

    row = build_facts._resolve_components(
        CAPEX,
        2025,
        "FY",
        _index(
            [
                ("PaymentsToAcquireOtherPropertyPlantAndEquipment", 759_000.0),
                ("PaymentsToExploreAndDevelopOilAndGasProperties", 812_000_000.0),
            ]
        ),
    )
    assert row is not None
    # The NOG shape: under the old chain this resolved to $0.76M.
    assert row["value"] == pytest.approx(812_759_000.0)
    assert "+" in row["source_tag"], "both legs must be named in source_tag"


def test_partial_ok_suppresses_the_partial_marker():
    import build_facts

    row = build_facts._resolve_components(
        CAPEX, 2025, "FY", _index([("PaymentsToAcquirePropertyPlantAndEquipment", 100.0)])
    )
    assert "(partial)" not in row["source_tag"]


def test_total_debt_still_marks_partial():
    """partial_ok must not leak: total_debt has two components and a filer
    missing one is genuinely notable."""
    import build_facts

    debt = concepts.CONCEPTS_BY_NAME["total_debt"]
    assert not debt.partial_ok
    index = {}
    for tag, value in [(debt.components[0], 500.0)]:
        index.setdefault((tag, 2025, "FY"), []).append(
            {
                "tag": tag,
                "value": value,
                "unit": "USD",
                "start": None,
                "end": None,
                "fiscal_year": 2025,
                "fiscal_period": "FY",
                "form": "10-K",
                "accn": "0000000000-25-000001",
                "filed": None,
                "taxonomy": "us-gaap",
            }
        )
    row = build_facts._resolve_components(debt, 2025, "FY", index)
    assert "(partial)" in row["source_tag"]


# --------------------------------------------------------------------------
# gate0.py -- a broken capex must not produce a flattering FCF
# --------------------------------------------------------------------------


def _derive(rows):
    frame = pl.DataFrame(rows).with_columns(pl.col("cik").cast(pl.Int64))
    return gate0.compute_metrics(frame)


BASE = {
    "cik": 1,
    "fiscal_year": 2025,
    "revenue": 15_150_000_000.0,
    "ocf": 567_000_000.0,
    "sbc": 23_100_000.0,
    "net_income": 119_000_000.0,
    "operating_income": 368_000_000.0,
    "equity": 1_000_000_000.0,
    "goodwill": 0.0,
    "intangibles": 0.0,
    "cash": 100_000_000.0,
    "total_debt": 0.0,
    "shares_diluted": 34_700_000.0,
    "tax_expense": 30_000_000.0,
    "pretax_income": 150_000_000.0,
    "acquisitions": 0.0,
    "buybacks": 0.0,
    "dep_amort": 250_000_000.0,
}


def test_negative_capex_nulls_fcf_instead_of_inflating_it():
    """SAH resolved to capex of -$149.9M, which ADDED $150M to its FCF."""
    out = _derive([{**BASE, "capex": -149_900_000.0}])
    assert out["capex_broken"][0] is True
    assert out["fcf"][0] is None, "an unknown FCF, never a flattered one"
    assert out["fcf_after_sbc"][0] is None


def test_zero_capex_against_real_revenue_is_broken_not_asset_light():
    out = _derive([{**BASE, "capex": 0.0}])
    assert out["capex_broken"][0] is True
    assert out["fcf"][0] is None


# NOG's real store row: an oil-and-gas E&P whose capex resolved to $0.76M
# against $2.48B of revenue and $1.505B of OCF, giving a 53% FCF yield and a
# 1.9x P/FCF. Kept as its own fixture because the ratio test only bites when
# OCF conversion is high, which is exactly the shape that makes the resulting
# FCF look spectacular.
NOG = {
    **BASE,
    "revenue": 2_480_000_000.0,
    "ocf": 1_505_000_000.0,
    "sbc": 15_400_000.0,
    "net_income": 39_000_000.0,
    "operating_income": 246_000_000.0,
    "shares_diluted": 99_300_000.0,
}


def test_tiny_capex_warns_but_still_computes():
    """The NOG shape. This file cannot tell a mis-extraction from a genuinely
    asset-light filer, so it flags for a human and does not reject."""
    out = _derive([{**NOG, "capex": 759_000.0}])
    assert out["capex_broken"][0] is False
    assert out["capex_suspect"][0] is True
    assert out["fcf"][0] == pytest.approx(1_504_241_000.0)


def test_thin_margin_filer_is_caught_by_the_da_test():
    """This USED to be a documented hole. The ratio test needs OCF above 5%
    of revenue to fire, so a low-margin distributor or auto dealer with an
    understated capex escaped it, and widening the revenue ratio would have
    flagged most of retail. The D&A test closes it without that cost --
    which is the argument for the D&A test in one case: it does not care
    what the filer's margins look like."""
    out = _derive([{**BASE, "capex": 759_000.0}])  # SAH's thin-margin shape
    assert out["capex_broken"][0] is False
    assert out["capex_suspect"][0] is True


# SKYW's real store row after the first patch landed: capex still $32.0M
# because aircraft purchases are tagged outside the chain, capex/revenue 0.79%
# -- ABOVE the 0.5% ratio threshold, so nothing fired and it went straight to
# the top of the shortlist at a 4.6x P/FCF. This is why the D&A test exists.
SKYW = {
    **BASE,
    "revenue": 4_060_000_000.0,
    "ocf": 940_000_000.0,
    "sbc": 18_700_000.0,
    "net_income": 428_000_000.0,
    "operating_income": 618_000_000.0,
    "shares_diluted": 41_400_000.0,
    "dep_amort": 364_500_000.0,
}


def test_capex_far_below_depreciation_is_suspect_even_at_a_passing_revenue_ratio():
    """The SKYW miss, pinned. capex/revenue alone is industry-dependent and
    let this through; capex/D&A is not."""
    out = _derive([{**SKYW, "capex": 32_000_000.0}])
    assert out["capex_suspect"][0] is True
    assert out["capex_vs_dep_amort"][0] == pytest.approx(0.0878, abs=1e-3)


def test_capex_near_depreciation_is_a_going_concern():
    """A business replacing its assets at roughly the rate it depreciates
    them is ordinary in every industry, and must not be flagged."""
    out = _derive([{**SKYW, "capex": 400_000_000.0}])
    assert out["capex_suspect"][0] is False


def test_da_test_is_skipped_when_da_is_unknown():
    """NOG and ANF report no D&A tag at all. An absent denominator must not
    manufacture a flag -- unknown is not suspect."""
    out = _derive([{**SKYW, "capex": 32_000_000.0, "dep_amort": None}])
    assert out["capex_vs_dep_amort"][0] is None


def test_ordinary_capex_is_neither_broken_nor_suspect():
    out = _derive([{**BASE, "capex": 274_000_000.0}])
    assert out["capex_broken"][0] is False
    assert out["capex_suspect"][0] is False
    assert out["fcf"][0] == pytest.approx(293_000_000.0)


# --------------------------------------------------------------------------
# FY vs TTM divergence (added 2026-08-19)
#
# Every screen.py leg runs on the FY figures. gate0 computes the TTM ones and
# nothing tested them, so a company whose cash generation inverted over the
# last four quarters still shortlisted as clean -- 5 of 19 main-lane
# survivors, 26%. A scheduled run caught NOG by hand; these flags do it for
# the whole universe.
# --------------------------------------------------------------------------


TTM_BASE = {
    "fcf_after_sbc": 237_500_000.0,
    "ocf": 1_505_000_000.0,
    "ttm_ocf": 1_415_000_000.0,
    "ttm_fcf_after_sbc": -207_100_000.0,
}


def _diverge(row):
    return gate0.add_ttm_divergence(pl.DataFrame([row]))


def test_nog_shape_is_a_real_divergence_not_a_broken_series():
    """FY positive, TTM negative, but TTM operating cash flow is plausible
    against FY -- a genuine capex ramp. Flag the divergence, not the data."""
    out = _diverge(TTM_BASE)
    assert out["ttm_fcf_divergence"][0] is True
    assert out["ttm_suspect"][0] is False


def test_kfy_shape_is_a_broken_ttm_series_not_a_finding():
    """Korn Ferry came out at -$971M of TTM operating cash against +$414M FY
    on $2.94B of revenue. No such business burns that; the four-quarter sum
    is wrong, and saying so is different from saying the company is."""
    out = _diverge({**TTM_BASE, "ocf": 414_000_000.0, "ttm_ocf": -971_000_000.0,
                    "fcf_after_sbc": 276_600_000.0, "ttm_fcf_after_sbc": -1_083_300_000.0})
    assert out["ttm_fcf_divergence"][0] is True
    assert out["ttm_suspect"][0] is True


def test_agreeing_fy_and_ttm_raise_nothing():
    out = _diverge({**TTM_BASE, "ttm_fcf_after_sbc": 208_100_000.0})
    assert out["ttm_fcf_divergence"][0] is False
    assert out["ttm_suspect"][0] is False


def test_absent_ttm_is_not_a_divergence():
    """A company with no buildable four-quarter series has an UNKNOWN TTM.
    Unknown is not negative, and must not be flagged as a disagreement."""
    out = _diverge({**TTM_BASE, "ttm_ocf": None, "ttm_fcf_after_sbc": None})
    assert out["ttm_fcf_divergence"][0] is False
    assert out["ttm_suspect"][0] is False


def test_negative_fy_is_not_a_divergence():
    """The flag is about a FY pass contradicted by the TTM. A name already
    failing on FY is caught by fail_fcf and needs no second signal."""
    out = _diverge({**TTM_BASE, "fcf_after_sbc": -50_000_000.0})
    assert out["ttm_fcf_divergence"][0] is False
