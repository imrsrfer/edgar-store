"""Separately reported operating-asset purchases are capex (2026-11-06).

A full-universe check against Compustat found filers whose PP&E leg is complete
but whose fleet, aircraft, rig or software purchases sit on a SECOND standard
line the capex sum never read: MGRC 44.4 vs 187.0, SKYW 32 vs 653, BORR 0.3 vs
88.5, ALK 309 vs 1,588. Values below are those filers' FY figures, in USD.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_facts  # noqa: E402
import concepts  # noqa: E402

CAPEX = concepts.CONCEPTS_BY_NAME["capex"]
NEW_LEGS = (
    "PaymentsForSoftware",
    "PaymentsToAcquireEquipmentOnLease",
    "PaymentsToAcquireOilAndGasPropertyAndEquipment",
    "PaymentsForFlightEquipment",
    "PaymentsToAcquireOtherProductiveAssets",
    "PaymentsToAcquireWaterAndWasteWaterSystems",
    "PaymentsForNuclearFuel",
    "PaymentsToAcquireAndDevelopRealEstate",
)


def _index(rows):
    """Same (tag, fiscal_year, period) -> [facts] shape as test_capex._index."""
    index = {}
    for tag, value in rows:
        index.setdefault((tag, 2025, "FY"), []).append({
            "tag": tag, "value": value, "unit": "USD", "start": "2025-01-01", "end": "2025-12-31",
            "fiscal_year": 2025, "fiscal_period": "FY", "form": "10-K",
            "accn": "0000000000-25-000001", "filed": None, "taxonomy": "us-gaap",
        })
    return index


def _capex(rows):
    row = build_facts._resolve_one(CAPEX, 2025, "FY", _index(rows))
    assert row is not None
    return row


@pytest.mark.parametrize("tag", NEW_LEGS)
def test_each_new_leg_is_a_summed_component(tag):
    assert tag in CAPEX.components
    assert tag not in CAPEX.chain


@pytest.mark.parametrize("tag", NEW_LEGS)
def test_each_new_leg_counts_in_the_investing_reconciliation(tag):
    assert tag in concepts.INVESTING_OUTFLOW_TAGS


def test_investing_outflow_tags_hold_no_duplicates():
    tags = concepts.INVESTING_OUTFLOW_TAGS
    assert len(tags) == len(set(tags)), "a duplicate tag is summed twice in the residual"


def test_mgrc_rental_fleet_is_added_to_ppe():
    row = _capex([("PaymentsToAcquirePropertyPlantAndEquipment", 44_384_000.0),
                  ("PaymentsToAcquireEquipmentOnLease", 142_616_000.0)])
    assert row["value"] == pytest.approx(187_000_000.0)


def test_skyw_aircraft_are_added_to_ppe():
    row = _capex([("PaymentsToAcquirePropertyPlantAndEquipment", 32_020_000.0),
                  ("PaymentsForFlightEquipment", 621_350_000.0)])
    assert row["value"] == pytest.approx(653_370_000.0)


def test_alk_other_ppe_flight_and_other_productive_assets_sum():
    """ALK reported three disjoint lines; the store read only the first, 309M of 1,588M."""
    row = _capex([("PaymentsToAcquireOtherPropertyPlantAndEquipment", 309_000_000.0),
                  ("PaymentsForFlightEquipment", 1_063_000_000.0),
                  ("PaymentsToAcquireOtherProductiveAssets", 216_000_000.0)])
    assert row["value"] == pytest.approx(1_588_000_000.0)
    assert row["source_tag"].count("+") == 2


def test_borr_rigs_are_capex():
    row = _capex([("PaymentsToAcquireMachineryAndEquipment", 300_000.0),
                  ("PaymentsToAcquireOilAndGasPropertyAndEquipment", 88_200_000.0)])
    assert row["value"] == pytest.approx(88_500_000.0)


def test_other_productive_assets_never_beats_a_broad_total():
    """Like other PP&E, the residual 'other productive assets' line is summed,
    never a substitute for a broad PaymentsToAcquireProductiveAssets total."""
    row = _capex([("PaymentsToAcquireProductiveAssets", 900_000_000.0),
                  ("PaymentsToAcquireOtherProductiveAssets", 20_000_000.0)])
    assert row["value"] == pytest.approx(900_000_000.0)


def test_construction_in_process_stays_out():
    """On AEP it overshot the Compustat gap by 65%: it double-counts a PP&E total."""
    assert "PaymentsForConstructionInProcess" not in CAPEX.all_tags
