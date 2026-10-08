#!/usr/bin/env python3
"""Deterministic store pulls for the screening-analyst agent. Standard library only.

    python .claude/screening/fact_pack.py store
    python .claude/screening/fact_pack.py rows AMPH LQDT
    python .claude/screening/fact_pack.py rows AMPH --quote AMPH=27.10,1.31e9

`store` answers Step 0 of the Opportunity Screener job: store age, the five
column checks, and whether the helper scripts the job falls back on exist.

`rows` prints one JSON fact pack per ticker straight from gate0.csv, grouped
the way the decider reads a row. With --quote (price, market cap from a LIVE
source) it also does the share-count check and the live P/FCF arithmetic.

It reads, it never writes, and it never decides. Three conventions, same as
the store's own:

- a value the CSV leaves empty is null, never zero, and nothing computed from
  it is published;
- a column missing from gate0.csv is reported as "ABSENT" -- the store
  predates it, which is a different finding from a null;
- every mechanical OPUS-ONLY trigger is true / false / "NOT_MEASURED". A test
  whose input is absent is not a pass.
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ABSENT = "ABSENT"
NOT_MEASURED = "NOT_MEASURED"

# Job definition, Step 0. column -> the store build it postdates.
COLUMN_CHECKS = {
    "capex_suspect": "2026-08-19",
    "ttm_unavailable": "2026-08-27",
    "investing_unreconciled": "2026-09-09",
    "store_defect_flags": "2026-10-01",
    "net_income_suspect": "2026-09-21",
}
FRESH_DAYS = 21
SHARE_GAP_LIMIT = 0.25
FOREIGN_FORMS = {"20-F", "20-F/A", "6-K", "40-F", "40-F/A"}

GROUPS = {
    "identity": [
        "ticker", "cik", "company_name", "exchange", "sic", "sic_description",
        "taxonomy", "filing_form", "reporting_currency", "fiscal_year_end",
        "latest_fiscal_year", "period_end", "latest_q_period_end", "sec_download_date",
    ],
    "gate0": [
        "gate0_pass", "gate0_framework_pass", "framework_leg_failed", "gate0_status",
        "gate0_not_evaluable", "imputed_fields", "test_tangible_book",
        "test_income_quality", "test_fcf", "test_fcf_after_sbc", "test_sbc",
        "test_ni_vs_oi", "test_tax_anomaly", "tangible_book_basis",
        "tangible_book_vintage_conflict",
    ],
    "flags": [
        "warn_inorganic", "capex_suspect", "capex_broken", "investing_unreconciled",
        "ttm_fcf_divergence", "ttm_suspect", "ttm_unavailable", "ttm_stale_concepts",
        "ttm_window_misaligned", "shares_scale_suspect", "income_quality_suspect",
        "net_income_suspect", "lease_unmeasured", "lease_heavy", "sbc_unverified",
        "sbc_window_lost", "sbc_assumed_zero", "ttm_capex_negative", "ttm_capex_missing",
        "ttm_sbc_window_lost", "ttm_sbc_assumed_zero", "ttm_sbc_evidence_conflict",
        "store_defect_flags", "total_debt_partial", "capex_gross_of_proceeds",
        "shares_history_scale_break", "fy_flows_stale", "ttm_behind_calendar",
        "ttm_side_flows_lagging",
    ],
    "residuals": ["investing_residual", "net_income_residual", "capex_vs_dep_amort"],
    "fy_flows": [
        "revenue", "operating_income", "pretax_income", "tax_expense", "net_income",
        "ocf", "capex", "capex_net", "ppe_sale_proceeds", "sbc", "lease_payments",
        "acquisitions", "buybacks", "dividends", "dep_amort", "investing_cf",
        "fcf", "fcf_after_sbc", "fy_flows_period_end",
    ],
    "ttm": [
        "ttm_window_start", "ttm_window_end", "ttm_revenue", "ttm_net_income",
        "ttm_ocf", "ttm_capex", "ttm_sbc", "ttm_lease_payments", "ttm_fcf_after_sbc",
        "ttm_acquisitions", "ttm_buybacks", "ttm_dividends",
    ],
    "balance": [
        "equity", "goodwill", "intangibles", "tangible_book", "tangible_book_latest_q",
        "cash", "total_debt", "net_cash", "latest_q_equity", "latest_q_cash",
        "latest_q_total_debt",
    ],
    "shares": ["shares_diluted", "latest_q_shares_diluted", "fcf_per_share"],
    "ratios": [
        "income_quality", "income_quality_3y_avg", "income_quality_direction",
        "sbc_pct_revenue", "effective_tax", "ni_vs_oi", "net_margin",
        "operating_margin_latest", "operating_margin_2y_ago", "operating_margin_5y_ago",
        "operating_margin_delta", "acq_intensity", "bs_acq_intensity",
        "acq_cf_bs_disagreement", "buyback_pct_fcf",
    ],
    "growth": [
        "growth_basis", "short_history", "revenue_cagr_3y", "revenue_cagr_5y",
        "fcf_per_share_cagr_3y", "fcf_per_share_cagr_5y", "fcf_inflection",
        "quarters_of_accelerating_revenue", "tangible_book_yrs_negative",
    ],
    "store_valuation": [
        "market_cap", "ev", "pct_vs_200ma", "p_fcf_after_sbc", "ev_fcf_after_sbc",
    ],
    "source_tags": [
        "source_tag_capex", "source_tag_net_income", "source_tag_ocf", "source_tag_sbc",
        "source_tag_equity", "source_tag_goodwill", "source_tag_intangibles",
    ],
}


def parse(raw):
    """CSV text -> None / bool / float / str. Empty is null, never zero."""
    if raw is None or raw == "":
        return None
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return float(raw)
    except ValueError:
        return raw


def read_gate0(root):
    path = root / "gate0.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def store_brief(repo, root):
    manifest_path = repo / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    built = manifest.get("store_built_utc_newest")
    age_days = None
    if built:
        built_at = datetime.fromisoformat(built.replace("Z", "+00:00"))
        age_days = round((datetime.now(timezone.utc) - built_at).total_seconds() / 86400, 1)

    columns, rows = read_gate0(root)
    checks = {}
    for column, postdates in COLUMN_CHECKS.items():
        if column not in columns:
            checks[column] = {"present": False, "store_predates": postdates}
            continue
        non_null = sum(1 for row in rows if row[column] != "")
        checks[column] = {"present": True, "non_null": non_null}
        if non_null == 0:
            # Present and uniformly null is NOT MEASURED, not "nothing fired".
            checks[column]["uniformly_null"] = True

    failed = [c for c, v in checks.items() if not v["present"] or v.get("uniformly_null")]
    return {
        "store_built_utc_newest": built,
        "store_age_days": age_days,
        "store_fresh": None if age_days is None else age_days <= FRESH_DAYS,
        "store_vintage_ok": manifest.get("store_vintage_ok"),
        "gate0_rows": len(rows),
        "column_checks": checks,
        "column_checks_failed": failed,
        "store_defects_py_present": (repo / "code" / "store_defects.py").exists(),
        "rank_queue_py_present": (repo / "code" / "rank_queue.py").exists(),
    }


def opus_only_triggers(values):
    """The OPUS-ONLY bullets the store can settle. The rest are named as owed."""

    def flag(name):
        value = values.get(name, ABSENT)
        return NOT_MEASURED if value in (ABSENT, None) else bool(value)

    sic = values.get("sic")
    form = values.get("filing_form") or ""
    foreign = form in FOREIGN_FORMS or values.get("taxonomy") == "ifrs-full"
    ttm_unavailable = flag("ttm_unavailable")
    return {
        "financial_sic_6000_6799": NOT_MEASURED if sic in (None, ABSENT) else 6000 <= sic <= 6799,
        "foreign_filer": foreign,
        "foreign_and_ttm_unavailable": (
            NOT_MEASURED if foreign and ttm_unavailable == NOT_MEASURED
            else bool(foreign and ttm_unavailable is True)
        ),
        "shares_scale_suspect": flag("shares_scale_suspect"),
        "shares_history_scale_break": flag("shares_history_scale_break"),
        "net_income_suspect": flag("net_income_suspect"),
        "not_settled_by_store": [
            "foreign vintage stale (TME/DIDIY/EDU shape)",
            "spin-off / recent IPO / re-IPO / re-domiciliation",
            "named per-share warning (NVMI/CAMT/RGEN shape)",
            "close call on MOS or a judgement-based Gate 0",
        ],
    }


def live_checks(values, price, market_cap):
    """Share-count check and P/FCF at a LIVE cap. Never derives a cap from shares."""
    implied = market_cap / price if price else None
    out = {"live_price": price, "live_market_cap": market_cap, "implied_shares": implied}
    for column in ("shares_diluted", "latest_q_shares_diluted"):
        store = values.get(column)
        if isinstance(store, float) and store > 0 and implied:
            out[f"gap_vs_{column}"] = round(implied / store - 1, 4)
    gaps = [abs(v) for k, v in out.items() if k.startswith("gap_vs_")]
    out["share_gap_over_25pct"] = (max(gaps) > SHARE_GAP_LIMIT) if gaps else NOT_MEASURED
    for column in ("fcf_after_sbc", "ttm_fcf_after_sbc"):
        flow = values.get(column)
        if isinstance(flow, float) and flow > 0:
            out[f"p_{column}_live"] = round(market_cap / flow, 2)
        else:
            out[f"p_{column}_live"] = None  # null or non-positive: no multiple
    return out


def row_pack(columns, row, quote):
    values = {c: (parse(row[c]) if c in columns else ABSENT) for g in GROUPS.values() for c in g}
    pack = {group: {c: values[c] for c in cols} for group, cols in GROUPS.items()}
    pack["opus_only_mechanical"] = opus_only_triggers(values)
    if quote:
        pack["live"] = live_checks(values, *quote)
    return pack


def parse_quotes(items):
    quotes = {}
    for item in items or []:
        ticker, _, nums = item.partition("=")
        price, _, cap = nums.partition(",")
        quotes[ticker.upper()] = (float(price), float(cap))
    return quotes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="edgar-store checkout (default: cwd)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("store")
    rows_cmd = sub.add_parser("rows")
    rows_cmd.add_argument("tickers", nargs="+")
    rows_cmd.add_argument(
        "--quote", action="append", metavar="TICKER=PRICE,MARKET_CAP",
        help="live price and market cap from a tier-1 source; repeatable",
    )
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    root = repo / "store"
    if args.cmd == "store":
        print(json.dumps(store_brief(repo, root), indent=1))
        return 0

    columns, rows = read_gate0(root)
    quotes = parse_quotes(args.quote)
    result = {}
    for ticker in (t.upper() for t in args.tickers):
        matches = [r for r in rows if r["ticker"].upper() == ticker]
        if not matches:
            result[ticker] = {"error": "ticker not in gate0.csv -- resolve by CIK, never by name"}
            continue
        packs = [row_pack(columns, r, quotes.get(ticker)) for r in matches]
        result[ticker] = packs[0] if len(packs) == 1 else {"multiple_rows": packs}
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
