"""Tests for the EDGAR pipeline.

Two layers:

* unit tests over the fiscal-calendar logic and the null-propagation rules,
  which run anywhere with no data;
* integration tests pinning hand-verified figures from real filings, which skip
  themselves until ``build_facts.py`` has been run.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

import build_facts
import gate0
import screen
from concepts import CONCEPTS_BY_NAME, WANTED_TAGS
from edgar_lib import Paths


def make_fact(tag, start, end, value, fy, fp, form, filed, accn="A", unit="USD"):
    """One raw fact in the shape :func:`build_facts.extract_raw_facts` emits."""
    return {
        "tag": tag,
        "unit": unit,
        "start": start,
        "end": end,
        "value": value,
        "accn": accn,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
    }


# --------------------------------------------------------------------------
# Fiscal-calendar handling
# --------------------------------------------------------------------------


def test_labels_february_year_end_without_calendar_alignment():
    """An off-cycle (February) filer gets its own fiscal year, not a calendar one."""
    # Arrange
    facts = [
        make_fact("Revenues", date(2024, 3, 3), date(2025, 3, 1), 100.0, 2025, "FY",
                  "10-K", date(2025, 4, 20), accn="a25"),
        make_fact("Revenues", date(2025, 3, 2), date(2026, 2, 28), 110.0, 2026, "FY",
                  "10-K", date(2026, 4, 20), accn="a26"),
    ]

    # Act
    annual, quarterly, offset = build_facts.build_period_labels(facts)
    labels = [build_facts.label_fact(f, annual, quarterly, offset) for f in facts]

    # Assert
    assert labels == [(2025, "FY"), (2026, "FY")]
    assert offset == 0


def test_comparative_period_is_not_labelled_with_the_filing_year():
    """A prior-year comparative carries the filing's fy; it must not inherit it."""
    # Arrange: the FY2025 10-K restates FY2024, so both rows carry fy=2025.
    current = make_fact("Revenues", date(2024, 12, 30), date(2025, 12, 28), 120.0,
                        2025, "FY", "10-K", date(2026, 2, 20), accn="a25")
    comparative = make_fact("Revenues", date(2023, 12, 31), date(2024, 12, 29), 100.0,
                            2025, "FY", "10-K", date(2026, 2, 20), accn="a25")
    prior_filing = make_fact("Revenues", date(2023, 12, 31), date(2024, 12, 29), 100.0,
                             2024, "FY", "10-K", date(2025, 2, 20), accn="a24")

    # Act
    annual, quarterly, offset = build_facts.build_period_labels(
        [current, comparative, prior_filing]
    )

    # Assert
    assert build_facts.label_fact(current, annual, quarterly, offset) == (2025, "FY")
    assert build_facts.label_fact(comparative, annual, quarterly, offset) == (2024, "FY")


def test_january_year_end_resolves_to_the_prior_fiscal_year():
    """A retailer ending in January calls that period the prior fiscal year."""
    # Arrange
    facts = [
        make_fact("Revenues", date(2024, 1, 29), date(2025, 2, 1), 100.0, 2024, "FY",
                  "10-K", date(2025, 3, 20), accn="a24"),
        make_fact("Revenues", date(2025, 2, 2), date(2026, 1, 31), 110.0, 2025, "FY",
                  "10-K", date(2026, 3, 20), accn="a25"),
    ]

    # Act
    annual, quarterly, offset = build_facts.build_period_labels(facts)
    unseen = make_fact("Revenues", date(2023, 1, 30), date(2024, 1, 27), 90.0, 2023,
                       "FY", "10-K", date(2024, 3, 20), accn="a23")

    # Assert: the learned offset carries unlabelled ends to the right year.
    assert offset == -1
    assert build_facts.label_fact(unseen, annual, quarterly, offset) == (2023, "FY")


def test_year_to_date_duration_is_labelled_ytd_never_a_quarter():
    """A cumulative figure is RETAINED under its own label, never as a quarter.

    Changed 2026-08-27. This test used to assert the fact was dropped entirely.
    Dropping it was the safe half of a correct instinct and the expensive half
    of a wrong one: filers report cash flow only as YTD, so discarding these
    left Q1 as the only surviving quarterly cash-flow fact, and build_ttm's
    four-row sum silently became four Q1s from four different years. The
    invariant that matters is unchanged and is what is asserted here -- a
    cumulative span must never carry a Q1..Q4 label, because every filter in
    this pipeline matches on those.
    """
    # Arrange
    ytd9 = make_fact("Revenues", date(2025, 1, 1), date(2025, 9, 30), 300.0, 2025, "Q3",
                     "10-Q", date(2025, 10, 30))
    ytd6 = make_fact("Revenues", date(2025, 1, 1), date(2025, 6, 30), 200.0, 2025, "Q2",
                     "10-Q", date(2025, 7, 30))

    # Act
    label9 = build_facts.label_fact(ytd9, {}, {}, 0)
    label6 = build_facts.label_fact(ytd6, {}, {}, 0)

    # Assert
    assert label9 == (2025, "YTD3")
    assert label6 == (2025, "YTD2")
    assert label9[1] not in ("Q1", "Q2", "Q3", "Q4", "FY")
    assert label6[1] not in ("Q1", "Q2", "Q3", "Q4", "FY")


def test_instant_at_year_end_joins_the_annual_period():
    """A balance-sheet instant belongs to the fiscal year closing on that date."""
    # Arrange
    annual_fact = make_fact("Revenues", date(2024, 12, 30), date(2025, 12, 28), 120.0,
                            2025, "FY", "10-K", date(2026, 2, 20), accn="a25")
    instant = make_fact("Goodwill", None, date(2025, 12, 28), 25.0, 2025, "FY", "10-K",
                        date(2026, 2, 20), accn="a25")

    # Act
    annual, quarterly, offset = build_facts.build_period_labels([annual_fact, instant])

    # Assert
    assert build_facts.label_fact(instant, annual, quarterly, offset) == (2025, "FY")


def test_restated_value_prefers_the_most_recent_filing():
    """A figure restated in a later 10-K resolves to the later accession."""
    # Arrange: FY2025 as first reported, then as restated inside the FY2026 10-K
    # alongside that filing's own primary period.
    original = make_fact("Revenues", date(2024, 12, 30), date(2025, 12, 28), 100.0,
                         2025, "FY", "10-K", date(2026, 2, 20), accn="a25")
    restated = make_fact("Revenues", date(2024, 12, 30), date(2025, 12, 28), 90.0,
                         2026, "FY", "10-K", date(2027, 2, 20), accn="a26")
    later_primary = make_fact("Revenues", date(2025, 12, 29), date(2026, 12, 27), 130.0,
                              2026, "FY", "10-K", date(2027, 2, 20), accn="a26")
    facts = [original, restated, later_primary]

    # Act
    annual, quarterly, offset = build_facts.build_period_labels(facts)
    rows = build_facts.resolve_concepts(facts, annual, quarterly, offset)
    revenue = [r for r in rows if r["concept"] == "revenue" and r["fiscal_year"] == 2025]

    # Assert
    assert len(revenue) == 1
    assert revenue[0]["value"] == 90.0
    assert revenue[0]["restated"] is True


def test_tag_chain_falls_back_and_records_the_tag_used():
    """When the preferred tag is absent, the fallback wins and is named."""
    # Arrange: only the third tag in the revenue chain is reported.
    fact = make_fact("SalesRevenueNet", date(2024, 12, 30), date(2025, 12, 28), 50.0,
                     2025, "FY", "10-K", date(2026, 2, 20), accn="a25")

    # Act
    annual, quarterly, offset = build_facts.build_period_labels([fact])
    rows = build_facts.resolve_concepts([fact], annual, quarterly, offset)
    revenue = [r for r in rows if r["concept"] == "revenue"]

    # Assert
    assert len(revenue) == 1
    assert revenue[0]["source_tag"] == "SalesRevenueNet"
    assert revenue[0]["value"] == 50.0


# --------------------------------------------------------------------------
# Null propagation and configuration
# --------------------------------------------------------------------------


def _one_row(**overrides):
    row = {concept: None for concept in gate0.ALL_CONCEPTS}
    row.update({"cik": 1, "fiscal_year": 2025})
    row.update(overrides)
    return pl.DataFrame([row], schema_overrides={c: pl.Float64 for c in gate0.ALL_CONCEPTS})


def test_missing_sbc_makes_fcf_after_sbc_unknown_not_larger():
    """A company missing SBC has unknown FCF-after-SBC, never a higher one."""
    # Arrange
    frame = _one_row(ocf=100.0, capex=30.0, sbc=None)

    # Act
    result = gate0.compute_metrics(frame)

    # Assert
    assert result["fcf"][0] == 70.0
    assert result["fcf_after_sbc"][0] is None


def test_absent_goodwill_does_not_manufacture_a_tangible_book_pass():
    """An absent Goodwill tag leaves tangible book unknown by default."""
    # Arrange
    frame = _one_row(equity=500.0, goodwill=None, intangibles=None)

    # Act
    strict = gate0.compute_metrics(frame)
    opted_in = gate0.compute_metrics(frame, assume_absent_zero=True)

    # Assert
    assert strict["tangible_book"][0] is None
    assert opted_in["tangible_book"][0] == 500.0


def test_zero_denominator_yields_null_not_infinity():
    # Arrange
    frame = _one_row(ocf=100.0, net_income=0.0)

    # Act
    result = gate0.compute_metrics(frame)

    # Assert
    assert result["income_quality"][0] is None


def test_unknown_flag_is_not_reported_as_a_pass():
    """gate0_status separates a genuine pass from an unverifiable one."""
    # Arrange: everything clean except an unknown tangible book.
    frame = pl.DataFrame(
        [
            {
                "fail_tangible_book": None,
                "fail_income_quality": False,
                "fail_fcf": False,
                "fail_sbc": False,
                "fail_ni_over_oi": False,
                "fail_tax_anomaly": False,
            }
        ],
        schema={c: pl.Boolean for c in gate0.FLAG_COLUMNS},
    )

    # Act
    result = gate0.add_verdict(frame)

    # Assert
    assert result["gate0_pass"][0] is False
    assert result["gate0_status"][0] == "unknown"


def test_parse_sic_ranges_handles_ranges_and_singletons():
    assert gate0.parse_sic_ranges("6000-6799") == [(6000, 6799)]
    assert gate0.parse_sic_ranges("6000-6799,7370") == [(6000, 6799), (7370, 7370)]
    assert gate0.parse_sic_ranges("") == []


def test_every_concept_tag_is_in_the_parser_whitelist():
    """A tag chain the parser does not retain would silently resolve to null."""
    for concept in CONCEPTS_BY_NAME.values():
        for tag in concept.all_tags:
            assert tag in WANTED_TAGS, f"{concept.name} -> {tag} not collected"


# --------------------------------------------------------------------------
# Integration: hand-verified figures from real filings
# --------------------------------------------------------------------------

MILLION = 1e6


@pytest.fixture(scope="module")
def universe():
    paths = Paths()
    if not paths.facts.exists() or not paths.meta.exists():
        pytest.skip("facts.parquet/meta.parquet not built; run build_facts.py first")
    return gate0.load_universe(paths)


@pytest.fixture(scope="module")
def universe_zeroed():
    """The same universe with --assume-absent-zero, for the debt-free case."""
    paths = Paths()
    if not paths.facts.exists() or not paths.meta.exists():
        pytest.skip("facts.parquet/meta.parquet not built; run build_facts.py first")
    return gate0.load_universe(paths, assume_absent_zero=True)


@pytest.fixture(scope="module")
def annual_facts():
    paths = Paths()
    if not paths.facts.exists():
        pytest.skip("facts.parquet not built; run build_facts.py first")
    return pl.read_parquet(paths.facts).filter(pl.col("fiscal_period") == "FY")


def company(universe, ticker, by_cik=None):
    """Look up a company by ticker, or by CIK when it is absent from the ticker map."""
    row = universe.filter(pl.col("cik") == by_cik) if by_cik else universe.filter(
        pl.col("ticker") == ticker
    )
    assert row.height == 1, f"{ticker}: expected 1 row, got {row.height}"
    return row.to_dicts()[0]


def test_mcri_tangible_book_and_quality(universe):
    """Monarch Casino, FY2025: hand-checked against the filing."""
    row = company(universe, "MCRI")

    assert row["cik"] == 907242
    assert row["latest_fiscal_year"] == 2025
    assert row["tangible_book"] == pytest.approx(510.7 * MILLION, rel=0.02)
    assert row["income_quality"] == pytest.approx(1.63, rel=0.03)
    assert row["sbc_pct_revenue"] == pytest.approx(0.015, abs=0.004)
    assert row["fail_tangible_book"] is False


def test_mcri_is_debt_free_so_net_cash_needs_the_opt_in(universe, universe_zeroed):
    """Monarch reports no debt tag at all, which is why net cash is strict-null.

    The company genuinely has no borrowings -- only operating leases -- so no
    tag in the total_debt chain is ever filed. Under the default rule an absent
    tag is unknown rather than zero, so net cash is unknowable; it only becomes
    the (correct, positive) figure once --assume-absent-zero is opted into.
    """
    strict = company(universe, "MCRI")
    assert strict["cash"] > 0
    assert strict["total_debt"] is None
    assert strict["net_cash"] is None

    zeroed = company(universe_zeroed, "MCRI")
    assert zeroed["net_cash"] == pytest.approx(strict["cash"])
    assert zeroed["net_cash"] > 0


def test_mcri_goodwill_is_unchanged_across_five_years(annual_facts):
    """Goodwill of $25.11M, flat FY2021-FY2025, is the tell of no acquisitions."""
    goodwill = annual_facts.filter(
        (pl.col("cik") == 907242)
        & (pl.col("concept") == "goodwill")
        & (pl.col("fiscal_year").is_between(2021, 2025))
    ).sort("fiscal_year")

    assert goodwill.height == 5
    for value in goodwill["value"]:
        assert value == pytest.approx(25.11 * MILLION, rel=0.01)
    assert goodwill["source_tag"].unique().to_list() == ["Goodwill"]


def test_prgs_negative_tangible_book_fails(universe):
    """Progress Software, FY2025: tangible book about -$1,415M."""
    row = company(universe, "PRGS")

    assert row["latest_fiscal_year"] == 2025
    assert row["tangible_book"] == pytest.approx(-1415 * MILLION, rel=0.03)
    assert row["fail_tangible_book"] is True
    assert row["gate0_pass"] is False


def test_cnxc_negative_tangible_book_fails(universe):
    """Concentrix, FY2025: tangible book about -$2,888M."""
    row = company(universe, "CNXC")

    assert row["latest_fiscal_year"] == 2025
    assert row["tangible_book"] == pytest.approx(-2888 * MILLION, rel=0.03)
    assert row["fail_tangible_book"] is True
    assert row["gate0_pass"] is False


def test_february_year_end_filer_is_present(universe):
    """APOG must appear at all.

    This is the regression test for the frames-API calendar bug: a
    calendar-aligned pipeline drops every filer whose year does not end near
    31 December, and Apogee ends in February.
    """
    row = company(universe, "APOG")

    # Apogee closes on the Saturday nearest end-February, so the year-end date
    # straddles the month boundary (0301 / 0302 / 0228 depending on the year).
    # What matters is that it is nowhere near December and the row is populated.
    assert not row["fiscal_year_end"].startswith("12"), row["fiscal_year_end"]
    assert row["fiscal_year_end"][:2] in ("02", "03"), row["fiscal_year_end"]
    assert row["latest_fiscal_year"] >= 2025
    assert row["revenue"] is not None


def test_off_cycle_filers_are_not_a_rounding_error(universe):
    """Non-December year-ends are a large minority, not a tail worth dropping."""
    with_fye = universe.filter(pl.col("fiscal_year_end").is_not_null())
    off_cycle = with_fye.filter(~pl.col("fiscal_year_end").str.starts_with("12"))

    assert with_fye.height > 1000
    assert off_cycle.height / with_fye.height > 0.15


def test_source_tag_is_recorded_for_every_fact(annual_facts):
    """A silently substituted tag is the main failure mode; it must be visible."""
    assert annual_facts["source_tag"].null_count() == 0


# --------------------------------------------------------------------------
# Follow-up fixes: widened equity chain, three-state tests, imputed gating
# --------------------------------------------------------------------------


def test_krp_lp_equity_is_visible_after_the_partners_capital_tag(universe):
    """Kimbell Royalty Partners is an LP; StockholdersEquity is never filed.

    Before the equity chain grew PartnersCapital / PartnersCapitalIncluding...
    fallbacks, every LP and LLC in the universe had a null equity and an
    untestable tangible-book -- this is the concrete instance of that.
    """
    row = company(universe, "KRP")

    assert row["equity"] is not None
    assert row["source_tag_equity"] in (
        "PartnersCapital",
        "PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest",
    )


def test_bank_missing_operating_income_is_not_evaluable_not_a_fail(universe):
    """JPMorgan reports no OperatingIncomeLoss subtotal, as banks generally don't.

    The old boolean fail_ni_over_oi collapsed "failed the test" and "the test
    could not run" into the same null. The three-state test_ni_vs_oi keeps
    them apart: a bank is NOT_EVALUABLE, never FAIL, on this test.
    """
    row = company(universe, "JPM")

    assert row["operating_income"] is None
    assert row["test_ni_vs_oi"] == "NOT_EVALUABLE"
    assert "ni_vs_oi" in row["gate0_not_evaluable"].split(",")


def test_imputed_field_blocks_pass_without_allow_imputed(universe_zeroed):
    """MCRI has no debt tag at all; --assume-absent-zero imputes total_debt.

    A row with a non-empty imputed_fields must never show gate0_pass True
    unless --allow-imputed was explicitly passed (it defaults to off).
    """
    row = company(universe_zeroed, "MCRI")

    assert row["imputed_fields"] != ""
    assert "total_debt" in row["imputed_fields"].split(",")
    assert row["gate0_pass"] is False


def test_allow_imputed_lets_an_otherwise_clean_row_pass(universe):
    """The same row, with --assume-absent-zero and --allow-imputed both on."""
    paths = Paths()
    zeroed_allowed = gate0.load_universe(
        paths, assume_absent_zero=True, allow_imputed=True
    )
    row = company(zeroed_allowed, "MCRI")

    assert row["imputed_fields"] != ""
    assert row["test_tangible_book"] == "PASS"
    assert row["test_income_quality"] == "PASS"
    assert row["test_fcf"] == "PASS"
    assert row["gate0_pass"] is True


# --------------------------------------------------------------------------
# Round 2: goodwill/intangibles resolved by inference, not assumption
# --------------------------------------------------------------------------


def _annual_row(cik, fiscal_year, period_end, **overrides):
    """One minimal per-year row, in the shape resolve_goodwill_intangibles reads."""
    row = {
        "cik": cik,
        "fiscal_year": fiscal_year,
        "period_end": period_end,
        "equity": None,
        "goodwill": None,
        "intangibles": None,
        "acquisitions": None,
    }
    row.update(overrides)
    return row


def test_tgt_structured_absence_passes_without_allow_imputed(universe):
    """Target reports Goodwill but no separate intangibles line (ASC 350).

    Substituted twice now: round-2 used DVN, which per-component resolution
    reclassifies as never_acquired (DVN's intangibles line was truly never
    filed and its M&A was stock-for-stock, generating no acquisitions-tag
    fact). The first round-3 draft used CDE, which extending the
    serial-acquirer guard to structured_absence correctly disqualified: CDE
    has real nonzero acquisitions on record and has never reported
    intangibles at all, so treating its absence as immaterial would be the
    same understatement the guard exists to prevent. TGT has no nonzero
    acquisitions on record and reports Goodwill normally -- a clean case.
    """
    row = company(universe, "TGT")

    assert row["resolution_basis"] == "structured_absence_asc350"
    assert row["resolution_basis_intangibles"] == "structured_absence_asc350"
    assert row["resolution_basis_goodwill"] == "reported"
    assert row["imputed_fields"] == "intangibles"
    assert row["intangibles"] == 0.0
    assert row["tangible_book"] == pytest.approx(row["equity"] - row["goodwill"])
    assert row["gate0_pass"] is True


def test_skyw_never_acquired_tangible_book_equals_equity(universe):
    """SkyWest has never reported goodwill or intangibles: it has never acquired.

    Hand-verified against the FY2025 filing: tangible book ~= +$2,746M, equal
    to equity since both goodwill and intangibles resolve to 0.
    """
    row = company(universe, "SKYW")

    assert row["resolution_basis"] == "never_acquired"
    assert row["tangible_book"] == pytest.approx(2746 * MILLION, rel=0.01)
    assert row["tangible_book"] == pytest.approx(row["equity"])
    assert row["gate0_pass"] is True


def test_serial_acquirer_missing_goodwill_is_not_never_acquired():
    """A real acquirer's missing goodwill tag must never read as never-acquired.

    This isolates the guard: goodwill/intangibles were never reported in any
    year (satisfying two of never_acquired's three conditions), but a genuine
    nonzero acquisition was. Without the guard this would be zero-filled as
    never_acquired -- the dangerous direction, understating a real acquirer.
    With the guard it is correctly excluded. There is no historical goodwill
    value in this fixture to carry forward either, so it lands unresolved --
    the safe fallback, and a real regression test that the guard fires.
    """
    annual = pl.DataFrame(
        [
            _annual_row(1, 2023, date(2023, 12, 31), equity=400.0, acquisitions=250.0),
            _annual_row(1, 2024, date(2024, 12, 31), equity=500.0, acquisitions=None),
        ]
    )
    latest = gate0.latest_rows(annual)

    resolved = gate0.resolve_goodwill_intangibles(annual, latest)
    row = resolved.to_dicts()[0]

    assert row["resolution_basis"] != "never_acquired"
    assert row["resolution_basis"] == "unresolved"
    assert row["tangible_book"] is None


def test_molson_coors_carried_forward_is_not_plain_pass(universe):
    """Molson Coors' goodwill and intangibles were reported historically, not now.

    Substituted for the round-2 DAN example: under per-component rules,
    structured_absence is checked before carry-forward for a component whose
    sibling is present, so DAN's missing goodwill (with intangibles present)
    now resolves via structured_absence_asc350 rather than carried_forward --
    real fact of DAN's data, verified against facts.parquet. Molson Coors
    (TAP-A) is a clean carried-forward case: BOTH goodwill and intangibles are
    absent at the latest period end (so neither can borrow structured absence
    from the other), and both were reported historically.
    """
    row = company(universe, "TAP-A")

    assert row["resolution_basis"] == "carried_forward"
    assert row["resolution_basis_goodwill"] == "carried_forward"
    assert row["resolution_basis_intangibles"] == "carried_forward"
    assert row["carried_forward_fields"] == "goodwill,intangibles"
    assert row["carry_forward_age_days"] is not None
    assert row["gate0_status"] not in ("pass", "fail")
    assert row["gate0_status"] in ("pass_stale", "fail_stale")


def test_stale_goodwill_past_two_years_stays_unresolved():
    """A carried-forward balance older than 730 days is not evidence about today."""
    annual = pl.DataFrame(
        [
            _annual_row(2, 2020, date(2020, 12, 31), equity=400.0, goodwill=100.0,
                       intangibles=20.0),
            _annual_row(2, 2024, date(2024, 12, 31), equity=500.0),
        ]
    )
    latest = gate0.latest_rows(annual)

    resolved = gate0.resolve_goodwill_intangibles(annual, latest)
    row = resolved.to_dicts()[0]

    assert row["resolution_basis"] == "unresolved"
    assert row["carried_forward_fields"] == ""
    assert row["tangible_book"] is None


# --------------------------------------------------------------------------
# Round 3: per-component resolution, duplicate CIKs, fcf vs fcf_after_sbc
# --------------------------------------------------------------------------


def test_cprx_per_component_resolution(universe):
    """Catalyst Pharmaceuticals: never filed Goodwill, reports Intangibles.

    Hand-verified against the FY2025 filing: tangible book ~= $822M. This is
    the worked example from the spec -- company-level never_acquired (round
    2) required BOTH components absent; per-component resolution lets goodwill
    resolve on its own history while intangibles is read directly.

    Looked up by CIK, not ticker: CPRX is absent from SEC's ticker map (see
    the README note on Catalyst Pharmaceuticals) though fully present by CIK.
    """
    row = company(universe, "CPRX", by_cik=1369568)

    assert row["resolution_basis_goodwill"] == "never_acquired"
    assert row["resolution_basis_intangibles"] == "reported"
    assert row["goodwill"] == 0.0
    assert row["intangibles"] == pytest.approx(131674000.0, rel=0.01)
    assert row["tangible_book"] == pytest.approx(822 * MILLION, rel=0.01)


def test_component_level_guard_does_not_weaken_at_the_goodwill_level():
    """The serial-acquirer guard, re-verified after moving to per-component rules.

    A nonzero acquisition in history must keep the GOODWILL component out of
    never_acquired even when intangibles resolves normally (reported) for the
    same company -- the two components must not contaminate each other's
    guard.
    """
    annual = pl.DataFrame(
        [
            _annual_row(3, 2023, date(2023, 12, 31), equity=400.0, intangibles=20.0,
                       acquisitions=250.0),
            _annual_row(3, 2024, date(2024, 12, 31), equity=500.0, intangibles=25.0,
                       acquisitions=None),
        ]
    )
    latest = gate0.latest_rows(annual)

    resolved = gate0.resolve_goodwill_intangibles(annual, latest)
    row = resolved.to_dicts()[0]

    assert row["resolution_basis_goodwill"] != "never_acquired"
    assert row["resolution_basis_goodwill"] == "unresolved"
    assert row["resolution_basis_intangibles"] == "reported"
    assert row["tangible_book"] is None


def test_exxon_duplicate_cik_collapses_to_one_row(tmp_path):
    """Exxon's ticker (CIK 2115436) carries no facts; CIK 34088 has them all.

    Exactly one row must survive, carrying the ticker, with plain FCF
    computable from the real filer's own OCF and capex (hand-verified FY2025:
    ocf $51,970M, capex $28,358M). The dropped sibling must be visible in
    duplicate_filers.csv, not silently gone.
    """
    paths = Paths()
    universe = gate0.load_universe(paths)
    kept, dropped = gate0.deduplicate_by_company_name(universe)

    matches = kept.filter(pl.col("company_name") == "Exxon Mobil Corporation")
    assert matches.height == 1
    row = matches.to_dicts()[0]
    assert row["cik"] == 34088
    assert row["ticker"] == "XOM"
    assert row["fcf"] == pytest.approx((51970 - 28358) * MILLION, rel=0.01)
    assert "2115436" in row["merged_from_ciks"].split(",")

    out = tmp_path / "duplicate_filers.csv"
    gate0.write_duplicate_filers(dropped, out)
    report = pl.read_csv(out)
    assert (report["cik"] == 2115436).any()


def test_exxon_sbc_unverified_still_scores(universe):
    """Exxon never tags SBC as a cash-flow line: fcf_after_sbc is NOT_EVALUABLE,
    but the row is scored, not unknown."""
    kept, _ = gate0.deduplicate_by_company_name(universe)
    row = company(kept, "XOM")

    assert row["sbc"] is None
    assert row["sbc_unverified"] is True
    assert row["test_fcf_after_sbc"] == "NOT_EVALUABLE"
    assert row["test_fcf"] == "PASS"
    assert row["gate0_status"] != "unknown"


def test_software_company_sbc_available_still_gates_on_fcf_after_sbc():
    """A negative fcf_after_sbc still blocks a pass when SBC is genuinely known.

    Regression guard for Task 3: only sbc_unverified (SBC unavailable) exempts
    fcf_after_sbc from gating. When SBC is reported, a company whose SBC
    add-back consumes its cash flow must still fail, exactly as under the
    original universal fcf_after_sbc rule -- Task 3 must not quietly weaken
    the test where SBC materiality is exactly the point (software filers).
    """
    frame = _one_row(
        equity=500.0, goodwill=0.0, intangibles=0.0,
        ocf=100.0, net_income=50.0, capex=20.0, sbc=90.0,
    )
    metrics = gate0.compute_metrics(frame)
    flagged = gate0.add_flags(metrics)
    verdict = gate0.add_verdict(flagged)
    row = verdict.to_dicts()[0]

    assert row["sbc_unverified"] is False
    assert row["test_fcf"] == "PASS"
    assert row["test_fcf_after_sbc"] == "FAIL"
    assert row["gate0_pass"] is False
    assert row["gate0_status"] == "fail"


# --------------------------------------------------------------------------
# Round 4: IFRS / foreign-filer coverage
# --------------------------------------------------------------------------


def test_bam_computes_cleanly_but_is_excluded_by_the_financials_sic_filter(universe):
    """Brookfield Asset Management, hand-diagnosed against the raw archive.

    The spec's diagnosis was that BAM is invisible because it files a 40-F
    under ifrs-full. Checked directly against companyfacts.zip: BAM's facts
    are tagged us-gaap, not ifrs-full, and its Revenues/Goodwill/
    StockholdersEquity history comes through form 10-K, not 40-F (Brookfield
    elected US domestic-filer treatment). It was never a taxonomy or form
    gap -- it resolves a computed tangible_book and income_quality here, in
    the raw (pre-SIC-filter) universe, exactly like any other company.

    The actual reason it is absent from a default `python gate0.py` run is
    SIC 6282 (Investment Advice), inside the default financials exclusion
    (6000-6799) -- the same, working-as-designed exclusion that hides every
    bank and insurer. `--include-financials` already surfaces it today.
    """
    row = company(universe, "BAM")

    assert row["sic"] == "6282"
    assert row["taxonomy"] == "us-gaap"
    assert row["tangible_book"] is not None
    assert row["income_quality"] is not None
    assert row["gate0_pass"] is True


def test_ifrs_serial_acquirer_guard_matches_us_gaap_behavior(universe):
    """Kinross Gold (20-F, ifrs-full): real M&A, no goodwill tag, ever.

    Verified against facts.parquet: Kinross recorded four years of nonzero
    ifrs-full acquisitions facts (up to $1.03B in FY2022) via
    CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinesses..., yet
    has never filed a Goodwill or IntangibleAssetsOtherThanGoodwill tag. The
    serial-acquirer guard must route this to unresolved, not never_acquired,
    exactly as it would for a us-gaap filer with the same fact pattern (see
    test_component_level_guard_does_not_weaken_at_the_goodwill_level).
    """
    row = company(universe, "KGC")

    assert row["taxonomy"] == "ifrs-full"
    assert row["goodwill"] is None
    assert row["resolution_basis_goodwill"] != "never_acquired"
    assert row["resolution_basis_goodwill"] == "unresolved"
    assert row["tangible_book"] is None


# --------------------------------------------------------------------------
# Round 4: growth_basis / short_history, --price-csv, quarterly acceleration
# --------------------------------------------------------------------------


def test_ge_vernova_spinoff_has_short_history_not_a_silent_drop(universe):
    """GE Vernova, spun off from GE in 2024, has under 5 fiscal years on file.

    Under the strict 5-year requirement this row's fcf_per_share_cagr_5y is
    null with no explanation attached. short_history/growth_basis make that
    explicit instead of leaving the caller to guess why a Gate-0-clean name
    has no growth figure.
    """
    row = company(universe, "GEV")

    assert row["short_history"] is True
    assert row["fcf_per_share_cagr_5y"] is None
    assert row["growth_basis"] == "insufficient"


def test_shopify_recovers_growth_via_3y_fallback(universe):
    """Shopify has under 5 years of FCF-per-share history but has 3.

    growth_basis must report which window the number actually rests on --
    "3y" here, not a bare null indistinguishable from "no data at all".
    """
    row = company(universe, "SHOP")

    assert row["fcf_per_share_cagr_5y"] is None
    assert row["fcf_per_share_cagr_3y"] is not None
    assert row["growth_basis"] == "3y"


def test_price_csv_derives_market_cap_ev_and_leaves_missing_debt_null(tmp_path):
    """--price-csv computes market_cap from the store's own shares_diluted.

    SKYW has both total_debt and cash on file, so ev/ev_fcf_after_sbc compute.
    MCRI genuinely has no debt tag (see test_mcri_is_debt_free_so_net_cash_
    needs_the_opt_in) -- ev must stay null there, never treat the missing
    debt as zero to manufacture a number.
    """
    price_csv = tmp_path / "prices.csv"
    price_csv.write_text("ticker,price,ma_200\nMCRI,85.50,80.00\nSKYW,95.00,90.00\n")

    exit_code = gate0.main(
        [
            "--tickers",
            "MCRI,SKYW",
            "--price-csv",
            str(price_csv),
            "--out",
            str(tmp_path / "gate0.csv"),
        ]
    )
    assert exit_code == 0

    result = pl.read_csv(tmp_path / "gate0.csv")
    skyw = result.filter(pl.col("ticker") == "SKYW").to_dicts()[0]
    mcri = result.filter(pl.col("ticker") == "MCRI").to_dicts()[0]

    assert skyw["market_cap"] == pytest.approx(95.0 * skyw["shares_diluted"])
    assert skyw["ev"] is not None
    assert skyw["pct_vs_200ma"] == pytest.approx((95.0 - 90.0) / 90.0)

    assert mcri["market_cap"] == pytest.approx(85.5 * mcri["shares_diluted"])
    assert mcri["total_debt"] is None
    assert mcri["ev"] is None


def test_palantir_nine_quarter_accelerating_revenue_streak(universe):
    """Palantir: hand-verified YoY revenue growth rising every quarter on file
    (48% -> 63% -> 85% -> 93% over the four most recent), a 9-quarter streak
    and a positive 4-quarter slope."""
    row = company(universe, "PLTR")

    assert row["revenue_growth_yoy_q1"] == pytest.approx(0.48, abs=0.02)
    assert row["revenue_growth_yoy_q4"] == pytest.approx(0.93, abs=0.02)
    assert row["revenue_growth_yoy_q4"] > row["revenue_growth_yoy_q1"]
    assert row["revenue_accel_4q"] > 0
    assert row["quarters_of_accelerating_revenue"] >= 4


# --------------------------------------------------------------------------
# Round 5: IFRS capex gap (Copa Holdings)
# --------------------------------------------------------------------------


def test_copa_holdings_resolves_capex_and_scores(universe):
    """Copa Holdings (20-F, ifrs-full) reports capex under a combined
    PP&E+intangibles+investment-property tag, not the narrower PP&E-only one.

    Hand-verified against the FY2025 filing: ocf $1,150.4M (was already
    resolving), capex $815.726M (was null before this round). Real fcf is
    now computable and the row scores instead of sitting unknown.
    """
    row = company(universe, "CPA")

    assert row["taxonomy"] == "ifrs-full"
    assert row["ocf"] == pytest.approx(1150.436 * MILLION, rel=0.001)
    assert row["capex"] == pytest.approx(815.726 * MILLION, rel=0.001)
    assert row["fcf"] == pytest.approx((1150.436 - 815.726) * MILLION, rel=0.001)
    assert row["gate0_status"] != "unknown"
    assert row["growth_basis"] != "insufficient"


# --------------------------------------------------------------------------
# Round 5: screen.py -- margin-expansion (PERF) bug fix, exact ticker
# matching (Copa/Copart), and the inflection lane
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gate0_csv_frame():
    paths = Paths()
    if not paths.gate0.exists():
        pytest.skip("gate0.csv not built; run gate0.py first")
    return pl.read_csv(paths.gate0, infer_schema_length=200000)


def test_perf_fails_margin_expansion_screen(gate0_csv_frame):
    """Perfect Corp (PERF): operating margin improved (+7.0pp) but is still
    negative (-2.5%) at the latest fiscal year.

    This is the exact 2026-08-07 bug: a hand-written margin-expansion screen
    tested only operating_margin_delta > 0 and let PERF through. Both
    conditions are mandatory in apply_margin_expansion; this pins that a
    still-unprofitable "improver" is rejected, not shortlisted.
    """
    row = gate0_csv_frame.filter(pl.col("ticker") == "PERF").to_dicts()[0]
    assert row["operating_margin_delta"] > 0
    assert row["operating_margin_latest"] < 0

    survivors, rejected = screen.apply_margin_expansion(gate0_csv_frame)
    assert survivors.filter(pl.col("ticker") == "PERF").height == 0
    perf_rejection = rejected.filter(pl.col("ticker") == "PERF")
    assert perf_rejection.height == 1
    assert perf_rejection["rejected_because"][0] == "margin_expansion"


def test_ticker_lookup_is_exact_never_a_copa_copart_substring(gate0_csv_frame):
    """Copa Holdings (CIK 1345105, ticker CPA) and Copart (CIK 900075, ticker
    CPRT) are the real pair behind the 2026-08-07 incident: a substring
    lookup on "COPA" matched both, and Copart's financials were read as
    Copa's for several minutes.

    Note: Copa's actual ticker is CPA, not "COPA" (verified against SEC's
    ticker map) -- the spec's literal wording doesn't match the real symbol,
    so this test uses the real one. What matters, and what is tested
    directly, is that lookup_by_ticker_or_cik is exact-match-only: it never
    returns Copart for any Copa-shaped query, by ticker or by CIK.
    """
    by_ticker = screen.lookup_by_ticker_or_cik(gate0_csv_frame, "CPA")
    assert by_ticker.height == 1
    assert by_ticker["cik"][0] == 1345105
    assert 900075 not in by_ticker["cik"].to_list()

    by_cik = screen.lookup_by_ticker_or_cik(gate0_csv_frame, "1345105")
    assert by_cik.height == 1
    assert by_cik["cik"][0] == 1345105

    # The literal spec string never matches anything -- exact match only,
    # no fuzzy/prefix fallback that could re-open the substring hole.
    literal = screen.lookup_by_ticker_or_cik(gate0_csv_frame, "COPA")
    assert literal.height == 0


def test_fcx_appears_in_inflection_lane_not_main_lane(gate0_csv_frame):
    """Freeport-McMoRan (FCX): fcf_per_share_cagr_3y is negative (-42.9%,
    volatility within the window produces a negative geometric rate) even
    though the company moved from -$0.85 to +$0.69 FCF/share over 5
    consecutive positive years -- delta_abs +$1.54. A CAGR-only growth
    screen rejects it; the inflection lane's substituted test does not.
    """
    row = gate0_csv_frame.filter(pl.col("ticker") == "FCX").to_dicts()[0]
    assert row["fcf_inflection"] is True
    assert row["fcf_per_share_delta_abs"] > 0

    class _Args:
        include_financials = False
        exclude_sic = gate0.DEFAULT_EXCLUDE_SIC
        min_revenue = screen.DEFAULT_MIN_REVENUE
        min_mktcap = None
        max_mktcap = None
        lane = "main"

    main_result, _ = screen.run_screen(gate0_csv_frame, _Args(), set(), None, None)
    main_shortlist = main_result.filter(pl.col("rejected_because") == "")["ticker"].to_list()
    assert "FCX" not in main_shortlist

    args_inflection = _Args()
    args_inflection.lane = "inflection"
    inflection_result, _ = screen.run_screen(
        gate0_csv_frame, args_inflection, set(), None, None
    )
    inflection_shortlist = inflection_result.filter(pl.col("rejected_because") == "")[
        "ticker"
    ].to_list()
    assert "FCX" in inflection_shortlist
