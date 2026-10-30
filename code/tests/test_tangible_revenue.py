"""Regression tests for the 2026-10-29 revenue-subset and acquisitions defects.

revenue was a first-match-wins tag chain over XBRL tags a filer can report
SIDE BY SIDE: the ASC 606 contract-revenue tag (a SUBSET) sat first in the
chain and won even when the same filing carried the broader Revenues total.
Understated revenue overstates sbc_pct_revenue and understates both inorganic
ratios (acq_intensity, bs_acq_intensity divide by revenue) -- it fails OPEN on
the inorganic screen. SEZL resolved FY2025 revenue to 235,878,000 against
Revenues of 450,279,000; RUSHA lost 5.0% the same way, and its acquisitions
were null because it tags PaymentsToAcquireBusinessesGross, which no chain
held.

The MMSI stale-latest_q tangible-book defect is NOT pinned here: it is owned
by the parallel W4 change (a latest_q component whose own period_end predates
latest_q_period_end is nulled there), and the end-to-end regression lands with
the coordinator after that merge.

Shapes pinned here because all were found live in the store:
  - the subset tag winning ahead of the side-by-side total (SEZL, RUSHA)
  - a gross-of-cash acquisitions tag unreachable by the chain (RUSHA)
"""

import datetime as dt
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_facts  # noqa: E402
import concepts  # noqa: E402
import gate0  # noqa: E402
import store_defects  # noqa: E402

REVENUE = concepts.CONCEPTS_BY_NAME["revenue"]
ACQUISITIONS = concepts.CONCEPTS_BY_NAME["acquisitions"]
SUBSET_CANDIDATE = concepts.CONCEPTS_BY_NAME["revenue_subset_candidate"]

RFCWCEAT = "RevenueFromContractWithCustomerExcludingAssessedTax"


def _fact(tag, value, start, end, accn="0000000000-25-000001", filed=None):
    return {
        "tag": tag,
        "value": value,
        "unit": "USD",
        "start": dt.date.fromisoformat(start) if start else None,
        "end": dt.date.fromisoformat(end),
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "form": "10-K",
        "accn": accn,
        "filed": filed,
        "taxonomy": "us-gaap",
    }


def _index(entries):
    """Minimal (tag, fiscal_year, period) -> facts index for _resolve_one.

    An entry is (tag, value, start, end) with optional trailing (accn, filed).
    """
    index = {}
    for entry in entries:
        tag, value, start, end = entry[:4]
        accn = entry[4] if len(entry) > 4 else "0000000000-25-000001"
        filed = dt.date.fromisoformat(entry[5]) if len(entry) > 5 else None
        index.setdefault((tag, 2025, "FY"), []).append(
            _fact(tag, value, start, end, accn=accn, filed=filed)
        )
    return index


FY_SPAN = ("2025-01-01", "2025-12-31")

# --------------------------------------------------------------------------
# (b) The SEZL / RUSHA shape: the side-by-side TOTAL wins, not the first match
# --------------------------------------------------------------------------


def test_sezl_shape_picks_the_total_over_the_606_subset():
    """SEZL FY2025: RFCWCEAT 235,878,000 beside Revenues 450,279,000 (the other
    214,401,000 is RevenueNotFromContractWithCustomer -- a lender's interest
    and fee income, which IS its revenue). First-match-wins published the
    subset."""
    row = build_facts._resolve_one(
        REVENUE, 2025, "FY",
        _index([(RFCWCEAT, 235_878_000.0, *FY_SPAN), ("Revenues", 450_279_000.0, *FY_SPAN)]),
    )
    assert row["value"] == pytest.approx(450_279_000.0)
    assert row["source_tag"] == "Revenues"


def test_rusha_shape_picks_the_total_over_the_subset():
    """RUSHA FY2025: 7,064,640,000 contract revenue beside 7,434,195,000 total
    -- a 5.0% haircut under first-match-wins."""
    row = build_facts._resolve_one(
        REVENUE, 2025, "FY",
        _index([(RFCWCEAT, 7_064_640_000.0, *FY_SPAN), ("Revenues", 7_434_195_000.0, *FY_SPAN)]),
    )
    assert row["value"] == pytest.approx(7_434_195_000.0)
    assert row["source_tag"] == "Revenues"


def test_a_candidate_from_a_different_span_never_wins():
    """A 6-month Revenues beside a 3-month RFCWCEAT is a different measure,
    not a sibling total. Maxing across spans would compare half a year with a
    quarter."""
    row = build_facts._resolve_one(
        REVENUE, 2025, "FY",
        _index([
            (RFCWCEAT, 100_000_000.0, "2025-01-01", "2025-03-31"),
            ("Revenues", 190_000_000.0, "2025-01-01", "2025-06-30"),
        ]),
    )
    assert row["value"] == pytest.approx(100_000_000.0)
    assert row["source_tag"] == RFCWCEAT


def test_a_lone_subset_tag_is_unchanged():
    """Clean control: with no side-by-side total reported, the chain winner
    and its value are exactly what first-match-wins produced."""
    row = build_facts._resolve_one(
        REVENUE, 2025, "FY", _index([(RFCWCEAT, 235_878_000.0, *FY_SPAN)])
    )
    assert row["value"] == pytest.approx(235_878_000.0)
    assert row["source_tag"] == RFCWCEAT


def test_agreeing_tags_keep_the_first_chain_tag():
    """Revenues == RFCWCEAT is the ordinary case; nothing about the row may
    change, source_tag included."""
    row = build_facts._resolve_one(
        REVENUE, 2025, "FY",
        _index([(RFCWCEAT, 100_000_000.0, *FY_SPAN), ("Revenues", 100_000_000.0, *FY_SPAN)]),
    )
    assert row["value"] == pytest.approx(100_000_000.0)
    assert row["source_tag"] == RFCWCEAT


def test_including_assessed_tax_never_wins_the_max():
    """The excise-tax trap: the IncludingAssessedTax variant is larger by the
    tax, and a LARGER revenue lowers sbc_pct_revenue and both acquisition
    intensities -- the flattering, fail-open direction."""
    row = build_facts._resolve_one(
        REVENUE, 2025, "FY",
        _index([
            (RFCWCEAT, 100_000_000.0, *FY_SPAN),
            ("RevenueFromContractWithCustomerIncludingAssessedTax", 112_000_000.0, *FY_SPAN),
        ]),
    )
    assert row["value"] == pytest.approx(100_000_000.0)
    assert row["source_tag"] == RFCWCEAT


# --------------------------------------------------------------------------
# Finding 1 (rv-review.md, HIGH): side-by-side totals compete only within the
# SAME accession. An obsolete filing's larger tag must never beat the
# restatement that superseded it.
# --------------------------------------------------------------------------

OLD_ACCN = "0000000000-25-000099"
NEW_ACCN = "0000000000-26-000001"


def test_an_obsolete_filings_larger_tag_never_beats_the_restatement():
    """The reviewer's synthetic, verbatim: the 2026 restatement reports
    RFCWCEAT=80 for the FY span and OMITS Revenues; the superseded 2025
    filing had reported Revenues=100 for the same span. Cross-accession
    maxing published 100 from a filing the filer itself replaced."""
    row = build_facts._resolve_one(
        REVENUE, 2025, "FY",
        _index([
            (RFCWCEAT, 80_000_000.0, *FY_SPAN, NEW_ACCN, "2026-02-01"),
            ("Revenues", 100_000_000.0, *FY_SPAN, OLD_ACCN, "2025-02-01"),
        ]),
    )
    assert row["value"] == pytest.approx(80_000_000.0)
    assert row["source_tag"] == RFCWCEAT
    assert row["accn"] == NEW_ACCN


def test_same_accession_siblings_still_max():
    """The SEZL/RUSHA fix is untouched: two totals reported side by side IN
    ONE filing still resolve to the larger."""
    row = build_facts._resolve_one(
        REVENUE, 2025, "FY",
        _index([
            (RFCWCEAT, 235_878_000.0, *FY_SPAN, NEW_ACCN, "2026-02-01"),
            ("Revenues", 450_279_000.0, *FY_SPAN, NEW_ACCN, "2026-02-01"),
        ]),
    )
    assert row["value"] == pytest.approx(450_279_000.0)
    assert row["source_tag"] == "Revenues"


def test_a_restated_total_inside_the_restatement_wins():
    """The restatement carries BOTH tags (the comparative re-presented): the
    larger of the restatement's OWN pair wins, and the obsolete filing is
    irrelevant either way."""
    row = build_facts._resolve_one(
        REVENUE, 2025, "FY",
        _index([
            (RFCWCEAT, 80_000_000.0, *FY_SPAN, NEW_ACCN, "2026-02-01"),
            ("Revenues", 95_000_000.0, *FY_SPAN, NEW_ACCN, "2026-02-01"),
            ("Revenues", 100_000_000.0, *FY_SPAN, OLD_ACCN, "2025-02-01"),
        ]),
    )
    assert row["value"] == pytest.approx(95_000_000.0)
    assert row["source_tag"] == "Revenues"
    assert row["accn"] == NEW_ACCN


def test_subset_candidate_ignores_cross_accession_siblings():
    """The diagnostic answers the same question as the max rule: with the
    only sibling in a superseded filing there is no side-by-side
    disagreement to report."""
    row = build_facts._resolve_one(
        SUBSET_CANDIDATE, 2025, "FY",
        _index([
            (RFCWCEAT, 80_000_000.0, *FY_SPAN, NEW_ACCN, "2026-02-01"),
            ("Revenues", 100_000_000.0, *FY_SPAN, OLD_ACCN, "2025-02-01"),
        ]),
    )
    assert row is None


# --------------------------------------------------------------------------
# revenue_subset_tested (finding 5, rv-review.md MEDIUM): an evaluable
# marker -- emitted whenever at least two side-by-side tags shared one span
# inside ONE filing, whether they agreed or not. Lone tag / cross-accession
# only -> no row, so the suspect flags can tell untested from tested-clean.
# --------------------------------------------------------------------------

TESTED = concepts.CONCEPTS_BY_NAME["revenue_subset_tested"]


def test_subset_tested_emitted_when_siblings_agree():
    row = build_facts._resolve_one(
        TESTED, 2025, "FY",
        _index([(RFCWCEAT, 100_000_000.0, *FY_SPAN), ("Revenues", 100_000_000.0, *FY_SPAN)]),
    )
    assert row is not None
    assert row["value"] == pytest.approx(2.0)


def test_subset_tested_emitted_when_siblings_disagree():
    row = build_facts._resolve_one(
        TESTED, 2025, "FY",
        _index([(RFCWCEAT, 235_878_000.0, *FY_SPAN), ("Revenues", 450_279_000.0, *FY_SPAN)]),
    )
    assert row is not None


def test_subset_tested_is_null_with_a_single_tag():
    row = build_facts._resolve_one(
        TESTED, 2025, "FY", _index([(RFCWCEAT, 100_000_000.0, *FY_SPAN)])
    )
    assert row is None


def test_subset_tested_is_null_across_accessions():
    row = build_facts._resolve_one(
        TESTED, 2025, "FY",
        _index([
            (RFCWCEAT, 80_000_000.0, *FY_SPAN, NEW_ACCN, "2026-02-01"),
            ("Revenues", 100_000_000.0, *FY_SPAN, OLD_ACCN, "2025-02-01"),
        ]),
    )
    assert row is None


def test_subset_tested_is_null_across_spans():
    row = build_facts._resolve_one(
        TESTED, 2025, "FY",
        _index([
            (RFCWCEAT, 100_000_000.0, "2025-01-01", "2025-03-31"),
            ("Revenues", 190_000_000.0, "2025-01-01", "2025-06-30"),
        ]),
    )
    assert row is None


def test_subset_tested_is_diagnostic_never_scoring():
    assert "revenue_subset_tested" in concepts.DIAGNOSTIC_CONCEPT_NAMES
    assert "revenue_subset_tested" not in gate0.ALL_CONCEPTS


# --------------------------------------------------------------------------
# The revenue_subset_candidate diagnostic: the figure first-match-wins would
# have published, emitted only when the sibling totals disagree.
# --------------------------------------------------------------------------


def test_subset_candidate_is_the_smallest_disagreeing_tag():
    row = build_facts._resolve_one(
        SUBSET_CANDIDATE, 2025, "FY",
        _index([(RFCWCEAT, 235_878_000.0, *FY_SPAN), ("Revenues", 450_279_000.0, *FY_SPAN)]),
    )
    assert row is not None
    assert row["value"] == pytest.approx(235_878_000.0)
    assert row["source_tag"] == RFCWCEAT


def test_subset_candidate_is_null_when_the_tags_agree():
    row = build_facts._resolve_one(
        SUBSET_CANDIDATE, 2025, "FY",
        _index([(RFCWCEAT, 100_000_000.0, *FY_SPAN), ("Revenues", 100_000_000.0, *FY_SPAN)]),
    )
    assert row is None


def test_subset_candidate_is_null_with_a_single_tag():
    row = build_facts._resolve_one(
        SUBSET_CANDIDATE, 2025, "FY", _index([(RFCWCEAT, 100_000_000.0, *FY_SPAN)])
    )
    assert row is None


def test_subset_candidate_is_diagnostic_never_scoring():
    """The concept describes a disagreement; it must never vote on periods or
    feed a Gate 0 leg."""
    assert "revenue_subset_candidate" in concepts.DIAGNOSTIC_CONCEPT_NAMES
    assert "revenue_subset_candidate" not in gate0.ALL_CONCEPTS


# --------------------------------------------------------------------------
# revenue_subset_suspect: the reader flag. Reader-only -- it must never move
# gate0_pass (store_defects posture: flags only).
# --------------------------------------------------------------------------


def _suspect(rows):
    frame = pl.DataFrame(
        rows,
        schema={
            "cik": pl.Int64,
            "revenue": pl.Float64,
            "revenue_subset_candidate": pl.Float64,
            "revenue_subset_tested": pl.Float64,
        },
    )
    return store_defects._revenue_subset_suspect(frame)


def test_a_material_gap_is_flagged():
    """RUSHA's 5.0% gap and SEZL's 47.6% gap are both material."""
    out = _suspect([
        {"cik": 1, "revenue": 450_279_000.0, "revenue_subset_candidate": 235_878_000.0,
         "revenue_subset_tested": 2.0},
        {"cik": 2, "revenue": 7_434_195_000.0, "revenue_subset_candidate": 7_064_640_000.0,
         "revenue_subset_tested": 2.0},
    ])
    assert out["revenue_subset_suspect"].to_list() == [True, True]


def test_an_immaterial_gap_is_not_flagged():
    out = _suspect([{"cik": 1, "revenue": 1_000_000_000.0,
                     "revenue_subset_candidate": 997_000_000.0, "revenue_subset_tested": 2.0}])
    assert out["revenue_subset_suspect"].to_list() == [False]


def test_tested_and_agreeing_is_measured_false():
    """False is reserved for tested-and-agreed (finding 5): the reviewer
    could not distinguish this from untested before revenue_subset_tested."""
    out = _suspect([{"cik": 1, "revenue": 1_000_000_000.0,
                     "revenue_subset_candidate": None, "revenue_subset_tested": 2.0}])
    assert out["revenue_subset_suspect"].to_list() == [False]


def test_an_untested_revenue_is_null_not_false():
    """The reviewer's synthetic (finding 5): revenue 100 with only one
    eligible tag -- nothing was compared, so the flag is null, not a False
    that reads as checked-and-clean. Missing is never zero."""
    out = _suspect([{"cik": 1, "revenue": 100_000_000.0,
                     "revenue_subset_candidate": None, "revenue_subset_tested": None}])
    assert out["revenue_subset_suspect"].to_list() == [None]


def test_an_unresolved_revenue_is_not_evaluated():
    """Missing is never zero: with no revenue there is nothing to test, and
    the flag is null, never a False that reads as checked-and-clean."""
    out = _suspect([{"cik": 1, "revenue": None,
                     "revenue_subset_candidate": None, "revenue_subset_tested": None}])
    assert out["revenue_subset_suspect"].to_list() == [None]


# --------------------------------------------------------------------------
# (c) RUSHA acquisitions: the Gross tag joins the chain behind Net
# --------------------------------------------------------------------------


def test_gross_acquisitions_resolve_when_net_is_absent():
    """RUSHA tags only PaymentsToAcquireBusinessesGross. Gross overstates by
    the cash acquired -- the fail-CLOSED direction for an inorganic screen."""
    row = build_facts._resolve_one(
        ACQUISITIONS, 2025, "FY",
        _index([("PaymentsToAcquireBusinessesGross", 24_314_000.0, *FY_SPAN)]),
    )
    assert row is not None
    assert row["value"] == pytest.approx(24_314_000.0)
    assert row["source_tag"] == "PaymentsToAcquireBusinessesGross"


def test_net_still_wins_over_gross_when_both_are_tagged():
    """Net-of-cash is the better measure wherever it exists."""
    row = build_facts._resolve_one(
        ACQUISITIONS, 2025, "FY",
        _index([
            ("PaymentsToAcquireBusinessesNetOfCashAcquired", 20_000_000.0, *FY_SPAN),
            ("PaymentsToAcquireBusinessesGross", 24_314_000.0, *FY_SPAN),
        ]),
    )
    assert row["value"] == pytest.approx(20_000_000.0)
    assert row["source_tag"] == "PaymentsToAcquireBusinessesNetOfCashAcquired"


# --------------------------------------------------------------------------
# RUSHA ttm_acquisitions: FY25 - YTD2'25 + YTD2'26, no build_ttm change needed
# --------------------------------------------------------------------------


def _flow(cik, concept, period, start, end, value):
    return {
        "cik": cik,
        "concept": concept,
        "fiscal_period": period,
        "period_start": dt.date.fromisoformat(start),
        "period_end": dt.date.fromisoformat(end),
        "value": float(value),
    }


def test_rusha_ttm_acquisitions_from_the_rollforward():
    """24,314,000 (FY2025) - 24,119,000 (YTD2-2025) + 51,762,000 (YTD2-2026)
    = 51,957,000. RUSHA has no Q1-2026 acquisitions fact; the YTD pair is the
    whole identity."""
    facts = pl.DataFrame(
        [
            _flow(1012019, "acquisitions", "FY", "2025-01-01", "2025-12-31", 24_314_000),
            _flow(1012019, "acquisitions", "YTD2", "2025-01-01", "2025-06-30", 24_119_000),
            _flow(1012019, "acquisitions", "YTD2", "2026-01-01", "2026-06-30", 51_762_000),
        ],
        schema={
            "cik": pl.Int64,
            "concept": pl.Utf8,
            "fiscal_period": pl.Utf8,
            "period_start": pl.Date,
            "period_end": pl.Date,
            "value": pl.Float64,
        },
    )
    out = gate0.build_ttm(facts)
    assert out["ttm_acquisitions"][0] == pytest.approx(51_957_000.0)
