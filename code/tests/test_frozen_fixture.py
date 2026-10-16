"""Frozen, network-free Gate 0 regression on the auditor's failure-mode filers.

Audit 2026-10-15 #12 (auditor recommendation 2026-10-16). These tests read only
tests/fixtures/store -- never the live store -- so they run in CI and block a
merge. Every expected value below was verified by hand against the audit,
the independent verification or the filing, and is pinned HERE. It is never
regenerated from the implementation: if the code changes one of these, the
test fails and a human decides whether the code or the expectation is wrong.
"""
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

import gate0
import screen

FIXTURE = Path(__file__).resolve().parent / "fixtures"
STORE = FIXTURE / "store"


def test_fixture_hashes_are_pinned():
    """A changed fixture file must be a reviewed decision, not a drift."""
    for line in (FIXTURE / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split()
        actual = hashlib.sha256((STORE / name).read_bytes()).hexdigest()
        assert actual == digest, f"{name} changed; review it and update SHA256SUMS by hand"


@pytest.fixture(scope="module")
def fixture_gate0(tmp_path_factory):
    """Run the real gate0 entry point on a disposable copy of the fixture."""
    root = tmp_path_factory.mktemp("frozen_store")
    for name in ("facts.parquet", "meta.parquet"):
        shutil.copyfile(STORE / name, root / name)
    assert gate0.main(["--root", str(root)]) == 0
    return pl.read_csv(root / "gate0.csv", infer_schema_length=10000)


def row(frame, ticker):
    hit = frame.filter(pl.col("ticker") == ticker)
    assert hit.height == 1, f"{ticker}: expected exactly one row, got {hit.height}"
    return hit.to_dicts()[0]


def test_screened_population_is_exactly_the_nonfinancial_survivors(fixture_gate0):
    # PNFP, CLBK (banks) and AIV (REIT, SIC 6798) are excluded as financials; the
    # XOM holdco 2115436 is merged away. Audit #5, and the 6073f42 SIC carry-over fix.
    assert sorted(fixture_gate0["ticker"].to_list()) == sorted(
        ["XOM", "CNXC", "WILCF", "SGHC", "DIDIY", "SELXF", "SHOP", "ZIM"]
    )
    assert 2115436 not in fixture_gate0["cik"].to_list()


def test_xom_successor_merge(fixture_gate0):
    # Audit #5: one row on CIK 34088 with the ticker and the sibling's SIC.
    # Hand-verified FY2025 10-K: OCF $51,970M, capex $28,358M.
    r = row(fixture_gate0, "XOM")
    assert (r["cik"], str(r["sic"]), r["latest_fiscal_year"], r["gate0_status"]) == (
        34088, "2911", 2025, "pass")
    assert r["fcf"] == pytest.approx((51_970 - 28_358) * 1e6, rel=1e-6)


def test_cnxc_fiscal_label_fix(fixture_gate0):
    # Audit #12 / CNXC labelling bug: back to FY2025, tangible book negative.
    r = row(fixture_gate0, "CNXC")
    assert (r["latest_fiscal_year"], r["gate0_status"]) == (2025, "fail")
    assert r["tangible_book"] == pytest.approx(-2_887_739_000, rel=1e-6)


@pytest.mark.parametrize("ticker,currency,status", [
    ("WILCF", "ILS", "unknown"),   # audit #1: ILS OCF never combined with USD NI
    ("SGHC", "USD", "unknown"),    # audit #1: EUR flows, USD balance sheet
    ("DIDIY", "CNY", "pass"),      # verification V1: was mislabelled USD
    ("SELXF", "TWD", "unknown"),   # auditor 2026-10-16: diagnostics no longer vote
])
def test_currency_cases(fixture_gate0, ticker, currency, status):
    r = row(fixture_gate0, ticker)
    assert (r["reporting_currency"], r["gate0_status"]) == (currency, status)
    assert r["currency_unresolved"] is False


def test_shop_split_guard_nulls_the_five_year_per_share_cagr(fixture_gate0):
    # Audit #2: diluted shares move >3x across the 5y endpoints (split artifact).
    assert row(fixture_gate0, "SHOP")["fcf_per_share_cagr_5y"] is None


def test_zim_lease_upper_bound_flag_without_verdict_change(fixture_gate0):
    # Lease forensics 2026-10-16: CashOutflowForLeases FY2025 $1,889.3M vs OCF
    # $2,299.5M fires lease_heavy via the proxy; Fer ruled flag-only.
    r = row(fixture_gate0, "ZIM")
    assert r["gate0_status"] == "pass"
    assert r["lease_heavy"] is True and r["lease_heavy_upper_bound"] is True
    assert r["lease_outflow_total"] == pytest.approx(1_889_300_000, rel=1e-6)


def test_every_lane_runs_with_eps_input_and_conserves_rows(fixture_gate0):
    """Audit #8 (--eps-csv) and #4 (conservation): every lane, EPS joined."""
    eps = pl.DataFrame({"ticker": ["XOM", "ZIM"], "fwd_pe": [10.0, 3.0], "trail_pe": [12.0, 4.0]})
    frame = fixture_gate0.with_columns(pl.col("market_cap").cast(pl.Float64, strict=False))
    for lane in screen.LANES:
        args = SimpleNamespace(lane=lane, include_financials=False,
                               exclude_sic=gate0.DEFAULT_EXCLUDE_SIC, min_revenue=0,
                               min_mktcap=0, max_mktcap=float("inf"), momentum="flag")
        result, _ = screen.run_screen(frame, args, set(), None, eps)
        assert result["cik"].n_unique() == result.height, lane
