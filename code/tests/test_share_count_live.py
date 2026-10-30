"""Independent price/cap share checks and a restated FY split basis break."""

from datetime import date

import polars as pl
import pytest

import rank_queue as rq
import screen
import store_defects as sd


def _row(shares=80_726_000, cap=5_294_891_695.65, price=45.36, **overrides):
    values = dict(
        ticker="RUSHA", filing_form="10-Q", shares_diluted=shares,
        store_defect_flags="total_debt_partial", market_cap_supplied=cap,
        price=price, as_of=date(2026, 10, 16),
        market_cap_fetched_at="2026-10-17T12:00:00Z",
        market_cap_currency="USD",
    )
    values.update(overrides)
    return pl.DataFrame([values])


def test_rusha_and_iesc_live_gap_flags_and_preserves_store_value():
    rush = sd.add_share_count_live_check(_row()).row(0, named=True)
    assert rush["live_share_count"] == pytest.approx(116_730_415, rel=1e-5)
    assert rush["share_count_live_gap"] == pytest.approx(0.446, abs=0.001)
    assert rush["share_count_stale_vs_live"] is True
    assert rush["store_defect_flags"] == "total_debt_partial,share_count_stale_vs_live"
    assert rush["shares_diluted"] == 80_726_000

    iesc = sd.add_share_count_live_check(_row(
        shares=20_414_932, cap=10_684_315_204, price=251.96,
        ticker="IESC", store_defect_flags="",
    )).row(0, named=True)
    assert iesc["share_count_live_gap"] == pytest.approx(1.077, abs=0.001)
    assert iesc["share_count_stale_vs_live"] is True


def test_clean_live_pair_does_not_change_values_or_flags():
    row = _row(shares=100_000_000, cap=1_030_000_000, price=10,
               store_defect_flags="").with_columns(pl.lit(9.0).alias("fcf_per_share"))
    out = sd.add_share_count_live_check(row).row(0, named=True)
    assert out["share_count_stale_vs_live"] is False
    assert out["share_count_live_gap"] == pytest.approx(0.03)
    assert out["store_defect_flags"] == ""
    assert out["shares_diluted"] == 100_000_000
    assert out["fcf_per_share"] == 9.0


@pytest.mark.parametrize("change", [
    {"market_cap_supplied": None}, {"price": None}, {"price": 0},
    {"market_cap_currency": "CAD"}, {"market_cap_fetched_at": None},
    {"market_cap_fetched_at": "2026-10-27T12:00:00Z"},
    {"filing_form": "20-F"}, {"shares_diluted": None}, {"shares_diluted": 0},
])
def test_unevaluable_live_pair_is_null(change):
    out = sd.add_share_count_live_check(_row(**change)).row(0, named=True)
    for name in ("live_share_count", "share_count_live_gap", "share_count_stale_vs_live"):
        assert out[name] is None


def test_live_flag_demotes_queue_without_withholding_multiple():
    assert "share_count_stale_vs_live" in sd.FAIL_OPEN_DEFECTS
    frame = pl.DataFrame([{
        "ticker": "RUSHA", "sic": "5000", "filing_form": "10-Q",
        "market_cap": 2e9, "fcf_after_sbc": 1e8, "lane_count": 1,
        "lanes": "value", "reporting_currency": "USD",
        "gate0_framework_pass": True, "store_defect_flags": "share_count_stale_vs_live",
        "p_fcf_after_sbc_live": 20.0,
    }])
    out = rq.rank(frame).row(0, named=True)
    assert out["fail_open_defect"] is True
    assert out["p_fcf_after_sbc_live"] == 20.0


def test_screen_price_join_publishes_live_check():
    frame = pl.DataFrame([{"ticker": "RUSHA", "filing_form": "10-Q",
                           "shares_diluted": 80_726_000.0, "store_defect_flags": ""}])
    prices = _row().drop("filing_form", "shares_diluted", "store_defect_flags")
    prices = prices.with_columns(pl.lit(None, dtype=pl.Float64).alias("ma_200"))
    out = screen.apply_prices(frame, prices).row(0, named=True)
    assert out["share_count_stale_vs_live"] is True
    assert out["live_share_count"] == pytest.approx(116_730_415, rel=1e-5)


def test_direct_refill_carries_live_check():
    from test_rank_queue import _gate0

    gate0 = _gate0([{"ticker": "RUSHA", "shares_diluted": "80726000",
                     "store_defect_flags": "", "filing_form": "10-Q"}])
    prices = _row().drop("filing_form", "shares_diluted", "store_defect_flags")
    rows, _ = rq.direct_pass(gate0, prices, set())
    assert rows.height == 1
    assert rows["share_count_stale_vs_live"][0] is True
    assert rows["live_share_count"][0] == pytest.approx(116_730_415, rel=1e-5)
    assert rows["store_defect_flags"][0] == "share_count_stale_vs_live"


def test_stale_cap_dropped_by_loader_yields_null_live_check():
    frame = pl.DataFrame([{"ticker": "RUSHA", "filing_form": "10-Q",
                           "shares_diluted": 80_726_000.0, "store_defect_flags": ""}])
    raw = _row().drop("filing_form", "shares_diluted", "store_defect_flags")
    raw = raw.with_columns(pl.lit(None, dtype=pl.Float64).alias("ma_200"))
    prices = screen.enforce_cap_freshness(
        raw, raw, {c: c for c in raw.columns}, date(2026, 10, 20)
    )
    out = screen.apply_prices(frame, prices).row(0, named=True)
    assert out["market_cap_derived"] is True
    assert out["live_share_count"] is None
    assert out["share_count_live_gap"] is None
    assert out["share_count_stale_vs_live"] is None


def test_deck_split_basis_break_and_issuance_controls():
    from test_store_defects import _fy, _frame, _row as store_row

    cases = {
        1: [(2021, 28.4e6, False), (2022, 27.8e6, False),
            (2023, 160.1e6, True), (2024, 156.3e6, False),
            (2025, 152.7e6, False), (2026, 145.8e6, False)],
        2: [(2024, 100e6, False), (2025, 160e6, False)],
        3: [(2024, 100e6, False), (2025, 110e6, True)],
        4: [(2024, 100e6, False), (2025, 100e9, True)],
        5: [(2024, 100e6, False), (2025, 10e6, True)],
        6: [(2024, 100e6, False), (2025, 150e6, True)],  # RBA-like merger
    }
    facts = pl.DataFrame([
        {**_fy(cik, "shares_diluted", year, value), "restated": restated}
        for cik, rows in cases.items() for year, value, restated in rows
    ])
    frame = _frame([store_row(cik, period_end=date(2026, 12, 31)) for cik in cases])
    out = sd.add_store_defect_flags(frame, facts)
    rows = {r["cik"]: r for r in out.iter_rows(named=True)}
    assert rows[1]["shares_split_basis_break"] is True
    assert rows[1]["shares_split_basis_ratio"] == pytest.approx(160.1 / 27.8)
    assert rows[1]["shares_history_scale_break"] is False
    assert rows[2]["shares_split_basis_break"] is False
    assert rows[3]["shares_split_basis_break"] is False
    assert rows[4]["shares_split_basis_break"] is False
    assert rows[4]["shares_history_scale_break"] is True
    assert rows[5]["shares_split_basis_break"] is True
    assert rows[6]["shares_split_basis_break"] is False


def test_split_basis_break_abstains_with_only_one_annual_count():
    from test_store_defects import _fy, _frame, _row as store_row

    facts = pl.DataFrame([
        {**_fy(70, "shares_diluted", 2025, 100e6), "restated": True},
    ])
    frame = _frame([store_row(70, period_end=date(2025, 12, 31))])
    row = sd.add_store_defect_flags(frame, facts).row(0, named=True)
    assert row["shares_split_basis_break"] is None
    assert row["shares_split_basis_ratio"] is None
