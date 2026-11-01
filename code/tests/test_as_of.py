"""Point-in-time builds must filter filings before learning anything from them."""

import json
import zipfile
from datetime import date

import polars as pl
import pytest

import build_facts
from edgar_lib import Paths, read_manifest, write_manifest


CUT = date(2024, 12, 31)


def entry(value, filed, accn="original", end="2023-12-31", fy=2023):
    return {
        "val": value, "filed": filed, "accn": accn, "end": end,
        "fy": fy, "fp": "FY", "form": "10-K",
    }


def archive(paths, entries):
    payload = {
        "cik": 123, "entityName": "Historical Co",
        "facts": {"us-gaap": {"StockholdersEquity": {"units": {"USD": entries}}}},
    }
    with zipfile.ZipFile(paths.companyfacts_zip, "w") as handle:
        handle.writestr("CIK123.json", json.dumps(payload))
    return str(paths.companyfacts_zip), "CIK123.json"


def test_cut_drops_future_and_undated_facts_but_keeps_cut_date(tmp_path):
    task = archive(Paths(tmp_path).ensure(), [
        entry(100, "2024-12-31"),
        entry(200, "2025-01-01", "future", "2024-12-31", 2024),
        entry(300, None, "undated", "2022-12-31", 2022),
        entry(400, "invalid", "invalid", "2021-12-31", 2021),
    ])
    result = build_facts.process_member(task, as_of=CUT)
    assert [(r["value"], r["filed"]) for r in result["rows"]] == [(100, CUT)]
    live = build_facts.process_member(task)
    assert {r["value"] for r in live["rows"]} == {100, 200, 300, 400}


def test_restatement_uses_only_filings_available_at_cut(tmp_path):
    task = archive(Paths(tmp_path).ensure(), [
        entry(100, "2024-02-01"), entry(120, "2025-03-01", "amended"),
    ])
    historical = build_facts.process_member(task, as_of=CUT)["rows"]
    live = build_facts.process_member(task)["rows"]
    assert [(r["value"], r["restated"]) for r in historical] == [(100, False)]
    assert [(r["value"], r["restated"]) for r in live] == [(120, True)]


def test_future_filing_cannot_teach_an_earlier_period_label(tmp_path):
    # The original accession's primary end is 2023; its 2022 comparative has
    # no primary label until a later filing explicitly calls that end FY2021.
    task = archive(Paths(tmp_path).ensure(), [
        entry(100, "2024-02-01"),
        entry(90, "2024-02-01", end="2022-12-31"),
        entry(90, "2025-03-01", "later-label", "2022-12-31", 2021),
    ])
    historical = build_facts.process_member(task, as_of=CUT)["rows"]
    live = build_facts.process_member(task)["rows"]
    assert {r["fiscal_year"] for r in historical if r["value"] == 90} == {2022}
    assert {r["fiscal_year"] for r in live if r["value"] == 90} == {2021}


def test_future_invalid_fiscal_year_is_not_reported(tmp_path):
    task = archive(Paths(tmp_path).ensure(), [
        entry(100, "2024-02-01"), entry(200, "2025-03-01", "bad", fy=9999),
    ])
    assert build_facts.process_member(task, as_of=CUT)["invalid_fiscal_years"] == []
    assert build_facts.process_member(task)["invalid_fiscal_years"]


@pytest.mark.parametrize("explicit_root", [False, True])
def test_as_of_requires_explicit_non_live_root(explicit_root, capsys):
    args = ["--as-of", CUT.isoformat()]
    if explicit_root:
        args += ["--root", str(Paths().root / ".." / Paths().root.name)]
    with pytest.raises(SystemExit) as exc:
        build_facts.main(args)
    assert exc.value.code == 2
    assert "--as-of requires an explicit --root different from the live store" in capsys.readouterr().err


@pytest.mark.parametrize("cut", ["2024-02-30", "20241231", "2024-W01-1"])
def test_as_of_requires_iso_calendar_date(cut, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        build_facts.main(["--as-of", cut, "--root", str(tmp_path)])
    assert exc.value.code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


@pytest.mark.parametrize("historical", [False, True])
def test_cli_worker_cut_and_manifest_preserve_schemas(tmp_path, historical):
    paths = Paths(tmp_path).ensure()
    archive(paths, [entry(100, "2024-02-01"), entry(120, "2025-03-01", "amended")])
    original = {"companyfacts": {"path": "companyfacts.zip", "downloaded_utc": "2026-10-31T00:00:00Z"}}
    write_manifest(paths, original)
    args = ["--root", str(tmp_path), "--workers", "1"]
    assert build_facts.main(args + (["--as-of", CUT.isoformat()] if historical else [])) == 0
    facts = pl.read_parquet(paths.facts)
    assert facts.schema == build_facts.FACTS_SCHEMA
    assert pl.read_parquet(paths.meta).schema == build_facts.META_SCHEMA
    assert facts["value"].to_list() == ([100] if historical else [120])
    assert facts["restated"].to_list() == [not historical]
    manifest = read_manifest(paths)
    if historical:
        assert manifest["build:facts"]["as_of"] == CUT.isoformat()
        assert manifest["companyfacts"] == original["companyfacts"]
    else:
        assert manifest == original
