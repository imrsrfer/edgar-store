"""Rotation lanes B/C, Fix 4, and the income_quality ceiling (2026-08-26).

Each lane here was proposed with at least one input that does not work, and
every test below pins the corrected behaviour rather than the proposal.
"""

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gate0  # noqa: E402
import screen  # noqa: E402


class _Args:
    def __init__(self, lane, **kw):
        self.lane = lane
        self.include_financials = False
        self.exclude_sic = gate0.DEFAULT_EXCLUDE_SIC
        self.min_revenue = 50e6
        for k, v in kw.items():
            setattr(self, k, v)


# --------------------------------------------------------------------------
# Lane accel -- the FCF legs are dead columns and must not come back
# --------------------------------------------------------------------------


def test_accel_does_not_read_the_dead_fcf_columns():
    """🔴 fcf_accel_4q is populated on 51 of 6,031 store rows (0.8%), and
    quarters_of_accelerating_fcf is 90% populated but 99.5% zeros -- 28 rows
    reach 1. Screening on either rejects ~99% of the universe and reports it
    as 'nothing passed'. Pinned so nobody restores the original sketch."""
    import inspect

    source = inspect.getsource(screen.apply_growth)
    accel = source.split('if lane == "accel"')[1].split('if lane ==')[0]
    # Strip comments -- the branch NAMES both dead columns in prose, precisely
    # to explain why it does not read them. Test the code, not the commentary.
    code = "\n".join(
        line.split("#")[0] for line in accel.splitlines() if not line.strip().startswith("#")
    )
    for dead in ("fcf_accel_4q", "quarters_of_accelerating_fcf"):
        assert f'pl.col("{dead}")' not in code, f"accel lane must not filter on {dead}"


def _accel(**over):
    row = {
        "quarters_of_accelerating_revenue": 2,
        "income_quality_direction": "rising",
        "growth_basis": "5y",
        "revenue_cagr_5y": 0.10,
        "revenue_cagr_3y": None,
        "fcf_per_share_cagr_5y": 0.10,
        "fcf_per_share_cagr_3y": None,
        "fcf_inflection": False,
        "fcf_per_share_delta_abs": None,
    }
    row.update(over)
    survivors, _ = screen.apply_growth(pl.DataFrame([row]), "accel")
    return survivors.height == 1


def test_accel_requires_both_acceleration_and_rising_cash_conversion():
    assert _accel() is True
    assert _accel(quarters_of_accelerating_revenue=1) is False
    assert _accel(income_quality_direction="falling") is False
    assert _accel(quarters_of_accelerating_revenue=None) is False


# --------------------------------------------------------------------------
# Lane margin2y
# --------------------------------------------------------------------------


def _m2y(**over):
    row = {
        "operating_margin_latest": 0.12,
        "operating_margin_2y_ago": 0.04,
        "operating_margin_delta": -0.03,
        "growth_basis": "5y",
        "revenue_cagr_5y": 0.10,
        "revenue_cagr_3y": None,
        "fcf_per_share_cagr_5y": 0.10,
        "fcf_per_share_cagr_3y": None,
        "fcf_inflection": False,
        "fcf_per_share_delta_abs": None,
    }
    row.update(over)
    survivors, _ = screen.apply_growth(pl.DataFrame([row]), "margin2y")
    return survivors.height == 1


def test_recent_turn_the_five_year_window_cannot_see():
    """Margin down over 5 years, up over 2. The main lane rejects this by
    construction; it is the entire cohort this lane exists for."""
    assert _m2y() is True


def test_a_name_main_already_finds_is_excluded():
    """Disjoint from main by construction -- measured 0.0% overlap."""
    assert _m2y(operating_margin_delta=0.05) is False


def test_still_requires_profitability_and_a_real_turn():
    assert _m2y(operating_margin_latest=-0.02) is False
    assert _m2y(operating_margin_2y_ago=0.20) is False
    assert _m2y(operating_margin_2y_ago=None) is False


def test_margin2y_refuses_to_run_on_a_store_without_the_column():
    """🔴 A missing INPUT is not an empty result. On an older gate0.csv this
    lane must refuse with an actionable message, not return zero names and
    let that read as 'nothing passed'."""
    frame = pl.DataFrame([{"operating_margin_latest": 0.12}])
    with pytest.raises(SystemExit) as excinfo:
        screen.apply_growth(frame, "margin2y")
    assert "gate0.py" in str(excinfo.value)


# --------------------------------------------------------------------------
# Fix 4 -- the unevaluated lane
# --------------------------------------------------------------------------


def test_unevaluated_quality_does_not_demand_the_legs_that_never_ran():
    """🔴 Guaranteed-zero bug, caught before shipping. These names are in the
    lane BECAUSE their annual Gate 0 legs never computed, so income_quality,
    sbc_pct_revenue and tangible_book are null for most. The ordinary quality
    leg requires all three non-null and rejected all 41 rows that reached it.
    A lane built to admit not-yet-evaluated companies must not then demand
    the evaluation."""
    frame = pl.DataFrame(
        [{"income_quality": None, "sbc_pct_revenue": None, "tangible_book": None}]
    )
    survivors, rejected = screen.apply_quality_unevaluated(frame)
    assert survivors.height == 1 and rejected.height == 0


def test_unevaluated_still_rejects_a_leg_that_DID_compute_and_failed():
    """Being unevaluated on one leg is not a licence to ignore one that ran."""
    frame = pl.DataFrame(
        [{"income_quality": 0.2, "sbc_pct_revenue": None, "tangible_book": None}]
    )
    survivors, rejected = screen.apply_quality_unevaluated(frame)
    assert survivors.height == 0
    assert rejected["rejected_because"][0] == "quality:evaluable_leg_failed"


def test_unevaluated_eligibility_inverts_the_status_test():
    frame = pl.DataFrame(
        [
            {"ticker": "U", "gate0_status": "unknown", "revenue": 100e6, "sic": 3600, "cik": 1},
            {"ticker": "P", "gate0_status": "pass", "revenue": 100e6, "sic": 3600, "cik": 2},
        ]
    )
    survivors, _ = screen.apply_eligibility(frame, _Args("unevaluated"), set())
    assert survivors["ticker"].to_list() == ["U"]


# --------------------------------------------------------------------------
# income_quality ceiling
# --------------------------------------------------------------------------


def _iq(income_quality, net_income, revenue):
    """compute_metrics RECOMPUTES income_quality as OCF/NI, so the fixture has
    to supply an OCF that produces the ratio under test rather than passing
    the ratio in and expecting it to survive."""
    ocf = income_quality * net_income
    frame = pl.DataFrame(
        [
            {
                "cik": 1,
                "fiscal_year": 2025,
                "net_income": net_income,
                "revenue": revenue,
                "ocf": ocf,
                "capex": 1.0,
                "sbc": 1.0,
                "operating_income": 1.0,
                "equity": 1.0,
                "goodwill": 0.0,
                "intangibles": 0.0,
                "cash": 1.0,
                "total_debt": 0.0,
                "shares_diluted": 1.0,
                "tax_expense": 1.0,
                "pretax_income": 1.0,
                "acquisitions": 0.0,
                "buybacks": 0.0,
                "dep_amort": 1.0,
            }
        ]
    )
    return gate0.compute_metrics(frame)


def test_nog_shape_is_flagged():
    """🔴 Live on the book: the console cites NOG's 'income quality 38.8x' as
    a STRENGTH in the same entry that describes its earnings collapse as a
    red flag. $38.8M of net income on $2.48B of revenue is a 1.57% net
    margin -- the ratio is measuring a vanishing denominator."""
    out = _iq(38.8, 38_800_000.0, 2_480_000_000.0)
    assert out["income_quality_suspect"][0] is True
    assert out["net_margin"][0] == pytest.approx(0.01565, abs=1e-4)


def test_normal_cash_conversion_is_not_flagged():
    out = _iq(1.4, 300_000_000.0, 2_000_000_000.0)
    assert out["income_quality_suspect"][0] is False


def test_the_low_end_test_is_untouched():
    """The 0.90 floor caught JOYY at 0.14x and must keep doing so -- the
    ceiling is a second, independent flag, not a replacement."""
    out = _iq(0.14, 2_098_000_000.0, 4_000_000_000.0)
    assert out["income_quality_suspect"][0] is False
    assert out["income_quality"][0] == pytest.approx(0.14, abs=1e-6)
