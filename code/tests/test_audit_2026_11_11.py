"""Regression tests for the 2026-11-11 audit.

Three fail-open paths between Gate 0 and the review queue:

1. a market cap the loader rejected was rebuilt as price x filed shares;
2. the queue did not say which caps were derived, nor rank them last;
3. the dollar revenue floor was compared against reporting-currency revenue.

Deliberately NOT changed:

- an unmeasured lease stays a reader flag and does not hold the FCF legs (ZIM).
  That is Fer's 2026-10-16 ruling, pinned by
  test_frozen_fixture.py::test_zim_lease_upper_bound_flag_without_verdict_change;
- with no band arguments and no price file the lanes still return their
  quality survivors unpriced (test_gate0.py::test_fcx_appears_in_inflection_lane_
  not_main_lane). rank_queue already puts a null cap in tier E, "price first".
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

import polars as pl
import pytest

import rank_queue as rq
import screen

TODAY = date(2026, 11, 11)

PRICE_HEADER = (
    "ticker,price,ma_200,market_cap,as_of,market_cap_fetched_at,market_cap_source,"
    "market_cap_currency,market_cap_security,market_cap_effective_at\n"
)


def _price_file(tmp_path, rows):
    """rows: (ticker, price, cap, days since the cap was fetched)."""
    lines = []
    for ticker, price, cap, age_days in rows:
        fetched = (date.today() - timedelta(days=age_days)).isoformat()
        lines.append(
            f"{ticker},{price},,{cap},{fetched},{fetched}T00:00:00Z,yf,USD,"
            f"{ticker}|NYQ|EQUITY,unknown\n"
        )
    path = tmp_path / "prices.csv"
    path.write_text(PRICE_HEADER + "".join(lines), encoding="utf-8")
    tickers = {row[0] for row in rows}
    return screen.load_prices(str(path), tickers, str(tmp_path / "unmatched.csv"))


# --------------------------------------------------------------------------
# 1. A rejected cap is not rebuilt from price x filed shares
# --------------------------------------------------------------------------


def test_stale_supplied_cap_leaves_no_cap_instead_of_a_derived_one(tmp_path):
    # Arrange: THOU tags its shares in thousands, ADR5 is 5 ordinary per ADR.
    prices = _price_file(tmp_path, [("THOU", 100, 5e9, 10), ("ADR5", 50, 2e9, 10)])
    frame = pl.DataFrame({"ticker": ["THOU", "ADR5"],
                          "shares_diluted": [50_000.0, 200_000_000.0]})

    # Act
    out = {r["ticker"]: r for r in screen.apply_prices(frame, prices).iter_rows(named=True)}

    # Assert: neither the $5M nor the $10B guess is published.
    for ticker in ("THOU", "ADR5"):
        assert out[ticker]["market_cap_rejected"] == "stale_fetch"
        assert out[ticker]["market_cap"] is None
        assert out[ticker]["market_cap_derived"] is False


def test_rejected_cap_is_cut_as_missing_so_it_gets_repriced(tmp_path):
    prices = _price_file(tmp_path, [("OLD", 100, 5e9, 10), ("NEW", 100, 5e9, 0)])
    frame = pl.DataFrame({"ticker": ["OLD", "NEW"], "shares_diluted": [5e7, 5e7]})

    kept, rejected = screen.apply_band(screen.apply_prices(frame, prices), 100e6, None)

    assert kept["ticker"].to_list() == ["NEW"]
    # build_prices.py --from-lanes re-prices exactly this reason.
    assert rejected.select("ticker", "rejected_because").row(0) == ("OLD", "band:market_cap_missing")


def test_fresh_supplied_cap_is_used_and_the_identity_check_still_fires(tmp_path):
    prices = _price_file(tmp_path, [("THOU", 100, 5e9, 0)])
    frame = pl.DataFrame({"ticker": ["THOU"], "shares_diluted": [50_000.0]})

    out = screen.apply_prices(frame, prices).row(0, named=True)

    assert out["market_cap"] == 5e9
    assert out["market_cap_derived"] is False
    assert out["shares_identity_suspect"] is True


def test_price_file_without_a_cap_column_still_derives_and_says_so(tmp_path):
    """Unchanged behaviour: nothing was rejected, so the flagged estimate stands."""
    path = tmp_path / "prices.csv"
    path.write_text(f"ticker,price,as_of\nPLAIN,20,{date.today().isoformat()}\n", encoding="utf-8")
    prices = screen.load_prices(str(path), {"PLAIN"}, str(tmp_path / "unmatched.csv"))
    frame = pl.DataFrame({"ticker": ["PLAIN"], "shares_diluted": [1e8]})

    out = screen.apply_prices(frame, prices).row(0, named=True)

    assert out["market_cap"] == 2e9
    assert out["market_cap_derived"] is True


def _direct_gate0(**overrides):
    row = {
        "ticker": "REFILL", "cik": "1", "sic": "3500", "filing_form": "10-K",
        "gate0_framework_pass": "true", "revenue": "2e8", "ttm_fcf_after_sbc": "1e7",
        "revenue_cagr_3y": "0.05", "fcf_per_share_cagr_3y": "0.08",
        "fcf_per_share_cagr_5y": "0.06", "shares_diluted": "1e8",
        "fcf_after_sbc": "1e7", "reporting_currency": "USD",
    }
    row.update(overrides)
    return pl.DataFrame([row], schema={k: pl.Utf8 for k in row})


def test_refill_does_not_derive_a_cap_the_loader_rejected():
    prices = pl.DataFrame(
        {"ticker": ["REFILL"], "price": [10.0], "market_cap_supplied": [None],
         "market_cap_rejected": ["stale_fetch"]},
        schema_overrides={"market_cap_supplied": pl.Float64},
    )

    rows, _ = rq.direct_pass(_direct_gate0(), prices, set())

    assert rows["market_cap"][0] is None
    assert rows["market_cap_derived"][0] is False
    assert rows["market_cap_rejected"][0] == "stale_fetch"


# --------------------------------------------------------------------------
# 2. The queue names a derived cap and ranks it after a supplied one
# --------------------------------------------------------------------------


def _survivors(rows):
    base = {"sic": "3500", "filing_form": "10-K", "market_cap": 2e9, "fcf_after_sbc": 1e8,
            "store_defect_flags": "", "lane_count": 1, "lanes": "value",
            "reporting_currency": "USD", "gate0_framework_pass": True,
            "market_cap_derived": False}
    return pl.DataFrame([{**base, **r} for r in rows])


def test_derived_cap_sorts_after_a_supplied_cap_in_the_same_tier():
    # Arrange: the derived row is cheaper AND in more lanes -- it still goes last.
    ranked = rq.rank(_survivors([
        {"ticker": "DERIVED", "market_cap_derived": True, "lane_count": 3, "fcf_after_sbc": 4e8},
        {"ticker": "SUPPLIED"},
    ]), today=TODAY)

    order = ranked.sort("rank")["ticker"].to_list()
    reasons = dict(ranked.select("ticker", "rank_reason").iter_rows())

    assert order == ["SUPPLIED", "DERIVED"]
    assert set(ranked["tier"].to_list()) == {"A"}
    assert "CAP DERIVED" in reasons["DERIVED"]
    assert "CAP DERIVED" not in reasons["SUPPLIED"]


def test_queue_without_the_column_ranks_exactly_as_before():
    """A lane file from an older build has no market_cap_derived: nothing moves."""
    frame = _survivors([{"ticker": "CHEAP", "fcf_after_sbc": 4e8},
                        {"ticker": "DEAR"}]).drop("market_cap_derived")

    ranked = rq.rank(frame, today=TODAY)

    assert ranked.sort("rank")["ticker"].to_list() == ["CHEAP", "DEAR"]
    assert ranked["cap_derived"].to_list() == [False, False]


def test_cap_provenance_columns_are_carried_into_the_queue():
    for column in ("market_cap_derived", "market_cap_rejected",
                   "shares_identity_suspect", "implied_shares_ratio"):
        assert column in rq.CARRY


def test_one_lane_reporting_a_derived_cap_marks_the_merged_row(tmp_path):
    header = "ticker,cik,market_cap,market_cap_derived,rejected_because\n"
    (tmp_path / "shortlist_a.csv").write_text(header + "DUAL,1,2e9,false,\n", encoding="utf-8")
    (tmp_path / "shortlist_b.csv").write_text(header + "DUAL,1,2e9,true,\n", encoding="utf-8")

    merged, missing = rq.load_survivors(tmp_path, ("shortlist_a.csv", "shortlist_b.csv"))

    assert missing == []
    assert merged["market_cap_derived"].to_list() == [True]


# --------------------------------------------------------------------------
# 3. The dollar revenue floor is tested in dollars
# --------------------------------------------------------------------------


def _eligibility_frame():
    return pl.DataFrame({
        "ticker": ["KRWCO", "USDBIG", "USDSMALL"], "cik": [1, 2, 3],
        "sic": ["3570", "3570", "3570"], "gate0_status": ["pass", "pass", "pass"],
        "revenue": [60e9, 80e6, 45e6],          # KRW 60bn is about $43M
        "reporting_currency": ["KRW", "USD", "USD"],
    })


def _args(**overrides):
    values = dict(include_financials=False, exclude_sic="6000-6799", lane="value",
                  min_revenue=50e6)
    values.update(overrides)
    return argparse.Namespace(**values)


def test_foreign_revenue_without_a_rate_is_unresolved_not_passed():
    kept, batches = screen.apply_eligibility(_eligibility_frame(), _args(), set())
    reasons = dict(pl.concat(batches, how="diagonal_relaxed")
                   .select("ticker", "rejected_because").iter_rows())

    assert kept["ticker"].to_list() == ["USDBIG"]
    assert reasons == {"KRWCO": "unresolved:ineligible:min_revenue",
                       "USDSMALL": "ineligible:min_revenue"}


def test_foreign_revenue_with_a_dated_rate_is_tested_in_usd():
    def rates(usd_per_krw):
        return pl.DataFrame({"currency": ["KRW"], "usd_per_unit": [usd_per_krw],
                             "as_of": [date.today()]})

    below, _ = screen.apply_eligibility(_eligibility_frame(), _args(), set(), rates(0.00072))
    above, _ = screen.apply_eligibility(_eligibility_frame(), _args(), set(), rates(0.00100))

    assert below["ticker"].to_list() == ["USDBIG"]                 # $43.2M < $50M
    assert sorted(above["ticker"].to_list()) == ["KRWCO", "USDBIG"]  # $60.0M > $50M
    assert above.filter(pl.col("ticker") == "KRWCO")["revenue_usd"][0] == pytest.approx(60e6)


def test_store_without_a_currency_column_keeps_the_raw_comparison():
    frame = _eligibility_frame().drop("reporting_currency")

    kept, _ = screen.apply_eligibility(frame, _args(), set())

    assert sorted(kept["ticker"].to_list()) == ["KRWCO", "USDBIG"]
    assert "revenue_usd" not in kept.columns


def test_refill_revenue_floor_is_in_usd_too():
    foreign = _direct_gate0(reporting_currency="KRW", revenue="6e10")

    rows, funnel = rq.direct_pass(foreign, None, set())

    assert rows.height == 0
    assert dict(funnel)["revenue above $50M"] == 0
