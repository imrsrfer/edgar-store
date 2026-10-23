"""F-06: exact issuer identity, with name collisions retained for review."""

import polars as pl
import pytest
from polars.testing import assert_frame_equal

import gate0


def filer(cik, name, *, ticker=None, facts=False, **metadata):
    row = {
        "cik": cik,
        "company_name": name,
        "ticker": ticker,
        "exchange": None,
        "sic": None,
        "sic_description": None,
        "latest_fiscal_year": 2025,
        **{column: 100.0 if facts else None for column in gate0.DEDUP_CONCEPTS},
    }
    row.update(metadata)
    return row


def frame(*rows):
    return pl.DataFrame(rows, schema_overrides={
        "cik": pl.Int64, "company_name": pl.String, "ticker": pl.String,
        "exchange": pl.String, "sic": pl.String, "sic_description": pl.String,
        **{column: pl.Float64 for column in gate0.DEDUP_CONCEPTS},
    })


@pytest.mark.parametrize("other_name", ["ACME, INC.", "  acme   inc  "])
def test_f06_name_collision_keeps_both_financials_and_metadata(other_name):
    original = frame(
        filer(101, "Acme Inc", facts=True),
        filer(202, other_name, ticker="OTHER", exchange="NYSE", sic="1234",
              sic_description="Other business"),
    )
    kept, report = gate0.dedupe_cross_cik(original)

    assert_frame_equal(kept.select(original.columns).sort("cik"), original.sort("cik"))
    assert kept["merged_from_ciks"].to_list() == ["", ""]
    assert set(report["cik"]) == {101, 202}
    assert report["report_type"].to_list() == ["review_candidate"] * 2
    assert report["merged_into_cik"].null_count() == 2
    assert set(report["review_candidate_ciks"]) == {"101,202"}
    assert set(report["reason"]) == {
        "normalized_name_collision_without_explicit_cik_relationship"
    }


def test_f06_successor_with_different_name_merges_and_fills_only_metadata():
    original = frame(
        filer(34088, "Exxon Mobil Corporation", facts=True),
        filer(2115436, "ExxonMobil Holdings Corp", ticker="XOM", exchange="NYSE",
              sic="2911", sic_description="Petroleum refining"),
    )
    kept, report = gate0.dedupe_cross_cik(original)

    assert kept.height == 1
    row = kept.row(0, named=True)
    assert row["cik"] == 34088
    assert row["ticker"] == "XOM"
    assert row["exchange"] == "NYSE"
    assert row["sic"] == "2911"
    assert row["sic_description"] == "Petroleum refining"
    assert row["merged_from_ciks"] == "2115436"
    assert all(row[column] == 100.0 for column in gate0.DEDUP_CONCEPTS)
    assert report["report_type"].to_list() == ["explicit_merge"]
    assert report["merged_into_cik"].to_list() == [34088]
    assert report["reason"].to_list() == ["explicit_issuer_successor_relationship"]


def test_f06_explicit_pair_and_unrelated_same_name_are_distinct_in_csv(tmp_path):
    original = frame(
        filer(34088, "Exxon Mobil Corporation", facts=True),
        filer(2115436, "ExxonMobil Holdings Corp", ticker="XOM"),
        filer(999, "EXXON MOBIL CORPORATION", ticker="OTHER", facts=True),
    )
    kept, report = gate0.dedupe_cross_cik(original)
    assert set(kept["cik"]) == {34088, 999}
    assert kept.filter(pl.col("cik") == 999)["ticker"].item() == "OTHER"

    output = tmp_path / "duplicate_filers.csv"
    assert gate0.write_duplicate_filers(report, output) == 3
    saved = pl.read_csv(output)
    merges = saved.filter(pl.col("report_type") == "explicit_merge")
    candidates = saved.filter(pl.col("report_type") == "review_candidate")
    assert merges["cik"].to_list() == [2115436]
    assert set(candidates["cik"]) == {34088, 999}
    assert candidates["merged_into_cik"].null_count() == 2
    assert candidates["reason"].str.contains("normalized_name_collision").all()


def test_f06_ticker_match_alone_never_authorizes_cross_cik_merge():
    original = frame(filer(1, "One", ticker="SAME"), filer(2, "Two", ticker="SAME"))
    kept, report = gate0.dedupe_cross_cik(original)
    assert_frame_equal(kept.select(original.columns).sort("cik"), original)
    assert report.is_empty()


@pytest.mark.parametrize("name", [None, "", "   "])
def test_f06_missing_names_neither_merge_nor_generate_review_candidates(name):
    original = frame(filer(1, name), filer(2, name, ticker="TWO"))
    kept, report = gate0.dedupe_cross_cik(original)
    assert kept.height == 2
    assert report.is_empty()


def test_f06_explicit_merge_is_deterministic_and_preserves_existing_metadata():
    original = frame(
        filer(34088, "One", facts=True, ticker="PRIMARY", sic="2911"),
        filer(2115436, "Two", facts=True, ticker="SECOND", sic="9999"),
    )
    kept, report = gate0.dedupe_cross_cik(original)
    reversed_kept, reversed_report = gate0.dedupe_cross_cik(original.reverse())
    assert_frame_equal(kept, reversed_kept)
    assert_frame_equal(report, reversed_report)
    assert kept["cik"].to_list() == [34088]
    assert kept["ticker"].to_list() == ["PRIMARY"]
    assert kept["sic"].to_list() == ["2911"]


def test_f06_empty_input_has_writable_report_and_legacy_caller_uses_safe_path(tmp_path):
    original = frame(filer(1, "One")).head(0)
    kept, report = gate0.deduplicate_by_company_name(original)
    assert kept.is_empty()
    assert report.is_empty()
    assert gate0.deduplicate_by_company_name is gate0.dedupe_cross_cik
    output = tmp_path / "duplicate_filers.csv"
    assert gate0.write_duplicate_filers(report, output) == 0
    assert {"report_type", "reason", "merged_into_cik", "review_candidate_ciks"} <= set(
        pl.read_csv(output).columns
    )
