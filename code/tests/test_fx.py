"""Dated FX conversion stays offline and fails closed on missing evidence."""

from datetime import date, timedelta

import polars as pl
import pytest

from fx import add_usd_fcf, load_fx


TODAY = date(2026, 10, 22)


@pytest.mark.parametrize("rate,age,owed", [
    (1.25, 0, False), (1.25, 7, False), (1.25, 8, True),
    (1.25, -1, True), (0.0, 0, True), (-1.0, 0, True),
    (float("inf"), 0, True), (float("nan"), 0, True), (None, 0, True),
])
def test_rate_must_be_positive_finite_and_no_more_than_seven_days_old(rate, age, owed):
    frame = pl.DataFrame({"reporting_currency": ["EUR"], "fcf_after_sbc": [100.0]})
    rates = pl.DataFrame({"currency": ["EUR"], "usd_per_unit": [rate],
                          "as_of": [TODAY - timedelta(days=age)]})
    row = add_usd_fcf(frame, rates, TODAY).row(0, named=True)
    assert row["fx_owed"] is owed
    assert row["fcf_after_sbc_usd"] == (None if owed else 125.0)
    assert row["fx_as_of"] == (None if owed else TODAY - timedelta(days=age))


def test_usd_needs_no_rate_and_unknown_currency_is_never_assumed_usd():
    frame = pl.DataFrame({"reporting_currency": ["USD", None, "", "unknown", "EUR"],
                          "fcf_after_sbc": [100.0] * 5})
    out = add_usd_fcf(frame, today=TODAY)
    assert out["fx_owed"].to_list() == [False, True, True, True, True]
    assert out["fcf_after_sbc_usd"].to_list() == [100.0, None, None, None, None]
    assert add_usd_fcf(frame.drop("reporting_currency"), today=TODAY)["fx_owed"].all()


def test_unresolved_currency_blocks_even_a_usd_label():
    frame = pl.DataFrame({"reporting_currency": ["USD"], "currency_unresolved": [True],
                          "fcf_after_sbc": [100.0]})
    assert add_usd_fcf(frame, today=TODAY)["fx_owed"][0] is True


def test_optional_local_csv_loads_and_validates_contract(tmp_path):
    path = tmp_path / "fx.csv"
    assert load_fx(path) is None
    path.write_text("currency,usd_per_unit,as_of\nEUR,1.25,2026-10-20\n", encoding="utf-8")
    rates = load_fx(path)
    assert rates["usd_per_unit"][0] == 1.25
    path.write_text("currency,usd_per_unit,as_of\nEUR,1.25,2026-10-20\neur,1.2,2026-10-21\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="duplicate"):
        load_fx(path)
    path.write_text("currency,rate\nEUR,1.25\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="requires"):
        load_fx(path)
