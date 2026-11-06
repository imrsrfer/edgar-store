"""Narrow capex tag hold and capex_unconfirmed reader flag (2026-11-05).

Every figure is from the Capital IQ cross-check of the 2026-10-30 store:
screener-note-2026-11-05-capiq-ttm-crosscheck.md.
"""

import polars as pl

import gate0

M = 1e6


def _row(**updates):
    row = dict(cik=1, ticker="X", source_tag_capex="PaymentsToAcquirePropertyPlantAndEquipment",
               capex_vs_dep_amort=0.5, fail_fcf=False, fail_fcf_after_sbc=False,
               capex_suspect=False, ttm_capex_suspect=False)
    row.update(updates)
    return pl.DataFrame([row])


def _hold(**updates):
    return gate0.add_capex_tag_hold(_row(**updates)).row(0, named=True)


def test_alk_other_ppe_below_one_is_held():
    """ALK: capex 289 vs CIQ 1,370; FCF-SBC +689 vs -392. capex/D&A 0.39."""
    row = _hold(ticker="ALK", source_tag_capex="PaymentsToAcquireOtherPropertyPlantAndEquipment",
                capex_vs_dep_amort=0.388679)
    assert row["capex_tag_hold"] is True
    assert row["fail_fcf"] is None
    assert row["fail_fcf_after_sbc"] is None


def test_hni_software_only_is_held():
    """HNI: PaymentsToAcquireSoftware alone, capex 3.4 vs CIQ 97.9."""
    row = _hold(ticker="HNI", source_tag_capex="PaymentsToAcquireSoftware",
                capex_vs_dep_amort=0.018674)
    assert row["capex_tag_hold"] is True
    assert row["fail_fcf"] is None


def test_hos_productive_assets_at_009_is_held():
    """HOS: capex 17.8 vs CIQ 105.1."""
    row = _hold(ticker="HOS", source_tag_capex="PaymentsToAcquireProductiveAssets",
                capex_vs_dep_amort=0.087212)
    assert row["capex_tag_hold"] is True


def test_cts_productive_assets_at_046_is_not_held():
    """CTS: ProductiveAssets is its genuine total; capex 15.2 matched CIQ exactly."""
    row = _hold(ticker="CTS", source_tag_capex="PaymentsToAcquireProductiveAssets",
                capex_vs_dep_amort=0.455)
    assert row["capex_tag_hold"] is False


def test_grdn_productive_assets_at_060_is_a_known_miss():
    """GRDN is understated (4.5 vs 19.1) but sits among verified-correct rows."""
    row = _hold(ticker="GRDN", source_tag_capex="PaymentsToAcquireProductiveAssets",
                capex_vs_dep_amort=0.600672)
    assert row["capex_tag_hold"] is False


def test_lly_other_ppe_above_one_is_not_held():
    """LLY files its genuine total under the same tag; capex/D&A 3.93."""
    row = _hold(ticker="LLY", source_tag_capex="PaymentsToAcquireOtherPropertyPlantAndEquipment",
                capex_vs_dep_amort=3.92639)
    assert row["capex_tag_hold"] is False
    assert row["fail_fcf"] is False
    assert row["fail_fcf_after_sbc"] is False


def test_null_ratio_on_narrow_tag_holds():
    row = _hold(source_tag_capex="PaymentsToAcquireSoftware", capex_vs_dep_amort=None)
    assert row["capex_tag_hold"] is True


def test_composite_tag_is_not_held():
    """A summed tag is not a lone narrow tag (NRDS: PP&E + software)."""
    row = _hold(source_tag_capex="PaymentsToAcquirePropertyPlantAndEquipment+PaymentsToDevelopSoftware",
                capex_vs_dep_amort=0.1)
    assert row["capex_tag_hold"] is False


def test_genuine_fail_is_not_upgraded_on_unheld_row():
    row = _hold(fail_fcf=True, fail_fcf_after_sbc=True)
    assert row["fail_fcf"] is True
    assert row["fail_fcf_after_sbc"] is True


def test_held_row_cannot_reach_gate0_pass():
    """End to end through add_verdict: a held row is not a pass."""
    frame = pl.DataFrame([dict(
        cik=1, ticker="ALK", source_tag_capex="PaymentsToAcquireOtherPropertyPlantAndEquipment",
        capex_vs_dep_amort=0.39, fail_tangible_book=False, fail_income_quality=False,
        fail_fcf=False, fail_fcf_after_sbc=False, fail_sbc=False, fail_ni_over_oi=False,
        fail_tax_anomaly=False, imputed_fields="", carried_forward_fields="",
    )])
    out = gate0.add_verdict(gate0.add_capex_tag_hold(frame)).row(0, named=True)
    assert out["test_fcf"] == "NOT_EVALUABLE"
    assert out["test_fcf_after_sbc"] == "NOT_EVALUABLE"
    assert out["gate0_pass"] is not True


def test_capex_unconfirmed_marks_suspect_but_not_held():
    frame = gate0.add_capex_tag_hold(_row(ttm_capex_suspect=True))
    assert gate0.add_capex_unconfirmed(frame).row(0, named=True)["capex_unconfirmed"] is True


def test_capex_unconfirmed_false_when_held():
    frame = gate0.add_capex_tag_hold(_row(
        source_tag_capex="PaymentsToAcquireSoftware", capex_vs_dep_amort=0.02, capex_suspect=True))
    assert gate0.add_capex_unconfirmed(frame).row(0, named=True)["capex_unconfirmed"] is False


def test_capex_unconfirmed_does_not_touch_fail_flags():
    frame = gate0.add_capex_tag_hold(_row(ttm_capex_suspect=True))
    row = gate0.add_capex_unconfirmed(frame).row(0, named=True)
    assert row["fail_fcf"] is False
    assert row["fail_fcf_after_sbc"] is False


def test_financial_sic_is_not_held():
    """BAM, SIC 6282: OtherPP&E alone, capex 9M vs D&A 40M -- asset-light, not a sub-line."""
    row = _hold(ticker="BAM", sic="6282",
                source_tag_capex="PaymentsToAcquireOtherPropertyPlantAndEquipment",
                capex_vs_dep_amort=0.225)
    assert row["capex_tag_hold"] is False
