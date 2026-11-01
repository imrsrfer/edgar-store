"""Scorecard contracts: deterministic prices only, never a live network."""

import csv
import socket
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import scorecard


DAY = date(2024, 1, 2)
AS_OF = date(2026, 1, 1)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("scorecard tests must not use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)


def write_csv(tmp_path, rows, columns=None):
    path = tmp_path / "decisions.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns or list(dict.fromkeys(
            key for record in rows for key in record)))
        writer.writeheader()
        writer.writerows(rows)
    return path


def row(decision_id="d1", **changes):
    return {"decision_id": decision_id, "decided_at": DAY.isoformat(),
            "ticker": "ABC", "disposition": "BUY", **changes}


def sessions(count=254, initial=100, final=120):
    days = tuple(DAY + timedelta(days=i) for i in range(count * 2)
                 if (DAY + timedelta(days=i)).weekday() < 5)[:count]
    return [(day, initial if i < 64 else final) for i, day in enumerate(days)]


def provider(series):
    return lambda symbol, start, end: [(d, p) for d, p in series.get(symbol, [])
                                       if start <= d <= end]


def grade(tmp_path, rows=None, prices=None, as_of=AS_OF):
    decisions = scorecard.read_decisions(write_csv(tmp_path, rows or [row()]))
    return scorecard.grade_decisions(decisions, as_of, provider(prices or {
        "ABC": sessions(), "SPY": [(DAY, 100), (AS_OF, 100)]}))


def test_entry_is_first_close_strictly_after_decision(tmp_path):
    prices = {"ABC": [(DAY, 999), *sessions()[1:]], "SPY": [(DAY, 100)]}
    result = grade(tmp_path, prices=prices)[0]
    assert result.horizon == 63 and result.status == "settled"
    assert result.entry_date == sessions()[1][0]
    assert result.exit_date == sessions()[64][0]
    assert result.entry_close == 100 and result.exit_close == 120
    assert result.stock_return == pytest.approx(.2)


def test_incomplete_window_is_pending_without_partial_return(tmp_path):
    result = grade(tmp_path, prices={"ABC": sessions(64), "SPY": [(DAY, 100)]})
    assert {g.status for g in result} == {"pending"}
    assert all(g.excess_return is None and g.hit is None for g in result)


def test_as_of_bounds_even_a_provider_that_ignores_end(tmp_path):
    decisions = scorecard.read_decisions(write_csv(tmp_path, [row()]))
    result = scorecard.grade_decisions(
        decisions, sessions()[63][0], lambda *args: sessions())
    assert all(g.status == "pending" for g in result)


def test_benchmark_aligns_on_or_before_both_dates(tmp_path):
    bars = sessions()
    prices = {"ABC": bars, "SPY": [(DAY, 100),
              (bars[64][0] - timedelta(days=1), 110), (bars[64][0] + timedelta(days=1), 999)]}
    result = grade(tmp_path, prices=prices)[0]
    assert result.status == "settled"
    assert result.benchmark_entry_date == DAY
    assert result.benchmark_exit_date < result.exit_date
    assert result.benchmark_return == pytest.approx(.1)
    assert result.excess_return == pytest.approx(.1)


@pytest.mark.parametrize("word,canonical,hit", [
    ("BUY", "pipeline", True), ("pIpElInE", "pipeline", True),
    ("WATCH", "watchlist", True), ("Watchlist", "watchlist", True),
    ("DISCARD", "discarded", False), ("Discarded", "discarded", False),
    ("OWED", "owed", None), ("owed", "owed", None)])
def test_disposition_aliases_and_direction(tmp_path, word, canonical, hit):
    result = grade(tmp_path, [row(disposition=word)])[0]
    assert result.decision.disposition == canonical
    assert result.hit is hit


def test_discarded_negative_excess_is_a_hit_and_zero_is_a_miss(tmp_path):
    result = grade(tmp_path, [row(disposition="discarded")],
                   {"ABC": sessions(final=80), "SPY": [(DAY, 100)]})
    assert result[0].hit is True
    result = grade(tmp_path, prices={"ABC": sessions(final=100), "SPY": [(DAY, 100)]})
    assert result[0].hit is False


def test_no_prices_are_counted_and_written_to_detail(tmp_path):
    path = write_csv(tmp_path, [row(ticker="DELISTED")])
    out = tmp_path / "results"
    assert scorecard.main(["--decisions", str(path), "--out", str(out),
                           "--as-of", AS_OF.isoformat()], provider({})) == 0
    with (out / "scorecard_detail.csv").open(newline="", encoding="utf-8") as handle:
        detail = list(csv.DictReader(handle))
    assert len(detail) == 3
    assert {r["ticker"] for r in detail} == {"DELISTED"}
    assert {r["status"] for r in detail} == {"no_data"}
    assert all(r["excess_return"] == "" for r in detail)
    report = (out / "scorecard.md").read_text(encoding="utf-8")
    assert "no_data=3" in report


def test_metrics_counts_directional_slugging_and_separate_superseded(tmp_path):
    rows = [row("win", ticker="UP", stage_failed="quality"),
            row("loss", ticker="DOWN", stage_failed="quality"),
            row("owed", ticker="UP", disposition="owed", stage_failed="quality"),
            row("old", ticker="UP", superseded_by="win", stage_failed="quality"),
            row("missing", ticker="GONE", stage_failed="quality"),
            row("young", ticker="YOUNG", stage_failed="quality")]
    prices = {"UP": sessions(final=140), "DOWN": sessions(final=80),
              "YOUNG": sessions(5), "SPY": [(DAY, 100)]}
    grades = grade(tmp_path, rows, prices)
    metrics = scorecard.aggregate(grades, "stage_failed")
    current = next(m for m in metrics if m.horizon == 63 and m.cohort == "current")
    assert (current.n, current.settled, current.pending, current.no_data) == (5, 3, 1, 1)
    assert current.mean_excess == pytest.approx(.2)
    assert current.median_excess == pytest.approx(.4)
    assert current.hit_rate == .5 and current.slugging == pytest.approx(2)
    old = next(m for m in metrics if m.horizon == 63 and m.cohort == "superseded")
    assert old.n == 1


def test_owed_metrics_are_unscored(tmp_path):
    metrics = scorecard.aggregate(grade(tmp_path, [row(disposition="owed")]), "disposition")
    assert all(m.hit_rate is None and m.slugging is None for m in metrics)
    assert all(m.mean_excess is not None for m in metrics)


def test_benchmark_override_and_price_at_decision_is_unused(tmp_path):
    decisions = scorecard.read_decisions(write_csv(tmp_path, [
        row("default", price_at_decision="999"), row("override", benchmark="qqq")]), "DIA")
    assert [d.benchmark for d in decisions] == ["DIA", "QQQ"]
    prices = {"ABC": sessions(), "DIA": [(DAY, 100)], "QQQ": [(DAY, 100), (AS_OF, 500)]}
    results = scorecard.grade_decisions(decisions, AS_OF, provider(prices))
    assert results[0].entry_close == 100


@pytest.mark.parametrize("field,value,message", [
    ("decision_id", "", "decision_id"), ("decided_at", "2024-1-02", "YYYY-MM-DD"),
    ("decided_at", "2024-02-30", "decided_at"), ("ticker", "", "ticker"),
    ("disposition", "HOLD", "disposition"), ("price_at_decision", "nan", "price_at_decision"),
    ("price_at_decision", "-1", "price_at_decision"), ("cik", "oops", "cik")])
def test_bad_csv_values_fail_clearly(tmp_path, field, value, message):
    with pytest.raises(ValueError, match=message):
        scorecard.read_decisions(write_csv(tmp_path, [row(**{field: value})]))


def test_missing_required_column_fails_before_fetch(tmp_path, capsys):
    path = write_csv(tmp_path, [{"ticker": "ABC"}])
    with pytest.raises(SystemExit) as exc:
        scorecard.main(["--decisions", str(path), "--out", str(tmp_path / "out")])
    assert exc.value.code == 2
    assert "missing required columns" in capsys.readouterr().err


def test_duplicate_csv_ids_fail(tmp_path):
    with pytest.raises(ValueError, match="duplicate decision_id.*d1"):
        scorecard.read_decisions(write_csv(tmp_path, [row(), row()]))


def test_run_log_parser_good_line(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("\ufeff\nDECISION · abc · 0000123456 · Watchlist · Gate 0 · 2024-01-02T23:30:00Z\n",
                    encoding="utf-8")
    decision, = scorecard.read_decisions(path)
    assert decision.decision_id == "2024-01-02T23:30:00Z|ABC"
    assert decision.ticker == "ABC" and decision.cik == "0000123456"
    assert decision.disposition == "watchlist" and decision.stage_failed == "Gate 0"
    assert decision.decided_at == DAY


@pytest.mark.parametrize("bad", ["DECISION · ABC · BUY", "DECISION · ABC · 123 · BUY · stage · bad-date",
                                     "DECISION · ABC · 123 · BUY · stage · 2024-01-02T23:30:00-05:00"])
def test_run_log_parser_reports_line_number(tmp_path, bad):
    path = tmp_path / "log.txt"
    path.write_text("\n" + bad, encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        scorecard.read_decisions(path)


def test_duplicate_run_log_ids_fail(tmp_path):
    path = tmp_path / "log.txt"
    line = "DECISION · ABC · 123 · pipeline · stage · 2024-01-02T23:30:00Z\n"
    path.write_text(line * 2, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate decision_id"):
        scorecard.read_decisions(path)


def test_csv_decision_prefix_does_not_trigger_text_detection(tmp_path):
    path = write_csv(tmp_path, [row("DECISION-001")])
    decision, = scorecard.read_decisions(path)
    assert decision.decision_id == "DECISION-001"


def test_run_log_cli_writes_settled_detail_and_owed_stays_unscored(tmp_path):
    path = tmp_path / "log.txt"
    path.write_text("DECISION · ABC · 123 · owed · obligation · 2024-01-02T23:30:00Z\n",
                    encoding="utf-8")
    out = tmp_path / "results"
    assert scorecard.main(["--decisions", str(path), "--out", str(out),
                           "--as-of", AS_OF.isoformat()],
                          provider({"ABC": sessions(), "SPY": [(DAY, 100)]})) == 0
    with (out / "scorecard_detail.csv").open(newline="", encoding="utf-8") as handle:
        detail = list(csv.DictReader(handle))
    assert len(detail) == 3 and all(d["status"] == "settled" for d in detail)
    assert all(d["hit"] == "" and float(d["excess_return"]) == pytest.approx(.2) for d in detail)
    assert all(d["decided_utc"] == "2024-01-02T23:30:00Z" for d in detail)


def test_out_inside_repo_refused_before_read_or_fetch(tmp_path, capsys):
    repo = Path(scorecard.__file__).resolve().parent.parent
    for out in (repo, repo / "private-results", repo / "code" / ".." / "results"):
        with pytest.raises(SystemExit) as exc:
            scorecard.main(["--decisions", "missing.csv", "--out", str(out)])
        assert exc.value.code == 2
        error = capsys.readouterr().err
        assert "portfolio data" in error and "git repo" in error
        assert not out.is_dir() or out == repo


def test_report_stays_under_30_lines_with_many_stages_and_five_misses(tmp_path):
    rows = [row(str(i), ticker=f"T{i}", disposition="discarded", stage_failed=f"stage {i}",
                superseded_by="next" if i % 2 else "") for i in range(50)]
    prices = {f"T{i}": sessions(final=110 + i) for i in range(50)}
    results = grade(tmp_path, rows, {**prices, "SPY": [(DAY, 100)]})
    report = scorecard.render_markdown(results, AS_OF)
    assert len(report.splitlines()) <= 30
    assert report.count("| Cohort |") == 3
    assert "stage 49" in report and "superseded" in report
    misses = report.split("5 biggest misses")[-1]
    assert misses.count("\n- ") == 5
    assert "T49" in misses and DAY.isoformat() in misses and "+59.0%" in misses


def test_missing_benchmark_is_no_data(tmp_path):
    results = grade(tmp_path, prices={"ABC": sessions()})
    assert all(g.status == "no_data" and "benchmark" in g.reason for g in results)


def test_provider_failures_remain_visible(tmp_path):
    decisions = scorecard.read_decisions(write_csv(tmp_path, [row()]))
    def broken(*args):
        raise OSError("offline")
    results = scorecard.grade_decisions(decisions, AS_OF, broken)
    assert all(g.status == "no_data" and "offline" in g.reason for g in results)


@pytest.mark.parametrize("multi", [False, True])
def test_default_provider_uses_adjusted_close_and_inclusive_end(monkeypatch, multi):
    calls = []
    columns = pd.MultiIndex.from_tuples([("ABC", "Close")]) if multi else ["Close"]
    frame = pd.DataFrame([[100.], [110.]], index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
                         columns=columns)
    def download(*args, **kwargs):
        calls.append((args, kwargs))
        return frame
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=download))
    assert scorecard.get_closes("ABC", DAY, DAY + timedelta(days=1)) == [
        (DAY, 100.), (DAY + timedelta(days=1), 110.)]
    assert calls[0][1]["auto_adjust"] is True
    assert calls[0][1]["end"] == "2024-01-04"


def test_yfinance_absence_has_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "yfinance", None)
    with pytest.raises(ImportError, match="yfinance.*requirements"):
        scorecard.get_closes("ABC", DAY, AS_OF)
