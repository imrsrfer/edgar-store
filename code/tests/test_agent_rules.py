"""Offline policy contracts: regression checks per audit finding, no model calls."""

from pathlib import Path
import re

import pytest


AGENTS = Path(__file__).resolve().parents[2] / ".claude" / "agents"

# The publication gate runs this suite from the local code root, which has no
# .claude/ tree; the agent contracts are enforced where the agents live.
pytestmark = pytest.mark.skipif(not AGENTS.is_dir(), reason="no .claude/agents beside this code tree")


def text(agent):
    content = (AGENTS / f"screening-{agent}.md").read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", content.replace("**", ""))


def test_f08_compounder_exception_preserves_tier2_floor():
    decider = text("decider")
    assert "Tier 1 normally needs at least 20% base-case MOS" in decider
    assert "Tier 1 only from 20% to 15%" in decider
    assert "base-case FCF/share CAGR above 15%" in decider
    assert "Tier 2 always needs at least 30% MOS" in decider
    assert "the exception never lowers its floor" in decider


def test_f12_bulk_cache_is_triage_only_with_per_figure_staleness_labels():
    analyst = text("analyst")
    prices = analyst.split("### `PRICES`", 1)[1].split("### `SWEEP`", 1)[0]
    assert "7 days old or less" in prices
    assert "bulk SWEEP triage only" in prices
    assert "each figure older than 24h" in prices
    assert "`STALE >24h`, with its own as-of timestamp and source" in prices
    assert "live pull this run" in prices
    assert "Pull these live this run for every decided row" in analyst
    assert "Rows older than that, and tickers it lacks, are re-priced" in prices


def test_f17_unlisted_tier1_requires_complete_five_year_cash_and_revenue_evidence():
    decider = text("decider")
    assert "Unlisted business types default to Tier 2" in decider
    assert "FCF is positive in every one of the last 5 FY" in decider
    assert "at most 1 of those 5 FY" in decider
    assert "prior-year comparatives needed to measure them" in decider
    assert "with incomplete history, use Tier 2" in decider
    assert "The named Tier 2 categories remain Tier 2" in decider


def test_f21_every_buy_logs_both_liquidity_numbers_and_thresholds():
    decider = text("decider")
    assert "Every buy recommendation logs both §F-2 liquidity numbers" in decider
    assert "full resulting position value (including an add)" in decider
    assert "last 30 trading days" in decider
    assert "position percent of ADV = 100 × position value / 30-day ADV" in decider
    assert "liquidation days at 10% of ADV = position value / (0.10 × 30-day ADV)" in decider
    assert "at most 5% and liquidation within 5 trading days" in decider
    assert "Missing inputs leave liquidity OWED and block the buy" in decider
    assert "Recheck both at each re-underwrite" in decider
    assert "a later breach caps adds and never forces a sale" in decider


@pytest.mark.parametrize("agent", ["analyst", "decider"])
def test_f22_each_missing_gate0_leg_blocks_past_watchlist(agent):
    rules = text(agent)
    assert "NOT_EVALUABLE" in rules
    assert "blocks advancement past WATCHLIST" in rules
    assert "organic/acquired growth" in rules
    assert "SBC dilution" in rules
    assert "adjusted-vs-GAAP reconciliation" in rules
    assert "Withhold Gate 0 clearance" in rules or "withhold Gate 0 clearance" in rules
    assert "each missing or undisclosed input" in rules or "missing or undisclosed inputs" in rules.lower()


def test_fact_pack_carries_dilution_bridge_and_section_c1_reconciliation_owed():
    analyst = text("analyst")
    output = analyst.split("## Return format", 1)[1]
    assert "Dilution:" in output
    assert "SBC / acquisition / other issuance / repurchases" in output
    assert "annual SBC-driven net dilution" in output
    assert "adjusted-vs-GAAP §C-1 reconciliation:" in output
    assert "dilution_yoy" in analyst and "dilution_cagr_3y" in analyst
    assert "2.5%/yr fails Gate 0" in analyst
    assert "first Gate 0 pass on or after 2026-10-01" in analyst
    assert "same-period reconciliation, itemized add-backs + nature" in output
    assert "logged acceptance, or NOT_EVALUABLE · OWED" in output
    assert "Blocking: <every NOT_EVALUABLE Gate 0 leg" in output
    assert "§C-1 tests what is added back; a gap percentage alone cannot clear it" in analyst


def test_f18_management_floor_is_a_post_gate0_veto_only():
    decider = text("decider")
    assert "fully scored total below 12/25, or any sub-score of 0, blocks advancement past WATCHLIST" in decider
    assert "only at the six-screen stage, after Gate 0" in decider
    assert "It is a veto, never a qualifier" in decider
    assert "never ranks, promotes, offsets a failed screen or raises a tier" in decider
