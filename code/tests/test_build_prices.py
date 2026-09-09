"""Tests for build_prices.py.

🔴 The oracle here is COVERAGE, not exit code. build_prices exits 0 whether it
prices 6,000 tickers or 3, so every test that matters asks "does the report name
what it failed to price", never "did it succeed". A silent 137-of-6031 is the
failure mode; a test suite that only checks the happy path reproduces it.
"""

from __future__ import annotations

import io
from datetime import date, timedelta

import polars as pl
import pytest

import build_prices


TODAY = date(2026, 9, 9)


def _row(ticker, as_of, price=100.0):
    return {
        "ticker": ticker,
        "price": price,
        "ma_200": 90.0,
        "market_cap": 1e9,
        "as_of": as_of,
    }


# --------------------------------------------------------------------------
# Freshness: re-fetch, never append
# --------------------------------------------------------------------------


def test_stale_row_is_refetched_not_reused():
    existing = {"MCRI": _row("MCRI", date(2026, 1, 1))}
    reusable, needs_fetch = build_prices.partition_by_freshness(
        ["MCRI"], existing, 7, TODAY
    )
    assert reusable == {}
    assert needs_fetch == ["MCRI"]


def test_fresh_row_is_reused_without_a_fetch():
    existing = {"AAPL": _row("AAPL", date(2026, 9, 5))}
    reusable, needs_fetch = build_prices.partition_by_freshness(
        ["AAPL"], existing, 7, TODAY
    )
    assert list(reusable) == ["AAPL"]
    assert needs_fetch == []


def test_row_exactly_at_the_age_boundary_is_still_fresh():
    """7 days old with --max-age-days 7 is inside the window, not outside."""
    existing = {"X": _row("X", TODAY - timedelta(days=7))}
    reusable, needs_fetch = build_prices.partition_by_freshness(["X"], existing, 7, TODAY)
    assert list(reusable) == ["X"]
    assert needs_fetch == []


def test_row_present_but_unpriced_is_refetched():
    """A row with a null price is not a price. It must not count as covered."""
    existing = {"X": _row("X", TODAY, price=None)}
    reusable, needs_fetch = build_prices.partition_by_freshness(["X"], existing, 7, TODAY)
    assert reusable == {}
    assert needs_fetch == ["X"]


def test_output_has_exactly_one_row_per_ticker():
    """The append bug: a second row for a ticker, not a replacement."""
    rows = {"B": _row("B", TODAY), "A": _row("A", TODAY)}
    frame = build_prices.build_frame(rows)
    assert frame["ticker"].to_list() == ["A", "B"]
    assert frame.columns == build_prices.PRICE_COLUMNS
    assert frame["ticker"].n_unique() == frame.height


# --------------------------------------------------------------------------
# Coverage reporting -- the actual oracle
# --------------------------------------------------------------------------


def test_unresolvable_tickers_are_reported_by_name_not_merely_counted():
    """The whole point. A ticker that fails to resolve must be NAMED."""
    stream = io.StringIO()
    stats = build_prices.report_coverage(
        requested=["AAPL", "ZZQQXX", "NOTATICKER9"],
        priced={"AAPL": _row("AAPL", TODAY)},
        carried={},
        max_age_days=7,
        today=TODAY,
        out_path="prices.csv",
        stream=stream,
    )
    text = stream.getvalue()
    assert stats["requested"] == 3
    assert stats["priced"] == 1
    assert stats["unpriced"] == 2
    assert sorted(stats["unpriced_tickers"]) == ["NOTATICKER9", "ZZQQXX"]
    assert "ZZQQXX" in text
    assert "NOTATICKER9" in text


def test_coverage_is_printed_even_when_everything_resolves():
    """Coverage on EVERY run. A report that only appears on failure is a report
    nobody reads on the run that quietly halved."""
    stream = io.StringIO()
    build_prices.report_coverage(
        ["AAPL"], {"AAPL": _row("AAPL", TODAY)}, {}, 7, TODAY, "p.csv", stream=stream
    )
    text = stream.getvalue()
    assert "PRICE COVERAGE" in text
    assert "tickers requested       : 1" in text
    assert "UNPRICED                : 0" in text


def test_coverage_counts_rows_older_than_the_window():
    stream = io.StringIO()
    stats = build_prices.report_coverage(
        ["A", "B"],
        {"A": _row("A", date(2026, 1, 1)), "B": _row("B", TODAY)},
        {},
        7,
        TODAY,
        "p.csv",
        stream=stream,
    )
    assert stats["stale"] == 1
    assert "STALE" in stream.getvalue()


def test_zero_coverage_still_reports_rather_than_looking_like_success():
    """A run that prices nothing exits 0. The report is the only signal."""
    stream = io.StringIO()
    stats = build_prices.report_coverage(
        ["AAA", "BBB"], {}, {}, 7, TODAY, "p.csv", stream=stream
    )
    assert stats["priced"] == 0
    assert stats["unpriced"] == 2
    assert "AAA" in stream.getvalue() and "BBB" in stream.getvalue()


# --------------------------------------------------------------------------
# The ticker list is the UNION of every lane
# --------------------------------------------------------------------------


def _lane_csv(path, rows):
    pl.DataFrame(
        rows, schema={"ticker": pl.String, "rejected_because": pl.String}
    ).write_csv(path)


def test_lane_union_takes_quality_survivors_from_every_lane(tmp_path):
    """Each lane contributes; a name in only one lane must still be priced."""
    _lane_csv(
        tmp_path / "shortlist_main.csv",
        [
            {"ticker": "AAA", "rejected_because": ""},
            {"ticker": "DEAD1", "rejected_because": "growth"},
        ],
    )
    _lane_csv(
        tmp_path / "shortlist_value.csv",
        [{"ticker": "BBB", "rejected_because": "band:market_cap_missing"}],
    )
    _lane_csv(
        tmp_path / "shortlist_accel.csv",
        [{"ticker": "CCC", "rejected_because": "momentum"}],
    )
    tickers, missing, per_lane = build_prices.tickers_from_lanes(tmp_path)
    assert tickers == ["AAA", "BBB", "CCC"]
    assert per_lane["shortlist_main.csv"] == 1
    assert "shortlist_ifrs.csv" in missing


def test_rows_cut_before_the_quality_stage_are_not_priced(tmp_path):
    """Pricing a row the screen already rejected on growth wastes a fetch and
    inflates coverage against a list nobody screens."""
    _lane_csv(
        tmp_path / "shortlist_main.csv",
        [
            {"ticker": "KEEP", "rejected_because": "band:market_cap"},
            {"ticker": "GONE1", "rejected_because": "quality"},
            {"ticker": "GONE2", "rejected_because": "ineligible:gate0_status"},
            {"ticker": "GONE3", "rejected_because": "margin_expansion"},
        ],
    )
    tickers, _, _ = build_prices.tickers_from_lanes(tmp_path)
    assert tickers == ["KEEP"]


def test_missing_lane_files_are_named_not_silently_skipped(tmp_path):
    """An absent lane file means its survivors go unpriced. Say so."""
    tickers, missing, _ = build_prices.tickers_from_lanes(tmp_path)
    assert tickers == []
    assert len(missing) == len(build_prices.LANE_FILES)


# --------------------------------------------------------------------------
# End to end, with the network seam faked
# --------------------------------------------------------------------------


def test_end_to_end_replaces_stale_keeps_fresh_carries_unrequested(tmp_path, monkeypatch):
    out = tmp_path / "prices.csv"
    out.write_text(
        "ticker,price,ma_200,market_cap,as_of\n"
        "STALE,1.11,1.11,111.0,2026-01-01\n"
        "FRESH,999.99,999.99,999.0,2026-09-09\n"
        "LEGACY,5.00,5.00,50.0,2026-09-08\n"
    )

    def fake_fetch(tickers, as_of):
        assert "FRESH" not in tickers, "a fresh row must not be re-fetched"
        return {t: _row(t, as_of, price=42.0) for t in tickers if t != "BADTICK"}

    monkeypatch.setattr(build_prices, "fetch_quotes", fake_fetch)
    monkeypatch.setattr(build_prices, "date", _FrozenDate)

    listing = tmp_path / "list.txt"
    listing.write_text("STALE\nFRESH\nBADTICK\n")
    assert (
        build_prices.main(
            ["--tickers-from", str(listing), "--out", str(out), "--root", str(tmp_path)]
        )
        == 0
    )

    result = pl.read_csv(out)
    by = {r["ticker"]: r for r in result.to_dicts()}
    assert result["ticker"].n_unique() == result.height
    assert by["STALE"]["price"] == 42.0, "stale row must be replaced, not kept"
    assert by["FRESH"]["price"] == 999.99, "fresh row must be reused untouched"
    assert by["LEGACY"]["price"] == 5.0, "an unrequested row is kept, not destroyed"
    assert "BADTICK" not in by, "an unresolvable ticker must not get an invented row"


def test_fetch_failure_never_carries_a_previous_price_forward(tmp_path, monkeypatch):
    """A name with no price this week has no price this week."""
    out = tmp_path / "prices.csv"
    out.write_text("ticker,price,ma_200,market_cap,as_of\nX,7.00,7.00,70.0,2026-01-01\n")
    monkeypatch.setattr(build_prices, "fetch_quotes", lambda tickers, as_of: {})
    monkeypatch.setattr(build_prices, "date", _FrozenDate)

    assert (
        build_prices.main(["--tickers", "X", "--out", str(out), "--root", str(tmp_path)])
        == 0
    )
    result = pl.read_csv(out)
    # X was requested, went stale, failed to fetch -> it must be GONE, not
    # silently still showing January's price as if it were current.
    assert "X" not in result["ticker"].to_list()


def test_from_lanes_refuses_to_write_an_empty_price_file(tmp_path):
    """An empty list would produce a valid, empty prices.csv and exit 0 --
    exactly the shape that reads as 'nothing to price'."""
    with pytest.raises(SystemExit, match="no quality-stage survivors"):
        build_prices.main(["--from-lanes", "--root", str(tmp_path)])


class _FrozenDate(date):
    """date with today() pinned, so freshness tests do not drift with the clock."""

    @classmethod
    def today(cls):
        return TODAY
