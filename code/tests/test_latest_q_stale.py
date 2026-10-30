"""latest_q_* must not publish an old balance sheet as the latest one, and the
TTM window label must say which leg binds it.

SBLK (2026-10-29): Star Bulk files its interims on 6-K. The only quarterly
balance-sheet instants in the store are from the 2021-06-30 6-K, so
build_latest_quarter published latest_q_equity / latest_q_cash from 2021 and a
null latest_q_total_debt beside a TTM window ending 2026-06-30. Nothing on the
row said the "latest quarter" was five years old.

BKTI / TXT (2026-10-29): ttm_window_end read one quarter behind the OCF and
capex figures. NOT an off-by-one: it is the documented BINDING window (the
earliest end in the FCF chain), and on both rows the SBC leg really ends a
quarter earlier. TXT tags no Q2 SBC at all. BKTI tags a discrete Q2 SBC that
the YTD rollforward cannot read -- an extraction gap, fixed at the source by
stepping the window forward one discrete quarter at a time.
"""
import datetime as dt

import polars as pl
import pytest

import gate0

D = dt.date.fromisoformat


def _fact(cik, concept, period, start, end, value):
    return {
        "cik": cik, "concept": concept, "fiscal_period": period,
        "period_start": D(start) if start else None,
        "period_end": D(end), "value": float(value),
    }


def _facts(rows):
    return pl.DataFrame(rows, schema={
        "cik": pl.Int64, "concept": pl.Utf8, "fiscal_period": pl.Utf8,
        "period_start": pl.Date, "period_end": pl.Date, "value": pl.Float64,
    })


def _ytd_chain(cik, concept, fy, ytd_prior, ytd_current):
    """FY2025 + a matching YTD2 pair: a rollforward window ending 2026-06-30."""
    return [
        _fact(cik, concept, "YTD2", "2025-01-01", "2025-06-30", ytd_prior),
        _fact(cik, concept, "FY", "2025-01-01", "2025-12-31", fy),
        _fact(cik, concept, "YTD2", "2026-01-01", "2026-06-30", ytd_current),
    ]


def _q1_chain(cik, concept, fy, q1_prior, q1_current):
    """FY2025 + a Q1 pair only: a rollforward window ending 2026-03-31."""
    return [
        _fact(cik, concept, "Q1", "2025-01-01", "2025-03-31", q1_prior),
        _fact(cik, concept, "FY", "2025-01-01", "2025-12-31", fy),
        _fact(cik, concept, "Q1", "2026-01-01", "2026-03-31", q1_current),
    ]


def _one(frame, cik, column):
    return frame.filter(pl.col("cik") == cik)[column][0]


# --------------------------------------------------------------------------
# (b) The window label: binding window unchanged, flows window published
# --------------------------------------------------------------------------

def test_txt_shape_keeps_binding_label_and_names_the_lagging_leg():
    """TXT: ocf/capex run to Q2, SBC only to Q1. The binding label stays on Q1
    (the SBC leg really ends there); the flows end and the binder are stated."""
    rows = (
        _ytd_chain(1, "ocf", 1312, 263, 187)
        + _ytd_chain(1, "capex", 383, 134, 228)
        + _q1_chain(1, "sbc", 81, 34, 40)
    )
    out = gate0.build_ttm(_facts(rows))
    assert _one(out, 1, "ttm_window_end") == D("2026-03-31")
    assert _one(out, 1, "ttm_flows_window_end") == D("2026-06-30")
    assert _one(out, 1, "ttm_window_binding_concepts") == "sbc"
    assert _one(out, 1, "ttm_sbc") == pytest.approx(81 - 34 + 40)


def test_bkti_shape_steps_sbc_forward_on_the_tagged_discrete_quarter():
    """BKTI: SBC has Q1 YTD pairs plus discrete Q2s in both years. The window
    steps forward one quarter (drop Q2'25, add Q2'26) and ends where the
    flows end: the window end equals the last quarter actually in the sum."""
    rows = (
        _ytd_chain(2, "ocf", 19441, 6002, 7461)
        + _ytd_chain(2, "capex", 1039, 519, 735)
        + _q1_chain(2, "sbc", 896, 275, 57)
        + [
            _fact(2, "sbc", "Q2", "2025-04-01", "2025-06-30", 299),
            _fact(2, "sbc", "Q2", "2026-04-01", "2026-06-30", 107),
        ]
    )
    out = gate0.build_ttm(_facts(rows))
    # Q3'25 + Q4'25 + Q1'26 + Q2'26 = (896 - 275 - 299) + 57 + 107
    assert _one(out, 2, "ttm_sbc") == pytest.approx(896 - 275 - 299 + 57 + 107)
    assert _one(out, 2, "ttm_window_start") == D("2025-07-01")
    assert _one(out, 2, "ttm_window_end") == D("2026-06-30")
    assert _one(out, 2, "ttm_flows_window_end") == D("2026-06-30")
    assert _one(out, 2, "ttm_window_binding_concepts") == ""


def test_discrete_step_needs_the_prior_year_quarter_it_drops():
    """Without Q2'25 the step cannot remove the quarter it replaces: no step,
    the binding label stays on Q1 and the figure is untouched."""
    rows = (
        _ytd_chain(3, "ocf", 100, 40, 50)
        + _q1_chain(3, "sbc", 12, 3, 4)
        + [_fact(3, "sbc", "Q2", "2026-04-01", "2026-06-30", 5)]
    )
    out = gate0.build_ttm(_facts(rows))
    assert _one(out, 3, "ttm_sbc") == pytest.approx(12 - 3 + 4)
    assert _one(out, 3, "ttm_window_end") == D("2026-03-31")
    assert _one(out, 3, "ttm_window_binding_concepts") == "sbc"


def test_capex_discrete_step_cannot_run_past_ocf():
    """Q2 capex facts alone cannot turn Q1 OCF 100 / capex 40 into FCF 89."""
    rows = (
        _q1_chain(30, "ocf", 100, 40, 40)
        + _q1_chain(30, "capex", 40, 20, 20)
        + _q1_chain(30, "sbc", 0, 0, 0)
        + [
            _fact(30, "capex", "Q2", "2025-04-01", "2025-06-30", 30),
            _fact(30, "capex", "Q2", "2026-04-01", "2026-06-30", 1),
        ]
    )
    out = gate0.build_ttm(_facts(rows))
    assert _one(out, 30, "ttm_capex") == pytest.approx(40)
    assert _one(out, 30, "ttm_flows_window_end") == D("2026-03-31")
    assert _one(out, 30, "ttm_window_end") == D("2026-03-31")
    assert _one(out, 30, "ttm_window_binding_concepts") == ""
    assert _one(out, 30, "ttm_fcf_after_sbc") == pytest.approx(60)


def test_fcf_stays_null_when_reported_capex_window_is_later_than_ocf():
    rows = (
        _q1_chain(31, "ocf", 100, 40, 40)
        + _ytd_chain(31, "capex", 40, 20, 20)
    )
    out = gate0.build_ttm(_facts(rows))
    assert _one(out, 31, "ttm_flows_window_end") == D("2026-03-31")
    assert _one(out, 31, "ttm_fcf_after_sbc") is None
    resolved = gate0.resolve_ttm_sbc(out.with_columns(pl.lit(0.0).alias("sbc")))
    assert _one(resolved, 31, "ttm_fcf_after_sbc") is None


def test_discrete_step_refuses_a_gap_after_the_window():
    """A discrete quarter that does not start the day after the window ends
    would leave a hole in the year. No step."""
    rows = (
        _ytd_chain(4, "ocf", 100, 40, 50)
        + _q1_chain(4, "sbc", 12, 3, 4)
        + [
            _fact(4, "sbc", "Q3", "2025-07-01", "2025-09-30", 3),
            _fact(4, "sbc", "Q3", "2026-07-01", "2026-09-30", 5),
        ]
    )
    out = gate0.build_ttm(_facts(rows))
    assert _one(out, 4, "ttm_sbc") == pytest.approx(12 - 3 + 4)
    assert _one(out, 4, "ttm_window_end") == D("2026-03-31")


def test_clean_control_label_unchanged():
    """Every FCF leg on the same YTD2 pair: nothing binds early, nothing steps."""
    rows = (
        _ytd_chain(5, "ocf", 100, 40, 50)
        + _ytd_chain(5, "capex", 30, 10, 12)
        + _ytd_chain(5, "sbc", 8, 4, 5)
    )
    out = gate0.build_ttm(_facts(rows))
    assert _one(out, 5, "ttm_window_end") == D("2026-06-30")
    assert _one(out, 5, "ttm_flows_window_end") == D("2026-06-30")
    assert _one(out, 5, "ttm_window_binding_concepts") == ""
    assert _one(out, 5, "ttm_sbc") == pytest.approx(9)


def test_no_ocf_window_leaves_the_flows_columns_unmeasured():
    """Missing is never zero: without an OCF window there is no flows end to
    compare against, so the binder list is NULL, not an empty all-clear."""
    out = gate0.build_ttm(_facts(_ytd_chain(6, "capex", 30, 10, 12)))
    assert _one(out, 6, "ttm_flows_window_end") is None
    assert _one(out, 6, "ttm_window_binding_concepts") is None


# --------------------------------------------------------------------------
# (a) latest_q_* staleness
# --------------------------------------------------------------------------

def _row(**over):
    base = {
        "cik": 1,
        "period_end": D("2025-12-31"),
        "ttm_window_end": D("2026-06-30"),
        "latest_q_period_end": D("2026-06-30"),
        "latest_q_equity": 1_000.0,
        "latest_q_goodwill": 10.0,
        "latest_q_intangibles": 5.0,
        "latest_q_cash": 200.0,
        "latest_q_total_debt": 300.0,
        "latest_q_shares_diluted": 50.0,
        "equity": 900.0,
        "goodwill": 10.0,
        "intangibles": 5.0,
        "tangible_book": 885.0,
    }
    base.update(over)
    return pl.DataFrame([base], schema={
        "cik": pl.Int64, "period_end": pl.Date, "ttm_window_end": pl.Date,
        "latest_q_period_end": pl.Date, "latest_q_equity": pl.Float64,
        "latest_q_goodwill": pl.Float64, "latest_q_intangibles": pl.Float64,
        "latest_q_cash": pl.Float64, "latest_q_total_debt": pl.Float64,
        "latest_q_shares_diluted": pl.Float64, "equity": pl.Float64,
        "goodwill": pl.Float64, "intangibles": pl.Float64,
        "tangible_book": pl.Float64,
    })


BALANCES = ("latest_q_equity", "latest_q_goodwill", "latest_q_intangibles",
            "latest_q_cash", "latest_q_total_debt")


def test_sblk_shape_nulls_a_five_year_old_balance_sheet():
    frame = _row(latest_q_period_end=D("2021-06-30"), latest_q_total_debt=None)
    out = gate0.add_latest_q_staleness(frame)
    assert out["latest_q_stale"][0] is True
    for column in BALANCES:
        assert out[column][0] is None, column
    # A weighted-average count is a duration with its own (current) date.
    assert out["latest_q_shares_diluted"][0] == 50.0
    # The rejected date stays on the row so the flag can be checked.
    assert out["latest_q_period_end"][0] == D("2021-06-30")


def test_sblk_shape_from_facts_end_to_end():
    """The SBLK facts shape through both builders and the staleness step."""
    rows = _ytd_chain(7, "ocf", 296, 103, 262) + [
        _fact(7, "equity", "Q2", None, "2021-06-30", 1_756_821_000),
        _fact(7, "cash", "Q2", None, "2021-06-30", 225_473_000),
        _fact(7, "equity", "FY", None, "2025-12-31", 2_449_300_000),
        _fact(7, "shares_diluted", "YTD2", "2026-01-01", "2026-06-30", 111_697_798),
    ]
    facts = _facts(rows)
    frame = (
        pl.DataFrame({"cik": [7], "period_end": [D("2025-12-31")]})
        .join(gate0.build_ttm(facts), on="cik", how="left")
        .join(gate0.build_latest_quarter(facts), on="cik", how="left")
    )
    out = gate0.add_latest_q_staleness(frame)
    assert out["latest_q_stale"][0] is True
    assert out["latest_q_equity"][0] is None
    assert out["latest_q_cash"][0] is None
    assert out["latest_q_shares_diluted"][0] == 111_697_798


def test_clean_control_current_quarter_is_untouched():
    frame = _row()
    out = gate0.add_latest_q_staleness(frame)
    assert out["latest_q_stale"][0] is False
    assert out.drop("latest_q_stale").equals(frame)


def test_staleness_is_null_when_not_evaluable():
    """No TTM window to measure against: NOT EVALUABLE, values untouched."""
    frame = _row(ttm_window_end=None, latest_q_period_end=D("2021-06-30"))
    out = gate0.add_latest_q_staleness(frame)
    assert out["latest_q_stale"][0] is None
    assert out["latest_q_equity"][0] == 1_000.0


def test_stale_quarter_newer_than_fiscal_year_is_kept_fail_closed():
    """A quarter >200 days behind the flows but still NEWER than the fiscal
    year is the newest balance sheet the row has. Nulling it would hand the
    tangible-book leg the OLDER fiscal-year vintage -- here turning a FAIL on
    the quarter (negative tangible book) into a PASS. Flagged, not nulled."""
    frame = _row(
        period_end=D("2025-06-30"),
        latest_q_period_end=D("2025-09-30"),
        ttm_window_end=D("2026-06-30"),
        latest_q_equity=-50.0,
    )
    out = gate0.add_tangible_book_vintage(gate0.add_latest_q_staleness(frame))
    assert out["latest_q_stale"][0] is True
    assert out["latest_q_equity"][0] == -50.0
    assert out["tangible_book_basis"][0] == gate0.TANGIBLE_BOOK_BASIS_LATEST_Q
    assert out["fail_tangible_book"][0] is True


def test_sblk_shape_tangible_book_basis_does_not_move():
    """The stale quarter was already older than the fiscal year, so the leg
    was on the fiscal-year vintage before and stays there."""
    frame = _row(latest_q_period_end=D("2021-06-30"))
    before = gate0.add_tangible_book_vintage(frame)
    after = gate0.add_tangible_book_vintage(gate0.add_latest_q_staleness(frame))
    for column in ("tangible_book_basis", "fail_tangible_book"):
        assert after[column][0] == before[column][0], column


# --------------------------------------------------------------------------
# Per-component vintage (MMSI, TREX): one row, one balance-sheet date
# --------------------------------------------------------------------------

def _balances(cik, end, **values):
    return [_fact(cik, c, "Q2", None, end, v) for c, v in values.items()]


def test_mmsi_shape_drops_a_2019_intangibles_beside_2026_equity():
    rows = (
        _balances(8, "2019-06-30", intangibles=74_400_000)
        + _balances(8, "2026-06-30", equity=1_600_000_000, goodwill=460_000_000,
                    cash=300_000_000)
    )
    out = gate0.build_latest_quarter(_facts(rows))
    assert _one(out, 8, "latest_q_period_end") == D("2026-06-30")
    assert _one(out, 8, "latest_q_intangibles") is None
    assert _one(out, 8, "latest_q_goodwill") == 460_000_000
    assert _one(out, 8, "latest_q_stale_components") == "intangibles"


def test_mmsi_shape_tangible_book_fails_closed_without_fy_intangibles():
    """FY intangibles unresolved: the quarterly figure cannot be built, so the
    leg falls to the fiscal-year vintage -- here also unbuildable -- and stays
    NOT_EVALUABLE. It must never become a PASS on a dropped component."""
    frame = _row(latest_q_intangibles=None, intangibles=None, tangible_book=None)
    out = gate0.add_tangible_book_vintage(gate0.add_latest_q_staleness(frame))
    assert out["tangible_book_latest_q"][0] is None
    assert out["fail_tangible_book"][0] is None
    assert out["tangible_book_basis"][0] == gate0.TANGIBLE_BOOK_BASIS_NONE


def _dropped(frame, concept, end, basis, value=None):
    return frame.with_columns(
        pl.lit(D(end)).alias(f"_latest_q_{concept}_dropped_end"),
        pl.lit(value, dtype=pl.Float64).alias(f"_latest_q_{concept}_dropped_value"),
        pl.lit(basis).alias(f"resolution_basis_{concept}"),
    )


def _osw(stale_goodwill):
    return _dropped(
        _row(latest_q_goodwill=None, goodwill=0.0, latest_q_equity=1_000.0,
             latest_q_intangibles=503.0, intangibles=511.0, tangible_book=31.0),
        "goodwill", "2023-12-31", gate0.BASIS_STRUCTURED_ABSENCE, stale_goodwill,
    )


def test_osw_shape_inferred_fy_zero_does_not_overturn_a_dropped_goodwill():
    """OSW: stale quarterly goodwill dropped, FY goodwill an INFERRED zero.
    On the stale figure the leg FAILS (1000 - 600 - 503); on the inferred zero
    it passes. Two disagreeing pieces of evidence are NOT_EVALUABLE."""
    out = gate0.add_tangible_book_vintage(_osw(600.0))
    assert out["fail_tangible_book"][0] is None
    assert out["tangible_book_latest_q"][0] is None
    assert out["tangible_book_basis"][0] == gate0.TANGIBLE_BOOK_BASIS_NONE
    assert not any(c.startswith("_latest_q_") for c in out.columns)


def test_osw_shape_agreeing_evidence_still_decides_the_leg():
    """Same shape, but the stale goodwill (100) leaves the leg positive either
    way: the answer does not depend on which figure is right."""
    out = gate0.add_tangible_book_vintage(_osw(100.0))
    assert out["fail_tangible_book"][0] is False
    assert out["tangible_book_basis"][0] == gate0.TANGIBLE_BOOK_BASIS_FISCAL_YEAR
    assert out["tangible_book_latest_q"][0] is None


def test_unsupported_goodwill_zero_never_publishes_quarterly_tangible_book():
    """Reviewer case: 200 equity, dropped 100 goodwill, inferred FY zero."""
    frame = _dropped(
        _row(latest_q_equity=200.0, latest_q_goodwill=None,
             latest_q_intangibles=0.0, goodwill=0.0, intangibles=0.0,
             tangible_book=200.0),
        "goodwill", "2025-09-30", "never_acquired", 100.0,
    )
    out = gate0.add_tangible_book_vintage(frame)
    assert out["tangible_book_latest_q"][0] is None
    assert out["fail_tangible_book"][0] is False
    assert out["tangible_book_basis"][0] == gate0.TANGIBLE_BOOK_BASIS_FISCAL_YEAR


def test_agreeing_unsupported_fallback_needs_independent_fy_evidence():
    frame = _dropped(
        _row(latest_q_equity=200.0, latest_q_goodwill=None,
             latest_q_intangibles=0.0, goodwill=0.0, intangibles=0.0,
             tangible_book=None),
        "goodwill", "2025-09-30", "never_acquired", 100.0,
    )
    out = gate0.add_tangible_book_vintage(frame)
    assert out["tangible_book_latest_q"][0] is None
    assert out["fail_tangible_book"][0] is None


def test_lvs_shape_newer_reported_fy_figure_may_stand_in():
    """LVS: quarterly intangibles 489M@2025-09 dropped; FY intangibles a
    REPORTED 470M@2025-12, newer than the dropped value. The fallback stands."""
    frame = _dropped(
        _row(latest_q_intangibles=None, intangibles=470.0, latest_q_equity=600.0,
             latest_q_goodwill=103.0, goodwill=103.0, tangible_book=1017.0),
        "intangibles", "2025-09-30", gate0.BASIS_REPORTED,
    )
    out = gate0.add_tangible_book_vintage(frame)
    assert out["tangible_book_latest_q"][0] == pytest.approx(600 - 103 - 470)
    assert out["tangible_book_basis"][0] == gate0.TANGIBLE_BOOK_BASIS_LATEST_Q
    assert out["fail_tangible_book"][0] is False


def test_trex_shape_drops_a_2019_goodwill():
    rows = (
        _balances(9, "2019-06-30", goodwill=10_000_000)
        + _balances(9, "2026-06-30", equity=900_000_000, intangibles=0)
    )
    out = gate0.build_latest_quarter(_facts(rows))
    assert _one(out, 9, "latest_q_goodwill") is None
    assert _one(out, 9, "latest_q_intangibles") == 0
    assert _one(out, 9, "latest_q_stale_components") == "goodwill"


def test_component_control_one_date_keeps_everything():
    rows = _balances(10, "2026-06-30", equity=900, goodwill=10, intangibles=5,
                     cash=200, total_debt=300) + [
        _fact(10, "shares_diluted", "YTD2", "2026-01-01", "2026-06-30", 50),
    ]
    out = gate0.build_latest_quarter(_facts(rows))
    for column, value in (("latest_q_equity", 900), ("latest_q_goodwill", 10),
                          ("latest_q_intangibles", 5), ("latest_q_cash", 200),
                          ("latest_q_total_debt", 300), ("latest_q_shares_diluted", 50)):
        assert _one(out, 10, column) == value, column
    assert _one(out, 10, "latest_q_stale_components") == ""


def test_share_count_older_than_the_balance_sheet_is_dropped():
    rows = _balances(11, "2026-06-30", equity=900) + [
        _fact(11, "shares_diluted", "YTD2", "2024-01-01", "2024-06-30", 50),
    ]
    out = gate0.build_latest_quarter(_facts(rows))
    assert _one(out, 11, "latest_q_shares_diluted") is None
    assert _one(out, 11, "latest_q_stale_components") == "shares_diluted"


# --------------------------------------------------------------------------
# Live store
# --------------------------------------------------------------------------

@pytest.mark.integration
def test_sblk_live_latest_q_is_not_a_2021_balance_sheet(universe):
    row = universe.filter(pl.col("ticker") == "SBLK")
    if row.is_empty():
        pytest.skip("SBLK not in this store")
    row = row.to_dicts()[0]
    assert row["latest_q_stale"] is True
    for column in BALANCES:
        assert row[column] is None, column
    assert "latest_q_stale" in row["store_defect_flags"]


@pytest.fixture(scope="module")
def universe():
    from edgar_lib import Paths

    paths = Paths()
    if not paths.facts.exists() or not paths.meta.exists():
        pytest.skip("facts.parquet/meta.parquet not built; run build_facts.py first")
    return gate0.load_universe(paths)


@pytest.mark.integration
def test_mmsi_live_tangible_book_is_not_built_on_a_2019_intangibles(universe):
    """MMSI read tangible_book_latest_q 1,065.6M (true ~528M): a 2019-06-30
    latest_q_intangibles (74.4M) sat beside 2026 equity. The stale component
    must be dropped and the leg must not PASS on it."""
    row = universe.filter(pl.col("ticker") == "MMSI")
    if row.is_empty():
        pytest.skip("MMSI not in this store")
    row = row.to_dicts()[0]
    assert "intangibles" in (row["latest_q_stale_components"] or "")
    assert row["latest_q_intangibles"] is None
    tb = row["tangible_book_latest_q"]
    assert tb is None or tb < 1_000e6
    assert row["fail_tangible_book"] is not False or tb is not None
