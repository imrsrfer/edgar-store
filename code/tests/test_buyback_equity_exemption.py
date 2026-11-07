"""Buyback-equity exemption from the negative-tangible-book fail (2026-11-06).

Fer ruling 2026-11-06. Shapes from the point-in-time backtest stores: MCD/AZO
(negative equity from buybacks, profitable) are exempt; JNJ-style positive
equity with goodwill-driven negative tangible book is not.
"""

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gate0  # noqa: E402
import screen  # noqa: E402

B = 1e9


def _row(**over):
    row = dict(fail_tangible_book=True, tangible_book_basis=gate0.TANGIBLE_BOOK_BASIS_FISCAL_YEAR,
               equity=-4.0 * B, latest_q_equity=None, buybacks_5y=20.0 * B, net_income_5y=30.0 * B,
               net_income_loss_yrs_5y=0, net_income_yrs_5y=5)
    row.update(over)
    return gate0.add_buyback_equity_exemption(pl.DataFrame([row])).row(0, named=True)


def test_buyback_built_negative_equity_is_exempt():
    r = _row()
    assert r["tangible_book_buyback_exempt"] is True
    assert r["fail_tangible_book"] is False


def test_goodwill_driven_negative_tangible_book_is_not_exempt():
    """Positive equity: the hole is goodwill, not buybacks (JNJ/PFE, FLO/UPBD shape)."""
    r = _row(equity=60.0 * B)
    assert r["tangible_book_buyback_exempt"] is False
    assert r["fail_tangible_book"] is True


def test_buybacks_smaller_than_the_hole_are_not_exempt():
    r = _row(equity=-25.0 * B, buybacks_5y=20.0 * B)
    assert r["fail_tangible_book"] is True


def test_loss_built_deficit_is_not_exempt():
    assert _row(net_income_5y=-1.0 * B)["fail_tangible_book"] is True
    assert _row(net_income_loss_yrs_5y=2)["fail_tangible_book"] is True


def test_short_history_is_not_exempt():
    assert _row(net_income_yrs_5y=2)["fail_tangible_book"] is True


def test_null_inputs_fail_closed():
    assert _row(buybacks_5y=None)["fail_tangible_book"] is True
    assert _row(equity=None)["fail_tangible_book"] is True


def test_not_evaluable_leg_stays_null():
    r = _row(fail_tangible_book=None)
    assert r["fail_tangible_book"] is None
    assert r["tangible_book_buyback_exempt"] is None


def test_passing_leg_is_untouched():
    r = _row(fail_tangible_book=False, equity=10.0 * B)
    assert r["fail_tangible_book"] is False
    assert r["tangible_book_buyback_exempt"] is None


def test_latest_quarter_vintage_uses_quarterly_equity():
    r = _row(tangible_book_basis=gate0.TANGIBLE_BOOK_BASIS_LATEST_Q, equity=5.0 * B,
             latest_q_equity=-3.0 * B)
    assert r["tangible_book_buyback_exempt"] is True
    r = _row(tangible_book_basis=gate0.TANGIBLE_BOOK_BASIS_LATEST_Q, equity=-3.0 * B,
             latest_q_equity=5.0 * B)
    assert r["fail_tangible_book"] is True


def test_exempt_row_reaches_test_pass_through_add_verdict():
    frame = pl.DataFrame([dict(
        cik=1, ticker="AZO", fail_tangible_book=True,
        tangible_book_basis=gate0.TANGIBLE_BOOK_BASIS_FISCAL_YEAR, equity=-4.0 * B,
        buybacks_5y=20.0 * B, net_income_5y=12.0 * B, net_income_loss_yrs_5y=0, net_income_yrs_5y=5,
        fail_income_quality=False, fail_fcf=False, fail_fcf_after_sbc=False, fail_sbc=False,
        fail_ni_over_oi=False, fail_tax_anomaly=False, imputed_fields="", carried_forward_fields="",
    )])
    out = gate0.add_verdict(gate0.add_buyback_equity_exemption(frame)).row(0, named=True)
    assert out["test_tangible_book"] == "PASS"


def _quality(**over):
    base = dict(income_quality=1.4, sbc_pct_revenue=0.02, tangible_book=-6.0 * B,
                tangible_book_yrs_negative=5, warn_inorganic=False,
                tangible_book_buyback_exempt=True)
    base.update(over)
    survivors, _ = screen.apply_quality(pl.DataFrame([base]))
    return survivors.height == 1


def test_screen_quality_honours_the_exemption():
    assert _quality() is True


def test_screen_quality_still_rejects_unexempt_negative_book():
    assert _quality(tangible_book_buyback_exempt=False) is False
    assert _quality(tangible_book_buyback_exempt=None) is False


def test_screen_without_the_column_exempts_nothing():
    survivors, _ = screen.apply_quality(pl.DataFrame([dict(
        income_quality=1.4, sbc_pct_revenue=0.02, tangible_book=-6.0 * B,
        tangible_book_yrs_negative=5, warn_inorganic=False)]))
    assert survivors.height == 0
