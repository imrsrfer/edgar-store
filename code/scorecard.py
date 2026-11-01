#!/usr/bin/env python3
"""Grade screener decisions against later dividend-adjusted market returns.

Input is a Decisions CSV or extracted DECISION lines; outputs must live outside
Git. Horizons count the stock's sessions AFTER its first post-decision close.
Returns are fractions in each instrument's own currency (no FX conversion).
Superseded decisions form a separate cohort. OWED returns are descriptive only.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import sys
from bisect import bisect_right
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, timedelta
from itertools import groupby
from pathlib import Path
from statistics import mean, median
from typing import Callable


HORIZONS = (63, 126, 252)
REQUIRED = frozenset(("decision_id", "decided_at", "ticker", "disposition"))
ALIASES = {"buy": "pipeline", "watch": "watchlist", "discard": "discarded",
           **{s: s for s in ("pipeline", "watchlist", "discarded", "owed")}}
Provider = Callable[[str, date, date], list[tuple[date, float]]]


@dataclass(frozen=True)
class Decision:
    decision_id: str
    decided_at: date
    ticker: str
    disposition: str
    benchmark: str = "SPY"
    run_id: str = ""
    cik: str = ""
    stage_failed: str = ""
    price_at_decision: float | None = None
    currency: str = ""
    superseded_by: str = ""
    decided_utc: str = ""

    @property
    def cohort(self):
        return "superseded" if self.superseded_by else "current"


@dataclass(frozen=True)
class Grade:
    decision: Decision
    horizon: int
    status: str
    reason: str = ""
    entry_date: date | None = None
    exit_date: date | None = None
    entry_close: float | None = None
    exit_close: float | None = None
    benchmark_entry_date: date | None = None
    benchmark_exit_date: date | None = None
    benchmark_entry_close: float | None = None
    benchmark_exit_close: float | None = None
    stock_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    hit: bool | None = None


@dataclass(frozen=True)
class Metrics:
    horizon: int
    cohort: str
    group_by: str
    group: str
    n: int
    settled: int
    pending: int
    no_data: int
    mean_excess: float | None
    median_excess: float | None
    hit_rate: float | None
    slugging: float | None


@dataclass(frozen=True)
class Prices:
    closes: tuple[tuple[date, float], ...] = ()
    error: str = ""


def parse_date(value, label="date"):
    """Require the CSV/CLI calendar-date contract, including zero padding."""
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(f"{label} must be a valid YYYY-MM-DD date: {value!r}") from None


def _symbol(value, label):
    if not re.fullmatch(r"[A-Za-z0-9^][A-Za-z0-9.^=_/-]*", value):
        raise ValueError(f"{label} must be a nonempty ticker symbol: {value!r}")
    return value.upper()


def _decision(row, benchmark):
    if not row.get("decision_id"):
        raise ValueError("decision_id is required and cannot be blank")
    disposition = ALIASES.get(row.get("disposition", "").lower())
    if disposition is None:
        raise ValueError("disposition must be pipeline/watchlist/discarded/owed "
                         "(aliases BUY/WATCH/DISCARD/OWED)")
    cik, currency = row.get("cik", ""), row.get("currency", "").upper()
    if cik and (not cik.isascii() or not cik.isdigit() or len(cik) > 10 or int(cik) == 0):
        raise ValueError(f"cik must contain 1-10 digits and be positive: {cik!r}")
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"currency must be a three-letter code: {currency!r}")
    price = row.get("price_at_decision", "")
    try:
        price = float(price) if price else None
        if price is not None and (not math.isfinite(price) or price <= 0):
            raise ValueError
    except ValueError:
        raise ValueError("price_at_decision must be blank or a finite positive number") from None
    optional = {key: row.get(key, "") for key in
                ("run_id", "stage_failed", "superseded_by", "decided_utc")}
    return Decision(row["decision_id"], parse_date(row.get("decided_at", ""), "decided_at"),
                    _symbol(row.get("ticker", ""), "ticker"), disposition,
                    _symbol(row.get("benchmark") or benchmark, "benchmark"),
                    cik=cik, currency=currency, price_at_decision=price, **optional)


def _utc_stamp(value):
    pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)"
    if not re.fullmatch(pattern, value):
        raise ValueError("decided UTC must be an ISO timestamp with Z or +00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid decided UTC timestamp: {value!r}") from None


def _line_rows(text):
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = tuple(part.strip() for part in line.split("·"))
        try:
            if len(parts) != 6 or parts[0] != "DECISION":
                raise ValueError("expected DECISION · ticker · cik · disposition · stage_failed · decided UTC ISO")
            _, ticker, cik, disposition, stage, stamp = parts
            instant = _utc_stamp(stamp)
            ticker = _symbol(ticker, "ticker")
            # Canonical UTC spelling prevents Z/+00:00 from evading ID uniqueness.
            canonical = instant.isoformat().replace("+00:00", "Z")
            yield number, {"decision_id": f"{canonical}|{ticker}", "ticker": ticker,
                           "cik": cik, "disposition": disposition, "stage_failed": stage,
                           "decided_at": instant.date().isoformat(), "decided_utc": canonical}
        except ValueError as exc:
            raise ValueError(f"line {number}: {exc}") from None


def _csv_rows(text):
    reader = csv.DictReader(io.StringIO(text), strict=True)
    columns = reader.fieldnames or []
    missing = sorted(REQUIRED - set(columns))
    if missing:
        raise ValueError("missing required columns: " + ", ".join(missing))
    if len(columns) != len(set(columns)):
        raise ValueError("duplicate CSV column names")
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"line {reader.line_num}: wrong number of CSV fields")
        yield reader.line_num, {key: value.strip() for key, value in row.items()}


def read_decisions(path, benchmark="SPY"):
    """Validate the entire input before any network call or output is made."""
    benchmark = _symbol(benchmark.strip(), "benchmark")
    text = Path(path).read_text(encoding="utf-8-sig")
    first = next((line.lstrip() for line in text.splitlines() if line.strip()), "")
    is_log = bool(re.match(r"^DECISION(?:\s|·|$)", first))
    rows = _line_rows(text) if is_log else _csv_rows(text)
    decisions, seen = [], set()
    try:
        for number, row in rows:
            try:
                decision = _decision(row, benchmark)
                if decision.decision_id in seen:
                    raise ValueError(f"duplicate decision_id: {decision.decision_id}")
                decisions.append(decision)
                seen.add(decision.decision_id)
            except ValueError as exc:
                raise ValueError(f"line {number}: {exc}") from None
    except csv.Error as exc:
        raise ValueError(f"invalid CSV: {exc}") from None
    return tuple(decisions)


def get_closes(symbol, start, end):
    """Inclusive dates; adjusted Close includes splits and dividend reinvestment.

    Like build_prices, import yfinance only at the network seam and use its
    ticker-grouped download shape helper. Today's potentially live bar is
    excluded conservatively; historical --as-of dates are inclusive.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance is required for prices; install code/requirements.txt") from None
    from build_prices import _closes_for

    until = min(end + timedelta(days=1), date.today())
    if start >= until:
        return []
    frame = yf.download([symbol], start=start.isoformat(), end=until.isoformat(),
                        interval="1d", auto_adjust=True, progress=False,
                        threads=True, group_by="ticker")
    closes = _closes_for(frame, symbol)
    if closes is None:
        return []
    return [(stamp.date(), float(value)) for stamp, value in closes.items()]


def _fetch(provider, symbol, start, end):
    try:
        values = tuple((day, float(value)) for day, value in provider(symbol, start, end)
                       if start <= day <= end)
        if any(not math.isfinite(value) or value <= 0 for _, value in values):
            raise ValueError("adjusted closes must be finite and positive")
        if len({day for day, _ in values}) != len(values):
            raise ValueError("duplicate close dates")
        return Prices(tuple(sorted(values)))
    except ImportError:
        raise  # A missing runtime dependency is a setup error, not ticker coverage.
    except Exception as exc:  # Provider failures stay visible in no_data detail.
        return Prices(error=f"{symbol}: {type(exc).__name__}: {exc}")


def _at_or_before(closes, day):
    index = bisect_right(closes, day, key=lambda item: item[0]) - 1
    return closes[index] if index >= 0 else None


def _settled(result, benchmark):
    entry = _at_or_before(benchmark.closes, result.entry_date)
    exit_ = _at_or_before(benchmark.closes, result.exit_date)
    if benchmark.error or entry is None or exit_ is None:
        return replace(result, status="no_data", reason="benchmark: " + (
            benchmark.error or "no close on or before entry/exit"))
    stock_return = result.exit_close / result.entry_close - 1
    benchmark_return = exit_[1] / entry[1] - 1
    excess = stock_return - benchmark_return
    disposition = result.decision.disposition
    hit = None if disposition == "owed" else excess < 0 if disposition == "discarded" else excess > 0
    return replace(result, status="settled", benchmark_entry_date=entry[0],
                   benchmark_exit_date=exit_[0], benchmark_entry_close=entry[1],
                   benchmark_exit_close=exit_[1], stock_return=stock_return,
                   benchmark_return=benchmark_return, excess_return=excess, hit=hit)


def _grade(decision, horizon, as_of, stock, benchmark):
    result = Grade(decision, horizon, "pending")
    if decision.decided_at >= as_of:
        return replace(result, reason="awaiting first post-decision close")
    if stock.error or not stock.closes:
        return replace(result, status="no_data", reason=stock.error or "no stock closes")
    closes = tuple(bar for bar in stock.closes if bar[0] > decision.decided_at)
    if not closes:
        return replace(result, reason="awaiting first post-decision close")
    result = replace(result, entry_date=closes[0][0], entry_close=closes[0][1])
    if len(closes) <= horizon:
        return replace(result, reason=f"only {len(closes) - 1}/{horizon} sessions after entry")
    result = replace(result, exit_date=closes[horizon][0], exit_close=closes[horizon][1])
    return _settled(result, benchmark)


def grade_decisions(decisions, as_of, provider=None):
    """Return every decision/horizon, including unpriced and superseded calls."""
    provider = provider or get_closes
    eligible = tuple(d for d in decisions if d.decided_at < as_of)
    if not eligible:
        return tuple(Grade(d, h, "pending", "awaiting first post-decision close")
                     for d in decisions for h in HORIZONS)
    start = min(d.decided_at for d in eligible)
    benchmarks = frozenset(d.benchmark for d in eligible)
    symbols = benchmarks | {d.ticker for d in eligible}
    # Full benchmark history avoids an arbitrary holiday lookback truncating
    # the last available close before entry. Each symbol is fetched only once.
    prices = {s: _fetch(provider, s, date(1900, 1, 1) if s in benchmarks else start, as_of)
              for s in sorted(symbols)}
    return tuple(_grade(d, h, as_of, prices.get(d.ticker, Prices()),
                        prices.get(d.benchmark, Prices())) for d in decisions for h in HORIZONS)


def _metrics(key, group_by, grades):
    horizon, cohort, name = key
    excess = tuple(g.excess_return for g in grades if g.status == "settled")
    scored = tuple(g for g in grades if g.hit is not None)
    wins = tuple(abs(g.excess_return) for g in scored if g.hit)
    losses = tuple(abs(g.excess_return) for g in scored if not g.hit)
    slugging = mean(wins) / mean(losses) if wins and losses and sum(losses) > 0 else None
    return Metrics(horizon, cohort, group_by, name, len(grades),
                   sum(g.status == "settled" for g in grades),
                   sum(g.status == "pending" for g in grades),
                   sum(g.status == "no_data" for g in grades),
                   mean(excess) if excess else None, median(excess) if excess else None,
                   len(wins) / len(scored) if scored else None, slugging)


def aggregate(grades, group_by):
    """All status counts; settled returns only; OWED excluded from scoring.

    Slugging uses direction-aware winning/losing excess magnitudes. Zero excess
    is a miss. A missing side or an all-zero losing side yields undefined (—).
    """
    if group_by not in ("disposition", "stage_failed"):
        raise ValueError("group_by must be disposition or stage_failed")
    def key(grade):
        return (grade.horizon, grade.decision.cohort,
                getattr(grade.decision, group_by) or "(none)")
    return tuple(_metrics(k, group_by, tuple(values))
                 for k, values in groupby(sorted(grades, key=key), key=key))


def _percent(value):
    return "—" if value is None else f"{value:+.1%}"


def _escape(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _metric_text(metric):
    hit = "—" if metric.hit_rate is None else f"{metric.hit_rate:.0%}"
    slug = "—" if metric.slugging is None else f"{metric.slugging:.2f}"
    return (f"{_escape(metric.group)}: {metric.n}/{metric.settled}/{metric.pending}/{metric.no_data}; "
            f"{_percent(metric.mean_excess)}/{_percent(metric.median_excess)}; {hit}/{slug}")


def _misses(grades):
    # One worst horizon per decision, then the five largest adverse excesses.
    scored = (g for g in grades if g.hit is False)
    ordered = sorted(scored, key=lambda g: (-abs(g.excess_return), -g.horizon,
                                            g.decision.decision_id))
    unique = dict.fromkeys(g.decision.decision_id for g in ordered)
    return tuple(next(g for g in ordered if g.decision.decision_id == key) for key in tuple(unique)[:5])


def render_markdown(grades, as_of):
    """At most 30 lines, with exhaustive groups packed into four rows/horizon."""
    decisions = {g.decision.decision_id: g.decision for g in grades}
    superseded = sum(bool(d.superseded_by) for d in decisions.values())
    counts = ", ".join(f"{s}={sum(g.status == s for g in grades)}"
                       for s in ("settled", "pending", "no_data"))
    lines = [f"# Scorecard — as-of {as_of.isoformat()}",
             f"Decisions={len(decisions)} (current={len(decisions) - superseded}, superseded={superseded}); decision-windows: {counts}.",
             "Groups: n/settled/pending/no_data; mean/median excess; hit/slugging (direction-aware; OWED unscored; — undefined)."]
    metrics = aggregate(grades, "disposition") + aggregate(grades, "stage_failed")
    for horizon in HORIZONS:
        lines.extend((f"## {horizon} trading days", "| Cohort | Grouped by | Metrics |", "|---|---|---|"))
        for cohort in ("current", "superseded"):
            for dimension in ("disposition", "stage_failed"):
                cell = "; ".join(_metric_text(m) for m in metrics
                                 if (m.horizon, m.cohort, m.group_by) == (horizon, cohort, dimension))
                lines.append(f"| {cohort} | {dimension} | {cell or '—'} |")
    lines.append("## 5 biggest misses (one worst horizon per decision)")
    misses = _misses(grades)
    lines.extend(f"- {_escape(g.decision.ticker)} {g.decision.decided_at}: {_percent(g.excess_return)} excess "
                 f"({g.horizon}d, {g.decision.disposition}, {g.decision.cohort})" for g in misses)
    if not misses:
        lines.append("- None settled.")
    return "\n".join(lines) + "\n"


def _repo_ancestor(path):
    return next((p for p in (path, *path.parents) if (p / ".git").exists()), None)


def validate_out(out):
    """Resolve symlinks/junctions before rejecting any repository destination."""
    path = Path(out).expanduser().resolve()
    source_repo = _repo_ancestor(Path(__file__).resolve().parent)
    if _repo_ancestor(path) or source_repo and path.is_relative_to(source_repo):
        raise ValueError("--out must be outside the git repo: outputs contain portfolio data "
                         "and must never be committed")
    for name in ("scorecard.md", "scorecard_detail.csv"):
        target = path / name
        if target.is_symlink() or target.exists() and target.stat().st_nlink > 1:
            raise ValueError(f"output file must not be a link: {target}")
    return path


def _detail_row(grade, as_of):
    record = asdict(grade)
    return {**record.pop("decision"), "cohort": grade.decision.cohort,
            "as_of": as_of, **record}


def write_outputs(out, grades, as_of):
    out = validate_out(out)
    out.mkdir(parents=True, exist_ok=True)
    columns = ([f.name for f in fields(Decision)] + ["cohort", "as_of"]
               + [f.name for f in fields(Grade) if f.name != "decision"])
    with (out / "scorecard_detail.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(_detail_row(g, as_of) for g in grades)
    (out / "scorecard.md").write_text(render_markdown(grades, as_of), encoding="utf-8")


def main(argv=None, provider=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", required=True, help="Decisions CSV or DECISION lines")
    parser.add_argument("--out", required=True, help="output directory outside Git")
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--as-of", default=date.today().isoformat())
    args = parser.parse_args(argv)
    try:
        out = validate_out(args.out)
        as_of = parse_date(args.as_of, "--as-of")
        decisions = read_decisions(args.decisions, args.benchmark)
        grades = grade_decisions(decisions, as_of, provider)
        write_outputs(out, grades, as_of)
    except (ValueError, OSError, ImportError) as exc:
        parser.error(str(exc))
    print(f"{len(decisions)} decisions, {len(grades)} windows; wrote scorecard to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
