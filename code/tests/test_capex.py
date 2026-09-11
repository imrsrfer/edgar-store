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

import datetime as dt
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
# 2026-09-09 -- PaymentsToAcquireIntangibleAssets, and the never_alone guard
# that had to exist before it could safely be added.
# --------------------------------------------------------------------------


def test_intangible_purchases_are_added_to_an_itemised_ppe_leg():
    """The OMCL shape, and the 2,032-filer case: an intangibles line beside a
    real PP&E leg. Both are disjoint investing lines, so both are capex."""
    import build_facts

    row = build_facts._resolve_one(
        CAPEX, 2025, "FY",
        _index([
            ("PaymentsToAcquirePropertyPlantAndEquipment", 10_000_000.0),
            ("PaymentsToAcquireIntangibleAssets", 4_000_000.0),
        ]),
    )
    assert row["value"] == pytest.approx(14_000_000.0)
    assert "PaymentsToAcquireIntangibleAssets" in row["source_tag"]


def test_intangibles_alone_never_replace_a_broad_productive_assets_total():
    """🔴 THE BLOCKER, pinned. Measured on the 2026-08-25 archive, 85 filers
    report an intangibles line and a BROAD total with no itemised PP&E leg.
    _resolve_components returns on the first component it finds and resolve()
    then skips the chain entirely, so without never_alone VERIZON's capex would
    read 450,000,000 instead of 16,658,000,000 -- a 97% cut, in the direction
    that OVERSTATES FCF. The broad total must win here."""
    import build_facts

    row = build_facts._resolve_one(
        CAPEX, 2025, "FY",
        _index([
            ("PaymentsToAcquireIntangibleAssets", 450_000_000.0),
            ("PaymentsToAcquireProductiveAssets", 16_658_000_000.0),
        ]),
    )
    assert row["value"] == pytest.approx(16_658_000_000.0)
    assert row["source_tag"] == "PaymentsToAcquireProductiveAssets"


def test_other_ppe_alone_never_replaces_a_broad_total_either():
    """The 2026-08-19 comment said this tag "must never be reachable as a lone
    winner". Nothing enforced it until never_alone existed, so the same
    substitution was reachable for it all along."""
    import build_facts

    row = build_facts._resolve_one(
        CAPEX, 2025, "FY",
        _index([
            ("PaymentsToAcquireOtherPropertyPlantAndEquipment", 759_000.0),
            ("PaymentsToAcquireProductiveAssets", 393_400_000.0),
        ]),
    )
    assert row["value"] == pytest.approx(393_400_000.0)


def test_other_ppe_alone_is_still_a_valid_sole_disclosure():
    """🔴 never_alone must not become never_sole. OtherPP&E IS a PP&E line, so a
    filer reporting only that is making a real capex disclosure -- LLY discloses
    7,841,000,000 that way, ALK 309,000,000, ADP 196,600,000. An earlier cut
    discarded these and cost 51 rows a capex they genuinely had, moving 14 from
    pass to unevaluable. It loses to a broad total; it does not vanish."""
    import build_facts

    row = build_facts._resolve_one(
        CAPEX, 2025, "FY",
        _index([("PaymentsToAcquireOtherPropertyPlantAndEquipment", 7_841_000_000.0)]),
    )
    assert row is not None
    assert row["value"] == pytest.approx(7_841_000_000.0)
    assert "_never_alone_only" not in row and "_never_sole_only" not in row


def test_intangibles_alone_resolve_to_nothing_rather_than_a_flattering_capex():
    """🔴 The first cut used a never_alone-only sum as a last resort, reasoning
    that intangibles are better than nothing. The 2026-09-09 regression run
    refuted it on five rows: VZ, STM, INCY, SUNB and APP all had a NULL capex
    and gate0_status="unknown" (latest filing a 10-Q/20-F/40-F, so the annual
    legs never computed). The fallback manufactured an ANNUAL capex from the one
    line captured for a partial period and flipped "unknown" into "pass" -- VZ
    at 450,000,000 against a real capex near 17,000,000,000, STM's FCF at
    2,059,000,000 on a 93,000,000 capex.

    "Capex could not be determined" and "capex is small" are different findings
    and only the second flatters the company."""
    import build_facts

    row = build_facts._resolve_one(
        CAPEX, 2025, "FY",
        _index([("PaymentsToAcquireIntangibleAssets", 2_000_000.0)]),
    )
    assert row is None


def test_never_alone_marker_never_escapes_into_a_stored_row():
    """The marker is plumbing. A stray key would land in facts.parquet."""
    import build_facts

    for index in (
        _index([("PaymentsToAcquirePropertyPlantAndEquipment", 100.0)]),
        _index([("PaymentsToAcquireIntangibleAssets", 100.0)]),
        _index([("PaymentsToAcquireIntangibleAssets", 1.0),
                ("PaymentsToAcquireProductiveAssets", 2.0)]),
    ):
        row = build_facts._resolve_one(CAPEX, 2025, "FY", index)
        assert row is None or ("_never_alone_only" not in row and "_never_sole_only" not in row)


def test_dead_sibling_tags_are_not_declared():
    """PaymentsToAcquireFiniteLivedIntangibleAssets and
    PaymentsToAcquireIntangibleAssetsExcludingGoodwill appear on ZERO filers
    anywhere in the 2026-08-25 archive. A declared tag that never occurs is a
    claim of coverage nothing tests, and the next reader cannot distinguish it
    from a live one."""
    for dead in ("PaymentsToAcquireFiniteLivedIntangibleAssets",
                 "PaymentsToAcquireIntangibleAssetsExcludingGoodwill"):
        assert dead not in CAPEX.all_tags


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


# --------------------------------------------------------------------------
# A NEGATIVE TTM capex must SAY it nulled the TTM FCF (added 2026-09-11)
#
# build_ttm already refused to compute through a negative ttm_capex -- the
# SAH shape, where -$149.9M of capex ADDS $150M to free cash flow -- and
# nulled ttm_fcf_after_sbc instead. But it nulled it SILENTLY. The resulting
# row published a raw ttm_capex, an empty ttm_stale_concepts, and
# ttm_unavailable FALSE (because ttm_ocf was built fine), so nothing on it
# distinguished "the window was built and one input is unusable" from "the
# window was built and everything is fine but the FCF happens to be absent".
# That is the same defect class as the null market cap at the band gate: a
# null that reads as clean.
#
# 64 rows in the 2026-09-10 store are this shape.
#
# 🔴 The flag does NOT correct the number, and build_ttm must NOT compute
# through the negative. Until the capex chain's sign convention is audited a
# negative TTM capex is as likely to be an extraction bug as a real disposal,
# and computing through it would flatter FCF by the full amount every time
# it is the former.
# --------------------------------------------------------------------------


def _ttm_q(cik, concept, start, end, value):
    return {
        "cik": cik, "concept": concept, "fiscal_period": "Q1",
        "period_start": dt.date.fromisoformat(start),
        "period_end": dt.date.fromisoformat(end), "value": float(value),
    }


def _ttm_facts(rows):
    return pl.DataFrame(rows, schema={
        "cik": pl.Int64, "concept": pl.Utf8, "fiscal_period": pl.Utf8,
        "period_start": pl.Date, "period_end": pl.Date, "value": pl.Float64,
    })


QUARTERS = (
    ("2025-01-01", "2025-03-31"),
    ("2025-04-01", "2025-06-30"),
    ("2025-07-01", "2025-09-30"),
    ("2025-10-01", "2025-12-31"),
)


def _ttm_year(cik, concept, quarterly_values):
    return [
        _ttm_q(cik, concept, start, end, value)
        for (start, end), value in zip(QUARTERS, quarterly_values)
    ]


def _ttm_rows(capex_quarters):
    """One filer, a full four-quarter OCF/capex/SBC chain."""
    return (
        _ttm_year(7, "ocf", [100_000_000.0] * 4)
        + _ttm_year(7, "capex", capex_quarters)
        + _ttm_year(7, "sbc", [5_000_000.0] * 4)
    )


def test_negative_ttm_capex_is_named_on_the_row_not_just_nulled():
    """The SAH shape on the TTM path. ttm_fcf_after_sbc stays NULL -- and the
    row states WHY, so it cannot be read as a clean one."""
    out = gate0.build_ttm(_ttm_facts(_ttm_rows(
        [-50_000_000.0, -40_000_000.0, -35_000_000.0, -24_900_000.0]
    )))
    assert out["ttm_capex"][0] == pytest.approx(-149_900_000.0)
    assert out["ttm_fcf_after_sbc"][0] is None, (
        "computing through a negative capex flatters TTM FCF by twice the capex"
    )
    assert out["ttm_capex_negative"][0] is True, (
        "a silently nulled ttm_fcf_after_sbc reads identically to a clean row"
    )


def test_ordinary_ttm_capex_is_not_flagged_and_still_computes():
    out = gate0.build_ttm(_ttm_facts(_ttm_rows([20_000_000.0] * 4)))
    assert out["ttm_capex_negative"][0] is False
    assert out["ttm_fcf_after_sbc"][0] == pytest.approx(300_000_000.0)


def test_zero_ttm_capex_is_not_the_negative_flag():
    """This flag states one thing: the sign. A zero capex is a different
    finding (capex_suspect's territory) and must not borrow this name."""
    out = gate0.build_ttm(_ttm_facts(_ttm_rows([0.0] * 4)))
    assert out["ttm_capex_negative"][0] is False


def test_unbuildable_ttm_capex_is_not_flagged_negative():
    """A filer with no capex series at all has an UNKNOWN TTM capex. Unknown
    is not negative -- the same distinction ttm_unavailable already carries."""
    rows = _ttm_year(8, "ocf", [100_000_000.0] * 4) + _ttm_year(8, "sbc", [5_000_000.0] * 4)
    out = gate0.build_ttm(_ttm_facts(rows))
    assert out["ttm_capex"][0] is None
    assert out["ttm_capex_negative"][0] is False


# --------------------------------------------------------------------------
# The other two silent-NULL populations (added 2026-09-11)
#
# ttm_fcf_after_sbc = ttm_ocf - ttm_capex - ttm_sbc, and a null in ANY input
# blanks the cell. ttm_capex_negative closed one cause. Two larger ones
# remained, and on both of them ttm_unavailable was false, ttm_suspect was
# false and ttm_stale_concepts named nothing -- the row read as measured and
# clean while the Growth screen's master metric was simply absent:
#
#   ttm_capex_missing    -- a TTM window exists and capex is not in it.
#   ttm_sbc_window_lost  -- a TTM window exists, capex is fine, the SBC term
#                           is absent, and the FISCAL YEAR shows real SBC. The
#                           company demonstrably pays it and the window lost
#                           it; assuming zero would overstate FCF-after-SBC by
#                           the whole SBC line. Same trap as the capex sign
#                           convention, relocated.
#   ttm_sbc_assumed_zero -- the same gap where the fiscal year ALSO shows no
#                           SBC (null or zero). Nothing is being hidden, so
#                           the metric is computed with an SBC term of zero
#                           and the assumption travels on the row -- the
#                           posture sbc_unverified already established on the
#                           annual path: state the assumption, never let an
#                           absent input silently produce a verdict.
#
# 🔴 Neither missing capex nor a corroborated SBC line is ever zero-filled.
# capex fails OPEN in the company's favour -- that is what the whole
# capex_broken / capex_suspect apparatus exists for -- so a missing capex
# keeps its NULL and gains a flag, exactly like a negative one.
# --------------------------------------------------------------------------


def test_null_ttm_capex_is_named_missing_not_silently_dropped():
    """No capex series in the window. The metric stays NULL -- zero-filling it
    would manufacture free cash flow out of an absent input -- and the row
    states which input went missing."""
    rows = _ttm_year(9, "ocf", [100_000_000.0] * 4) + _ttm_year(9, "sbc", [5_000_000.0] * 4)
    out = gate0.build_ttm(_ttm_facts(rows))
    assert out["ttm_capex"][0] is None
    assert out["ttm_fcf_after_sbc"][0] is None
    assert out["ttm_capex_missing"][0] is True
    assert out["ttm_capex_negative"][0] is False


def test_missing_and_negative_ttm_capex_cannot_both_fire():
    """They are different findings with different fixes: one is an absent
    series, the other a sign convention. A row may not claim both."""
    out = gate0.build_ttm(_ttm_facts(_ttm_rows([-20_000_000.0] * 4)))
    assert out["ttm_capex_negative"][0] is True
    assert out["ttm_capex_missing"][0] is False


def test_present_ttm_capex_is_not_flagged_missing():
    out = gate0.build_ttm(_ttm_facts(_ttm_rows([20_000_000.0] * 4)))
    assert out["ttm_capex_missing"][0] is False


# The SBC resolution needs the FISCAL-YEAR sbc column, which only exists after
# the annual frame and the TTM frame are joined -- so it is tested on the
# joined shape, not on build_ttm's output.
SBC_GAP = {
    "ttm_ocf": 100_000_000.0,
    "ttm_capex": 20_000_000.0,
    "ttm_sbc": None,
    "sbc": None,
    "ttm_fcf_after_sbc": None,
}


# Explicit dtypes: on the real frame every one of these is Float64, and a
# single-row literal of None would otherwise arrive as polars' Null dtype --
# a test artifact, not a shape gate0 ever sees.
_SBC_SCHEMA = {
    "ttm_ocf": pl.Float64,
    "ttm_capex": pl.Float64,
    "ttm_sbc": pl.Float64,
    "sbc": pl.Float64,
    "ttm_fcf_after_sbc": pl.Float64,
}


def _resolve(row):
    schema = {**_SBC_SCHEMA}
    schema.update({k: pl.Boolean for k in row if k not in schema})
    return gate0.resolve_ttm_sbc(pl.DataFrame([row], schema=schema))


def test_sbc_gap_with_no_fy_sbc_is_assumed_zero_and_says_so():
    """The filer tags no SBC anywhere -- Exxon's shape on the annual path.
    Nothing is being hidden by assuming zero, so the metric is computed and
    the assumption is stated rather than left to be inferred from a blank."""
    out = _resolve(SBC_GAP)
    assert out["ttm_sbc_assumed_zero"][0] is True
    assert out["ttm_sbc_window_lost"][0] is False
    assert out["ttm_fcf_after_sbc"][0] == pytest.approx(80_000_000.0)


def test_sbc_gap_with_a_zero_fy_sbc_is_assumed_zero():
    """A reported zero corroborates the assumption at least as well as an
    absent tag does."""
    out = _resolve({**SBC_GAP, "sbc": 0.0})
    assert out["ttm_sbc_assumed_zero"][0] is True
    assert out["ttm_fcf_after_sbc"][0] == pytest.approx(80_000_000.0)


def test_sbc_gap_with_a_real_fy_sbc_is_withheld_not_assumed():
    """🔴 The load-bearing case. This company pays SBC -- the fiscal year says
    so -- and the TTM window lost the line. Assuming zero here would overstate
    FCF-after-SBC by the entire SBC figure, in the company's favour, which is
    the one direction a quality gate must never fail in."""
    out = _resolve({**SBC_GAP, "sbc": 12_000_000.0})
    assert out["ttm_sbc_window_lost"][0] is True
    assert out["ttm_sbc_assumed_zero"][0] is False
    assert out["ttm_fcf_after_sbc"][0] is None


def test_missing_capex_and_a_lost_sbc_window_travel_together():
    """Two independent inputs, two independent flags. A row that lost both
    must say both -- naming only the first would leave the second silent."""
    out = _resolve({**SBC_GAP, "ttm_capex": None, "sbc": 12_000_000.0,
                    "ttm_capex_missing": True})
    assert out["ttm_capex_missing"][0] is True
    assert out["ttm_sbc_window_lost"][0] is True
    assert out["ttm_fcf_after_sbc"][0] is None


def test_missing_capex_blocks_the_sbc_recovery():
    """An assumable SBC gap does not make an absent capex computable. The
    metric stays NULL and nothing is assumed on the capex side."""
    out = _resolve({**SBC_GAP, "ttm_capex": None})
    assert out["ttm_fcf_after_sbc"][0] is None
    assert out["ttm_sbc_assumed_zero"][0] is False


def test_negative_capex_dominates_an_assumable_sbc_gap():
    """The shipped guard still wins. A negative capex is unusable whatever the
    SBC column says, so the recovery must not compute through it."""
    out = _resolve({**SBC_GAP, "ttm_capex": -20_000_000.0})
    assert out["ttm_fcf_after_sbc"][0] is None
    assert out["ttm_sbc_assumed_zero"][0] is False


def test_resolution_never_touches_a_row_that_already_has_a_value():
    """This fix may only turn NULLs into numbers. A row whose TTM FCF was
    already computed must come out bit-identical."""
    out = _resolve({**SBC_GAP, "ttm_sbc": 5_000_000.0, "sbc": 5_000_000.0,
                    "ttm_fcf_after_sbc": 75_000_000.0})
    assert out["ttm_fcf_after_sbc"][0] == 75_000_000.0
    assert out["ttm_sbc_assumed_zero"][0] is False
    assert out["ttm_sbc_window_lost"][0] is False


def test_a_row_with_no_ttm_window_is_not_an_sbc_gap():
    """No window means NOT MEASURED, which ttm_unavailable already carries.
    Recovering a metric from a row that has no OCF would invent one."""
    out = _resolve({**SBC_GAP, "ttm_ocf": None})
    assert out["ttm_fcf_after_sbc"][0] is None
    assert out["ttm_sbc_assumed_zero"][0] is False
    assert out["ttm_sbc_window_lost"][0] is False


# --------------------------------------------------------------------------
# The annual SBC convention (added 2026-09-11)
#
# The TTM path computes with an SBC term of zero where nothing corroborates a
# missing SBC line; the annual path withholds fcf_after_sbc whenever sbc is
# null. Same filer, two columns, opposite conventions -- a reader comparing
# them sees a blank beside a number and can read it as a collapse.
#
# 🔴 PHASE A IS DIAGNOSTIC ONLY. fcf_after_sbc feeds fail_fcf_after_sbc,
# which is a GATE 0 LEG, so changing the annual computation changes verdicts,
# shortlists and the review queue. These flags say which case each null-sbc
# row is. They do not change one number. The convention flip lives behind
# --resolve-annual-sbc-zero, default OFF, and is measured, not shipped.
#
# The discriminator for "this filer does report SBC" is its OWN FILING
# HISTORY: any fiscal year with sbc > 0, or a positive ttm_sbc. That mirrors
# _goodwill_intangibles_history, which already resolves a missing latest
# value from whether the concept was EVER reported.
# --------------------------------------------------------------------------

ANNUAL_SBC = {
    "sbc": None,
    "ocf": 100_000_000.0,
    "capex": 20_000_000.0,
    "ttm_sbc": None,
    "sbc_ever_reported": False,
}


def _annual_sbc(row, apply_zero=False):
    """One filer, ONE fiscal year, resolved by compute_metrics.

    These used to drive a latest-row resolver. The rule moved into
    compute_metrics so it runs per year; the cases it has to get right did
    not change, so they are pinned here against the function that now owns
    them. A single-year frame is the degenerate case of the per-year rule.
    """
    evidence = None
    if row.get("sbc_ever_reported") or (row.get("ttm_sbc") or 0) > 0:
        evidence = pl.DataFrame(
            {"cik": [9], "sbc_ever_reported": [True]},
            schema={"cik": pl.Int64, "sbc_ever_reported": pl.Boolean},
        )
    frame = pl.DataFrame([{
        **BASE, "cik": 9, "fiscal_year": 2025,
        "sbc": row["sbc"], "ocf": row["ocf"], "capex": row["capex"],
    }]).with_columns(pl.col("cik").cast(pl.Int64))
    return gate0.compute_metrics(
        frame, sbc_evidence=evidence, resolve_sbc_zero=apply_zero
    )


def test_reported_sbc_carries_neither_annual_flag():
    out = _annual_sbc({**ANNUAL_SBC, "sbc": 5_000_000.0})
    assert out["sbc_assumed_zero"][0] is False
    assert out["sbc_window_lost"][0] is False
    assert out["fcf_after_sbc"][0] == 75_000_000.0


def test_null_sbc_with_no_filing_history_of_sbc_is_assumable():
    """Exxon's shape: the filer has never tagged share-based compensation in
    any year. Nothing is hidden by an SBC term of zero -- but PHASE A STILL
    WITHHOLDS. The flag states the case; it does not change the number."""
    out = _annual_sbc(ANNUAL_SBC)
    assert out["sbc_assumed_zero"][0] is True
    assert out["sbc_window_lost"][0] is False
    assert out["fcf_after_sbc"][0] is None


def test_null_sbc_on_a_filer_that_has_reported_sbc_before_is_a_lost_window():
    """🔴 The load-bearing case. This company pays SBC -- an earlier fiscal
    year says so -- and the latest year lost the line. It must never be
    assumed away, in phase A or under the phase-B flag."""
    out = _annual_sbc({**ANNUAL_SBC, "sbc_ever_reported": True})
    assert out["sbc_window_lost"][0] is True
    assert out["sbc_assumed_zero"][0] is False
    assert out["fcf_after_sbc"][0] is None


def test_a_positive_ttm_sbc_is_also_evidence():
    """A filer whose annual tag is missing but whose four-quarter sum carries
    real SBC is reporting it. Evidence is evidence whichever column holds it."""
    out = _annual_sbc({**ANNUAL_SBC, "ttm_sbc": 3_000_000.0})
    assert out["sbc_window_lost"][0] is True
    assert out["sbc_assumed_zero"][0] is False


def test_a_filer_with_no_prior_years_at_all_is_assumable_not_lost():
    """A first-year filer has no history to contradict the absence. Unknown
    history is not evidence of SBC, and a null sbc_ever_reported must not
    silently become a lost window."""
    out = _annual_sbc({**ANNUAL_SBC, "sbc_ever_reported": None})
    assert out["sbc_assumed_zero"][0] is True
    assert out["sbc_window_lost"][0] is False


def test_a_reported_zero_in_a_prior_year_is_not_evidence_of_sbc():
    """sbc_ever_reported tests sbc > 0, not sbc is not null. A filer that
    reported zero has corroborated the absence, not contradicted it."""
    out = _annual_sbc({**ANNUAL_SBC, "ttm_sbc": 0.0, "sbc_ever_reported": False})
    assert out["sbc_assumed_zero"][0] is True
    assert out["sbc_window_lost"][0] is False


def test_phase_b_flag_computes_only_the_assumable_rows():
    """--resolve-annual-sbc-zero. OFF by default; this asserts what it would
    do if switched on, which is the whole point of measuring it first."""
    out = _annual_sbc(ANNUAL_SBC, apply_zero=True)
    assert out["fcf_after_sbc"][0] == pytest.approx(80_000_000.0)
    assert out["sbc_assumed_zero"][0] is True


def test_phase_b_flag_never_computes_a_lost_window():
    out = _annual_sbc({**ANNUAL_SBC, "sbc_ever_reported": True}, apply_zero=True)
    assert out["fcf_after_sbc"][0] is None


def test_phase_b_flag_never_computes_through_an_absent_capex():
    """The capex rules do not relax because the SBC term became assumable."""
    out = _annual_sbc({**ANNUAL_SBC, "capex": None}, apply_zero=True)
    assert out["fcf_after_sbc"][0] is None


def test_phase_b_flag_never_moves_a_row_that_already_has_a_value():
    out = _annual_sbc({**ANNUAL_SBC, "sbc": 5_000_000.0}, apply_zero=True)
    assert out["fcf_after_sbc"][0] == 75_000_000.0


def test_ttm_assumed_zero_contradicted_by_a_stale_sbc_concept_is_stated():
    """The least-corroborated corner of the TTM recovery: the builder SAW an
    sbc concept and withdrew the window for staleness, which is positive
    evidence an SBC line exists, against a null FY value. Assuming zero there
    errs high. Flagged, not silently changed."""
    frame = pl.DataFrame(
        [
            {"ttm_sbc_assumed_zero": True, "ttm_stale_concepts": "sbc,acquisitions"},
            {"ttm_sbc_assumed_zero": True, "ttm_stale_concepts": "acquisitions"},
            {"ttm_sbc_assumed_zero": False, "ttm_stale_concepts": "sbc"},
        ],
        schema={"ttm_sbc_assumed_zero": pl.Boolean, "ttm_stale_concepts": pl.Utf8},
    )
    out = gate0.add_ttm_sbc_evidence_conflict(frame)
    assert out["ttm_sbc_evidence_conflict"].to_list() == [True, False, False]


# --------------------------------------------------------------------------
# The SBC-zero resolution runs PER FISCAL YEAR (added 2026-09-11)
#
# It used to run on the latest row only. fcf_per_share_cagr_3y/5y,
# fcf_per_share_earliest/latest/delta_abs, fcf_inflection and
# fcf_inflection_years are assembled per year in compute_metrics and
# build_trends, so a latest-row resolution put TWO CONVENTIONS INSIDE ONE
# ROW: a level computed with an SBC term of zero beside a CAGR assembled from
# years that withheld. That is worse than the annual-vs-TTM asymmetry it was
# meant to fix, because both halves sit in the same path and a reader
# comparing a level against its own growth rate cannot see it.
#
# FCF per share after SBC is the growth screen's master metric, so the trend
# columns are the ones that matter.
#
# 🔴 sbc_ever_reported stays FILER-level, never year-level. A filer that
# reports SBC in ANY year has an SBC line, so a null in another year is a
# lost tag, not an absence. That is what makes the push-down non-trivial: one
# filer can hold assumed-zero years and reported years, but NEVER assumed-zero
# and window-lost years.
# --------------------------------------------------------------------------


def _years(cik, sbc_by_year, **overrides):
    """One filer, one row per fiscal year, sbc taken from the mapping."""
    return [
        {**BASE, **overrides, "cik": cik, "fiscal_year": year, "capex": 274_000_000.0,
         "sbc": sbc}
        for year, sbc in sorted(sbc_by_year.items())
    ]


def _resolve_years(rows, resolve=False):
    frame = pl.DataFrame(rows).with_columns(pl.col("cik").cast(pl.Int64))
    return gate0.compute_metrics(frame, resolve_sbc_zero=resolve).sort(
        ["cik", "fiscal_year"]
    )


def test_a_filer_with_no_sbc_in_any_year_has_every_year_assumable():
    """No evidence anywhere in the history, so EVERY year is assumable -- not
    just the latest one. This is the push-down in one assertion."""
    out = _resolve_years(_years(1, {2021: None, 2022: None, 2023: None,
                                    2024: None, 2025: None}))
    assert out["sbc_assumed_zero"].to_list() == [True] * 5
    assert out["sbc_window_lost"].to_list() == [False] * 5
    assert out["sbc_ever_reported"].to_list() == [False] * 5


def test_every_year_of_an_assumable_filer_computes_under_the_flag():
    """🔴 The whole point. With the flag on, no year of this filer's history
    is withheld, so the CAGR and the level rest on one convention."""
    out = _resolve_years(
        _years(1, {2021: None, 2022: None, 2023: None, 2024: None, 2025: None}),
        resolve=True,
    )
    assert out["fcf_after_sbc"].null_count() == 0
    assert out["fcf_after_sbc"].to_list() == [pytest.approx(293_000_000.0)] * 5
    assert out["fcf_per_share"].null_count() == 0


def test_one_reported_year_makes_every_null_year_a_lost_window():
    """Evidence is FILER-level. A single year with sbc > 0 proves the line
    exists, so the other years lost a tag -- they did not lack SBC."""
    out = _resolve_years(_years(2, {2021: None, 2022: 5_000_000.0, 2023: None,
                                    2024: None, 2025: None}))
    assert out["sbc_ever_reported"].to_list() == [True] * 5
    assert out["sbc_window_lost"].to_list() == [True, False, True, True, True]
    assert out["sbc_assumed_zero"].to_list() == [False] * 5


def test_a_lost_window_year_stays_withheld_even_under_the_flag():
    out = _resolve_years(
        _years(2, {2021: None, 2022: 5_000_000.0, 2023: None, 2024: None, 2025: None}),
        resolve=True,
    )
    assert out["fcf_after_sbc"].to_list()[0] is None
    assert out["fcf_after_sbc"].to_list()[1] == pytest.approx(288_000_000.0)
    assert out["fcf_after_sbc"].null_count() == 4


def test_a_filer_with_no_null_years_carries_neither_flag():
    out = _resolve_years(_years(3, {2023: 1_000_000.0, 2024: 2_000_000.0,
                                    2025: 3_000_000.0}))
    assert out["sbc_assumed_zero"].to_list() == [False] * 3
    assert out["sbc_window_lost"].to_list() == [False] * 3


def test_ttm_sbc_alone_is_enough_evidence_to_withhold_every_year():
    """A filer whose annual tag is missing in every year but whose
    four-quarter sum carries real SBC is reporting it. The evidence arrives
    from outside compute_metrics, so it is passed in rather than derived."""
    evidence = pl.DataFrame(
        {"cik": [4], "sbc_ever_reported": [True]},
        schema={"cik": pl.Int64, "sbc_ever_reported": pl.Boolean},
    )
    frame = pl.DataFrame(
        _years(4, {2023: None, 2024: None, 2025: None})
    ).with_columns(pl.col("cik").cast(pl.Int64))
    out = gate0.compute_metrics(frame, sbc_evidence=evidence, resolve_sbc_zero=True)
    assert out["sbc_window_lost"].to_list() == [True] * 3
    assert out["fcf_after_sbc"].null_count() == 3


def test_a_single_year_filer_with_no_evidence_is_assumable():
    """No history to contradict the absence. One year is still a history."""
    out = _resolve_years(_years(5, {2025: None}))
    assert out["sbc_assumed_zero"].to_list() == [True]
    assert out["sbc_window_lost"].to_list() == [False]


def test_no_filer_can_hold_both_an_assumed_and_a_lost_year():
    """Two filers in one frame, opposite evidence. The grouping must be per
    cik -- a frame-wide 'any sbc reported' test would make every year of the
    first filer a lost window too."""
    rows = _years(6, {2024: None, 2025: None}) + _years(7, {2024: 7_000_000.0,
                                                            2025: None})
    out = _resolve_years(rows)
    first = out.filter(pl.col("cik") == 6)
    second = out.filter(pl.col("cik") == 7)
    assert first["sbc_assumed_zero"].to_list() == [True, True]
    assert first["sbc_window_lost"].any() is False
    assert second["sbc_window_lost"].to_list() == [False, True]
    assert second["sbc_assumed_zero"].any() is False


def test_the_flag_off_withholds_every_null_year():
    """Phase A behaviour, per year. The flags classify; they do not compute."""
    out = _resolve_years(_years(8, {2024: None, 2025: None}))
    assert out["fcf_after_sbc"].null_count() == 2
