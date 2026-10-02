"""store_defects.py: each detector fires on the case that proved it and stays
quiet on the honest shape beside it. Synthetic facts first; the named live cases
last, skipped when no store is built."""

from datetime import date

import polars as pl
import pytest

import store_defects as sd

FACT_SCHEMA = {
    "cik": pl.Int64,
    "concept": pl.Utf8,
    "source_tag": pl.Utf8,
    "fiscal_period": pl.Utf8,
    "period_start": pl.Date,
    "period_end": pl.Date,
    "value": pl.Float64,
    "filed": pl.Date,
}


def _fy(cik, concept, year, value, tag="Tag", end_md=(12, 31), filed=None):
    end = date(year, *end_md)
    return {
        "cik": cik,
        "concept": concept,
        "source_tag": tag,
        "fiscal_period": "FY",
        "period_start": date(year - 1, end_md[0], end_md[1]) if end_md != (12, 31) else date(year, 1, 1),
        "period_end": end,
        "value": float(value),
        "filed": filed or date(year + 1, 2, 20),
    }


def _q(cik, concept, end, value, tag="Tag", fp="Q2", filed=None):
    return {
        "cik": cik,
        "concept": concept,
        "source_tag": tag,
        "fiscal_period": fp,
        "period_start": None,
        "period_end": end,
        "value": float(value),
        "filed": filed or end,
    }


def _facts(rows):
    return pl.DataFrame(rows, schema=FACT_SCHEMA)


def _frame(rows):
    base = {
        "cik": pl.Int64,
        "period_end": pl.Date,
        "shares_diluted": pl.Float64,
        "latest_q_shares_diluted": pl.Float64,
        "ttm_window_end": pl.Date,
        "filing_form": pl.Utf8,
        "ttm_side_flows_lagging": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=base)


def _row(cik, **kw):
    out = {
        "cik": cik,
        "period_end": date(2025, 12, 31),
        "shares_diluted": 1e8,
        "latest_q_shares_diluted": 1e8,
        "ttm_window_end": date(2026, 6, 30),
        "filing_form": "10-Q",
        "ttm_side_flows_lagging": "",
    }
    out.update(kw)
    return out


def _one(result, cik, column):
    return result.filter(pl.col("cik") == cik)[column].item()


# ---------------------------------------------------------------- total debt


def test_partial_debt_component_is_flagged_and_full_sum_is_not():
    facts = _facts(
        [
            _fy(1, "ocf", 2025, 10),
            _fy(1, "total_debt", 2025, 2e6, tag="LongTermDebtCurrent (partial)"),
            _fy(2, "ocf", 2025, 10),
            _fy(2, "total_debt", 2025, 3e9, tag="LongTermDebtNoncurrent+LongTermDebtCurrent"),
        ]
    )
    out = sd.add_store_defect_flags(_frame([_row(1), _row(2)]), facts)
    assert _one(out, 1, "total_debt_partial") is True  # UNFI shape
    assert _one(out, 2, "total_debt_partial") is False
    assert "total_debt_partial" in _one(out, 1, "store_defect_flags")


def test_partial_on_latest_quarter_alone_still_flags():
    facts = _facts(
        [
            _fy(1, "ocf", 2025, 10),
            _fy(1, "total_debt", 2025, 16e6, tag="LongTermDebtNoncurrent+LongTermDebtCurrent"),
            _q(1, "total_debt", date(2026, 6, 30), 4.9e6, tag="LongTermDebtCurrent (partial)"),
        ]
    )
    out = sd.add_store_defect_flags(_frame([_row(1)]), facts)
    assert _one(out, 1, "total_debt_partial") is True  # DIT shape


def test_no_debt_fact_is_null_not_false():
    facts = _facts([_fy(1, "ocf", 2025, 10)])
    out = sd.add_store_defect_flags(_frame([_row(1)]), facts)
    assert _one(out, 1, "total_debt_partial") is None


# ---------------------------------------------------------------- capex


def _fleet(cik, share, tag="ProceedsFromSaleOfPropertyPlantAndEquipment"):
    rows = [_fy(cik, "ocf", 2025, 1000)]
    for year in range(2021, 2026):
        rows.append(_fy(cik, "capex", year, 800))
        rows.append(_fy(cik, "investing_inflows", year, 800 * share, tag=tag))
    return rows


def test_recurring_equipment_sales_flag_and_net_capex():
    out = sd.add_store_defect_flags(_frame([_row(1)]), _facts(_fleet(1, 0.30)))
    assert _one(out, 1, "capex_gross_of_proceeds") is True  # KNX shape
    assert _one(out, 1, "ppe_sale_proceeds") == pytest.approx(240.0)
    assert _one(out, 1, "capex_net") == pytest.approx(560.0)


def test_immaterial_proceeds_do_not_flag():
    out = sd.add_store_defect_flags(_frame([_row(1)]), _facts(_fleet(1, 0.05)))
    assert _one(out, 1, "capex_gross_of_proceeds") is False
    assert _one(out, 1, "capex_net") == pytest.approx(760.0)


def test_mixed_inflow_sum_is_not_split():
    tag = "ProceedsFromSaleOfPropertyPlantAndEquipment+ProceedsFromDivestitureOfBusinesses"
    out = sd.add_store_defect_flags(_frame([_row(1)]), _facts(_fleet(1, 0.30, tag=tag)))
    # LII shape: the PP&E share of the sum is unknowable, so nothing is published.
    assert _one(out, 1, "ppe_sale_proceeds") is None
    assert _one(out, 1, "capex_net") is None
    assert _one(out, 1, "capex_gross_of_proceeds") is False


# ---------------------------------------------------------------- shares


def _share_history(cik, counts):
    rows = [_fy(cik, "ocf", 2025, 10)]
    for year, count in zip(range(2020, 2026), counts):
        rows.append(_fy(cik, "shares_diluted", year, count))
    return rows


def test_history_in_thousands_flags():
    counts = [29.2e3, 29.2e3, 29.2e3, 29.3e6, 29.4e6, 29.5e6]  # BMI shape
    out = sd.add_store_defect_flags(
        _frame([_row(1, shares_diluted=29.5e6, latest_q_shares_diluted=29.3e6)]),
        _facts(_share_history(1, counts)),
    )
    assert _one(out, 1, "shares_history_scale_break") is True


def test_fy_in_thousands_against_quarter_in_units_flags():
    counts = [44e3, 44e3, 45e3, 45e3, 44.9e3, 44.9e3]  # SWBI: consistent history
    out = sd.add_store_defect_flags(
        _frame([_row(1, shares_diluted=44_933, latest_q_shares_diluted=45_476_000)]),
        _facts(_share_history(1, counts)),
    )
    assert _one(out, 1, "shares_history_scale_break") is True


def test_ordinary_buybacks_do_not_flag():
    counts = [120e6, 115e6, 110e6, 104e6, 99e6, 95e6]
    out = sd.add_store_defect_flags(
        _frame([_row(1, shares_diluted=95e6, latest_q_shares_diluted=93e6)]),
        _facts(_share_history(1, counts)),
    )
    assert _one(out, 1, "shares_history_scale_break") is False


# ---------------------------------------------------------------- flows vs label


def test_label_a_year_ahead_of_flows_flags():
    facts = _facts([_fy(1, "ocf", 2025, 10, end_md=(2, 1))])  # KR: flows FY Feb-25
    out = sd.add_store_defect_flags(_frame([_row(1, period_end=date(2026, 1, 31))]), facts)
    assert _one(out, 1, "fy_flows_stale") is True
    assert _one(out, 1, "fy_flows_period_end") == date(2025, 2, 1)


def test_fye_change_gap_below_300_days_still_flags():
    facts = _facts([_fy(1, "ocf", 2025, 10, end_md=(7, 31))])  # FERG: 153 days
    out = sd.add_store_defect_flags(_frame([_row(1, period_end=date(2025, 12, 31))]), facts)
    assert _one(out, 1, "fy_flows_stale") is True


def test_aligned_label_does_not_flag_and_missing_ocf_is_null():
    facts = _facts([_fy(1, "ocf", 2025, 10), _fy(2, "revenue", 2025, 10)])
    out = sd.add_store_defect_flags(_frame([_row(1), _row(2)]), facts)
    assert _one(out, 1, "fy_flows_stale") is False
    assert _one(out, 2, "fy_flows_stale") is None


# ---------------------------------------------------------------- calendar


def _calendar_facts(as_of, newest=None):
    rows = [_fy(1, "ocf", 2025, 10, filed=as_of), _fy(2, "ocf", 2025, 10, filed=as_of)]
    if newest is not None:
        rows.append(_q(1, "ocf", newest, 5, filed=as_of))
    return _facts(rows)


def test_missing_10q_flags_against_store_as_of_not_wall_clock():
    as_of = date(2026, 9, 18)
    frame = _frame([_row(1, ttm_window_end=date(2026, 3, 31))])  # VMC
    out = sd.add_store_defect_flags(frame, _calendar_facts(as_of, newest=date(2026, 3, 31)))
    assert _one(out, 1, "ttm_behind_calendar") is True
    assert _one(out, 1, "store_as_of") == as_of


def test_10k_not_yet_due_does_not_flag():
    # UNFI: FY ends 2026-08-01, window ends 2026-05-02, the next report is the
    # 10-K. 139 days old is inside the annual allowance.
    frame = _frame(
        [_row(1, period_end=date(2025, 8, 2), ttm_window_end=date(2026, 5, 2))]
    )
    out = sd.add_store_defect_flags(frame, _calendar_facts(date(2026, 9, 18), newest=date(2026, 5, 2)))
    assert _one(out, 1, "ttm_behind_calendar") is False


def test_stale_binding_label_with_current_quarter_does_not_flag():
    # AVPT: ttm_window_end 2025-09-30 (one stale leg), newest quarter on file Jun-26.
    frame = _frame([_row(1, ttm_window_end=date(2025, 9, 30))])
    out = sd.add_store_defect_flags(frame, _calendar_facts(date(2026, 9, 18), newest=date(2026, 6, 30)))
    assert _one(out, 1, "ttm_behind_calendar") is False


def test_foreign_filer_is_not_tested():
    frame = _frame([_row(1, ttm_window_end=date(2025, 6, 30), filing_form="20-F")])
    out = sd.add_store_defect_flags(frame, _calendar_facts(date(2026, 9, 18)))
    assert _one(out, 1, "ttm_behind_calendar") is None


# ---------------------------------------------------------------- summary


def test_side_flow_lag_is_summarised_and_clean_row_is_empty():
    facts = _facts([_fy(1, "ocf", 2025, 10), _fy(2, "ocf", 2025, 10)])
    frame = _frame(
        [
            _row(1, ttm_side_flows_lagging="acquisitions,buybacks"),
            # window ends after the store's newest filing: nothing is behind
            _row(2, ttm_window_end=date(2026, 12, 31)),
        ]
    )
    out = sd.add_store_defect_flags(frame, facts)
    assert _one(out, 1, "store_defect_flags") == "ttm_side_flows_lagging"
    assert _one(out, 2, "store_defect_flags") == ""


# ---------------------------------------------------------------- live store


LIVE_CASES = {
    "UNFI": "total_debt_partial",
    "LII": "total_debt_partial",
    "PLOW": "total_debt_partial",
    "TMDX": "total_debt_partial",
    "KNX": "capex_gross_of_proceeds",
    "BMI": "shares_history_scale_break",
    "SWBI": "shares_history_scale_break",
    "RGEN": "shares_history_scale_break",
    "KR": "fy_flows_stale",
    "FERG": "fy_flows_stale",
    "VMC": "ttm_behind_calendar",
    "LSTR": "ttm_behind_calendar",
    "CTSH": "ttm_behind_calendar",
    "OKE": "ttm_side_flows_lagging",
}


@pytest.fixture(scope="module")
def live_rows():
    from edgar_lib import Paths
    from gate0 import load_universe

    paths = Paths()
    if not (paths.facts.exists() and paths.meta.exists()):
        pytest.skip("facts.parquet/meta.parquet not built; run build_facts.py first")
    return load_universe(paths)


def test_avpt_stale_binding_label_is_not_called_behind(live_rows):
    row = live_rows.filter(pl.col("ticker") == "AVPT")
    if row.is_empty():
        pytest.skip("AVPT not in this store")
    assert "ttm_behind_calendar" not in row["store_defect_flags"].item().split(",")


@pytest.mark.parametrize("ticker,flag", sorted(LIVE_CASES.items()))
def test_named_cases_fire_on_the_live_store(live_rows, ticker, flag):
    row = live_rows.filter(pl.col("ticker") == ticker)
    if row.is_empty():
        pytest.skip(f"{ticker} not in this store")
    assert flag in row["store_defect_flags"].item().split(","), (ticker, flag)
