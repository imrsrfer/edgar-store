"""SBC fallback tags and the sbc_unmeasured screen flag (added 2026-09-29).

Before this, a filer reporting SBC under any tag outside the first three
resolved sbc to NULL, and the TTM step then ASSUMED ZERO -- overstating
FCF-after-SBC, the master metric, in the company's favour. BKE and CTS reached
the Review Queue that way with $17.3M and $6.6M of real TTM SBC.
"""

import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_facts  # noqa: E402
import concepts  # noqa: E402
import screen  # noqa: E402

SBC = concepts.CONCEPTS_BY_NAME["sbc"]

PRIMARY = (
    "ShareBasedCompensation",
    "AllocatedShareBasedCompensationExpense",
    "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensationCost1",
)
FALLBACKS = (
    "RestrictedStockExpense",
    "StockOptionPlanExpense",
    "EmployeeStockOwnershipPlanESOPCompensationExpense",
    "AdjustmentsToAdditionalPaidInCapitalSharebasedCompensationRequisiteServicePeriodRecognitionValue",
    "AdjustmentsToAdditionalPaidInCapitalShareBasedCompensationRestrictedStockUnitsRequisiteServicePeriodRecognition",
)


def _index(entries):
    index = {}
    for tag, value in entries:
        index.setdefault((tag, 2025, "FY"), []).append(
            {
                "tag": tag,
                "value": value,
                "unit": "USD",
                "start": "2025-02-02",
                "end": "2026-01-31",
                "fiscal_year": 2025,
                "fiscal_period": "FY",
                "form": "10-K",
                "accn": "0000000000-26-000001",
                "filed": None,
                "taxonomy": "us-gaap",
            }
        )
    return index


def test_primary_tags_still_win_and_fallbacks_follow_in_order():
    assert SBC.chain[:3] == PRIMARY
    assert SBC.chain[3:] == FALLBACKS


@pytest.mark.parametrize(
    "tag",
    [
        "StockIssuedDuringPeriodValueShareBasedCompensation",
        "PaymentsRelatedToTaxWithholdingForShareBasedCompensation",
        "ProceedsFromStockOptionsExercised",
        "EmployeeBenefitsAndShareBasedCompensation",
        "IncomeTaxReconciliationNondeductibleExpenseShareBasedCompensationCost",
    ],
)
def test_tags_that_measure_something_else_are_never_read_as_sbc(tag):
    assert tag not in SBC.all_tags


def test_the_bke_shape_resolves_through_restricted_stock_expense():
    row = build_facts._resolve_one(
        SBC, 2025, "FY", _index([("RestrictedStockExpense", 16_185_000.0)])
    )
    assert row is not None
    assert row["value"] == pytest.approx(16_185_000.0)
    assert row["source_tag"] == "RestrictedStockExpense"


def test_a_primary_tag_beats_a_fallback_in_the_same_period():
    row = build_facts._resolve_one(
        SBC,
        2025,
        "FY",
        _index([("RestrictedStockExpense", 4_889_000.0), ("ShareBasedCompensation", 5_000_000.0)]),
    )
    assert row["source_tag"] == "ShareBasedCompensation"
    assert row["value"] == pytest.approx(5_000_000.0)


def test_the_expense_tag_beats_the_apic_mirror():
    row = build_facts._resolve_one(
        SBC,
        2025,
        "FY",
        _index(
            [
                ("AdjustmentsToAdditionalPaidInCapitalSharebasedCompensationRequisiteServicePeriodRecognitionValue", 4_354_000.0),
                ("RestrictedStockExpense", 4_889_000.0),
            ]
        ),
    )
    assert row["source_tag"] == "RestrictedStockExpense"


def test_no_sbc_tag_at_all_still_resolves_to_nothing():
    """Negative control: the fallbacks must not invent an SBC."""
    assert build_facts._resolve_one(SBC, 2025, "FY", _index([("Revenues", 1.0)])) is None


def test_sbc_unmeasured_truth_table():
    frame = pl.DataFrame(
        {
            "ticker": ["A", "B", "C", "D"],
            "ttm_sbc_assumed_zero": [True, True, False, False],
            "sbc_ever_reported": [False, True, False, True],
        }
    )
    out = screen.add_sbc_unmeasured(frame)
    assert out["sbc_unmeasured"].to_list() == [True, False, False, False]


def test_sbc_unmeasured_reads_csv_string_booleans():
    """gate0.csv writes lowercase 'true'/'false'; a string column must not
    silently read as all-False."""
    frame = pl.DataFrame(
        {"ttm_sbc_assumed_zero": ["true", "true", ""], "sbc_ever_reported": ["false", "true", "false"]}
    )
    out = screen.add_sbc_unmeasured(frame)
    assert out["sbc_unmeasured"].to_list() == [True, False, None]


def test_sbc_unmeasured_is_null_not_false_on_an_older_store():
    out = screen.add_sbc_unmeasured(pl.DataFrame({"ticker": ["A"]}))
    assert out["sbc_unmeasured"].to_list() == [None]
