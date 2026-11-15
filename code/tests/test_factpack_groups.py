"""fact_pack GROUPS must show the analyst every gate0.csv column its prompt tells it to act on.

2026-11-15: capex_tag_hold, capex_unconfirmed, fail_dilution, revenue_subset_history_suspect,
tangible_book_buyback_exempt, lease_heavy_upper_bound and currency_unresolved were named in
screening-analyst.md but missing from GROUPS, so `fact_pack.py rows` never showed them.

Method: every backticked token in screening-analyst.md that is also a gate0.csv header
column. (Parsing is deliberately the intersection, so prose like `NOT_EVALUABLE` or
`--quote` can never false-fail.)
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".claude/screening/fact_pack.py"
AGENT = REPO / ".claude/agents/screening-analyst.md"
GATE0 = REPO / "store/gate0.csv"
if not (SCRIPT.is_file() and AGENT.is_file() and GATE0.is_file()):
    pytest.skip("needs .claude/ and store/gate0.csv beside this code tree", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("fact_pack", SCRIPT)
fp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fp)


def header():
    return set(GATE0.open(encoding="utf-8").readline().strip().split(","))


def analyst_columns():
    tokens = set(re.findall(r"`([A-Za-z0-9_]+)`", AGENT.read_text(encoding="utf-8")))
    return tokens & header()


def test_every_analyst_named_column_is_in_a_group():
    shown = {c for cols in fp.GROUPS.values() for c in cols}
    missing = sorted(analyst_columns() - shown)
    assert not missing, f"screening-analyst.md names these gate0.csv columns but fact_pack GROUPS hides them: {missing}"


def test_parser_sees_the_known_flag_columns():
    # Guards the parser itself: if it silently found nothing the test above would pass vacuously.
    assert {"capex_tag_hold", "capex_unconfirmed", "capex_suspect"} <= analyst_columns()


# Explicit list: columns the 2026-11-15 audit required, whether or not the analyst prompt
# names them today (the first four are named only by other agents / the job definition).
REQUIRED = [
    "capex_tag_hold", "capex_unconfirmed", "fail_dilution", "revenue_subset_history_suspect",
    "tangible_book_buyback_exempt", "lease_heavy_upper_bound", "currency_unresolved",
    "dilution_sbc_issuance",
]


@pytest.mark.parametrize("column", REQUIRED)
def test_required_columns_exist_and_are_shown(column):
    assert column in header()
    assert column in {c for cols in fp.GROUPS.values() for c in cols}
