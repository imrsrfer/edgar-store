#!/usr/bin/env python3
"""
Verify the 2026-09-08 anomaly-fix patch against a real store.

Usage:
    python3 verify_fixes.py --before gate0_before.csv --after gate0_after.csv

INVARIANTS are asserted and will fail the script. They must hold on ANY store.
COUNTS are reported, not asserted, because they depend on which store you ran
against. The reference figures from the 2026-08-30 store are printed alongside
for comparison ONLY -- a difference is information, not a failure.

That split is deliberate. A check that asserts a store-dependent number fails
for the wrong reason on a newer store, and a run that learns to ignore a red
result has stopped verifying anything.
"""
import argparse, csv, sys

REFERENCE = {  # measured on the 2026-08-30 store, 6,031 rows
    "rows": 6031, "gate0_pass_true": 952, "framework_false": 200,
    "stale_rows": 1818, "withdrawn": 2811, "shares_scale": 53,
    "shares_scale_with_fps": 33, "fps_5y_ago": 2764,
}
TTM_FLOWS = ["ttm_revenue", "ttm_net_income", "ttm_ocf", "ttm_capex", "ttm_sbc",
             "ttm_acquisitions", "ttm_buybacks", "ttm_dividends", "ttm_dep_amort",
             "ttm_operating_income", "ttm_pretax_income", "ttm_tax_expense"]
NEW_COLS = ["ttm_stale_concepts", "gate0_framework_pass", "framework_leg_failed",
            "shares_scale_suspect", "fcf_per_share_3y_ago", "fcf_per_share_5y_ago"]

fails, notes = [], []
def inv(cond, msg):
    (notes if cond else fails).append(("OK  " if cond else "FAIL") + "  " + msg)

def load(p):
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def key(r): return (r.get("cik", ""), r.get("ticker", ""))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True, help="gate0.csv produced BEFORE the patch")
    ap.add_argument("--after", required=True, help="gate0.csv produced AFTER the patch")
    a = ap.parse_args()
    before, after = load(a.before), load(a.after)
    bmap = {key(r): r for r in before}

    # ---------------- INVARIANTS ----------------
    inv(len(before) == len(after),
        f"row count unchanged ({len(before)} -> {len(after)})")

    missing = [c for c in NEW_COLS if after and c not in after[0]]
    inv(not missing, f"all six new columns present{'' if not missing else ' -- MISSING: ' + ','.join(missing)}")

    changed = [r for r in after
               if key(r) in bmap and bmap[key(r)].get("gate0_pass") != r.get("gate0_pass")]
    inv(not changed,
        f"gate0_pass unchanged on every row (changed on {len(changed)}"
        + (f": {[c.get('ticker') for c in changed[:5]]}" if changed else "") + ")")

    # every withdrawn ttm_* value must be NAMED in ttm_stale_concepts on that row.
    # This is the check that a botched apply fails: silent nulling is the defect.
    unnamed, withdrawn = [], 0
    for r in after:
        b = bmap.get(key(r))
        if not b:
            continue
        named = set(x for x in (r.get("ttm_stale_concepts") or "").split(",") if x)
        for col in TTM_FLOWS:
            if b.get(col) and not r.get(col):
                withdrawn += 1
                if col[len("ttm_"):] not in named:
                    unnamed.append((r.get("ticker"), col))
    inv(not unnamed,
        f"every withdrawn ttm_* value is named in ttm_stale_concepts "
        f"({withdrawn} withdrawn, {len(unnamed)} unnamed"
        + (f", e.g. {unnamed[:3]}" if unnamed else "") + ")")

    # the SIC 6000-6799 exemption the code's own docstring names must be honoured
    leaked = [r.get("ticker") for r in after
              if r.get("framework_leg_failed")
              and (r.get("sic") or "").isdigit() and 6000 <= int(r["sic"]) <= 6799]
    inv(not leaked, f"no SIC 6000-6799 row carries framework_leg_failed ({len(leaked)} leaked)")

    # controls, skipped rather than failed if the name is absent from this store
    idx = {r.get("ticker"): r for r in after}
    for t, want in (("DDOG", "sbc"), ("GLP", "tax_anomaly")):
        r = idx.get(t)
        if r:
            inv(r.get("gate0_framework_pass") == "false" and want in (r.get("framework_leg_failed") or ""),
                f"positive control {t}: framework_leg_failed contains '{want}' (got '{r.get('framework_leg_failed')}')")
        else:
            notes.append(f"SKIP  positive control {t} not in this store")
    for t in ("SEB", "NVR", "BKNG", "AZO"):
        r = idx.get(t)
        if r:
            inv(r.get("shares_scale_suspect") == "false",
                f"negative control {t}: shares_scale_suspect is false (a real small share count, not a scale error)")
        else:
            notes.append(f"SKIP  negative control {t} not in this store")
    r = idx.get("MCD")
    if r:
        inv(r.get("shares_scale_suspect") == "true",
            "positive control MCD: shares_scale_suspect is true (716 'shares' against $26.9B revenue)")

    # ---------------- COUNTS (reported, never asserted) ----------------
    def n(f): return sum(1 for r in after if f(r))
    counts = {
        "rows": len(after),
        "gate0_pass_true": n(lambda r: r.get("gate0_pass") == "true"),
        "framework_false": n(lambda r: r.get("gate0_pass") == "true" and r.get("gate0_framework_pass") == "false"),
        "stale_rows": n(lambda r: r.get("ttm_stale_concepts")),
        "withdrawn": withdrawn,
        "shares_scale": n(lambda r: r.get("shares_scale_suspect") == "true"),
        "shares_scale_with_fps": n(lambda r: r.get("shares_scale_suspect") == "true" and r.get("fcf_per_share")),
        "fps_5y_ago": n(lambda r: r.get("fcf_per_share_5y_ago")),
    }

    print("=" * 72)
    print("INVARIANTS")
    print("=" * 72)
    for line in notes + fails:
        print("  " + line)
    print()
    print("=" * 72)
    print("COUNTS -- reported only. Reference is the 2026-08-30 store; a newer")
    print("store SHOULD differ. Judge these, do not gate on them.")
    print("=" * 72)
    print(f"  {'metric':26s} {'this store':>12s} {'2026-08-30 ref':>16s}")
    for k, v in counts.items():
        print(f"  {k:26s} {v:>12,} {REFERENCE[k]:>16,}")
    print()
    if fails:
        print(f"RESULT: {len(fails)} INVARIANT FAILURE(S) -- DO NOT PUSH.")
        return 1
    print("VERIFY_FIXES_ALL_INVARIANTS_HELD")
    return 0

if __name__ == "__main__":
    sys.exit(main())
