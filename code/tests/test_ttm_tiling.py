"""A TTM must be four quarters that TILE a year, not four rows tagged "Q".

Regression tests for the 2026-08-27 defect: build_ttm counted rows, so for
every cash-flow concept it summed four FIRST quarters from four different
fiscal years and published the result as a trailing twelve months. ATRO and
FIGS both tripped ttm_fcf_divergence against a correct FY figure on the
strength of it.
"""
import datetime as dt
import polars as pl
import gate0


def _q(cik, concept, start, end, value):
    return {
        "cik": cik, "concept": concept, "fiscal_period": "Q1",
        "period_start": dt.date.fromisoformat(start),
        "period_end": dt.date.fromisoformat(end), "value": float(value),
    }


def _facts(rows):
    return pl.DataFrame(rows, schema={
        "cik": pl.Int64, "concept": pl.Utf8, "fiscal_period": pl.Utf8,
        "period_start": pl.Date, "period_end": pl.Date, "value": pl.Float64,
    })


def test_same_quarter_across_four_years_is_not_a_ttm():
    """The exact ATRO shape: four Q1s, four years. Must NOT produce a TTM."""
    rows = [
        _q(8063, "ocf", "2023-01-01", "2023-04-01", -19_181_000),
        _q(8063, "ocf", "2024-01-01", "2024-03-30", 2_037_000),
        _q(8063, "ocf", "2025-01-01", "2025-03-29", 20_642_000),
        _q(8063, "ocf", "2026-01-01", "2026-04-04", 10_606_000),
    ]
    out = gate0.build_ttm(_facts(rows))
    assert "ttm_ocf" not in out.columns or out["ttm_ocf"].is_null().all(), (
        "four Q1s from four years summed to a 'TTM' -- the 2026-08-27 defect"
    )


def test_four_consecutive_quarters_do_produce_a_ttm():
    rows = [
        _q(1, "ocf", "2025-01-01", "2025-03-31", 10.0),
        _q(1, "ocf", "2025-04-01", "2025-06-30", 20.0),
        _q(1, "ocf", "2025-07-01", "2025-09-30", 30.0),
        _q(1, "ocf", "2025-10-01", "2025-12-31", 40.0),
    ]
    out = gate0.build_ttm(_facts(rows))
    assert out["ttm_ocf"][0] == 100.0


def test_overlapping_quarters_are_rejected():
    """YTD spans that overlap sum to more than the year they cover."""
    rows = [
        _q(2, "ocf", "2025-01-01", "2025-03-31", 10.0),
        _q(2, "ocf", "2025-01-01", "2025-06-30", 30.0),
        _q(2, "ocf", "2025-07-01", "2025-09-30", 20.0),
        _q(2, "ocf", "2025-10-01", "2025-12-31", 25.0),
    ]
    out = gate0.build_ttm(_facts(rows))
    assert "ttm_ocf" not in out.columns or out["ttm_ocf"].is_null().all()


def test_a_missing_quarter_leaves_a_gap_and_is_rejected():
    """Three quarters of 2025 plus one of 2024 spans a year but does not tile it."""
    rows = [
        _q(3, "ocf", "2024-10-01", "2024-12-31", 5.0),
        _q(3, "ocf", "2025-01-01", "2025-03-31", 10.0),
        _q(3, "ocf", "2025-04-01", "2025-06-30", 20.0),
        _q(3, "ocf", "2025-10-01", "2025-12-31", 40.0),  # Q3 absent
    ]
    out = gate0.build_ttm(_facts(rows))
    assert "ttm_ocf" not in out.columns or out["ttm_ocf"].is_null().all()


def test_unmeasured_ttm_is_not_reported_as_agreement():
    """ttm_unavailable must distinguish 'not measured' from 'measured, clean'."""
    frame = pl.DataFrame({
        "ocf": [100.0, 100.0],
        "fcf_after_sbc": [50.0, 50.0],
        "ttm_ocf": [None, 90.0],
        "ttm_fcf_after_sbc": [None, 45.0],
    })
    out = gate0.add_ttm_divergence(frame)
    assert out["ttm_unavailable"].to_list() == [True, False]
    # both rows read False on the divergence flags -- which is exactly why
    # the third column has to exist.
    assert out["ttm_fcf_divergence"].to_list() == [False, False]


def test_a_rate_concept_is_averaged_not_summed():
    """Validity is not additivity. shares_diluted is a weighted average.

    Four quarters can tile a year perfectly and still not be summable. Summing
    four quarterly average share counts gives 4x the average and quadruples the
    denominator of every per-share figure. Measured after the tiling fix alone,
    ttm_shares_diluted / FY was still 3.976.
    """
    rows = []
    for start, end in [("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"),
                       ("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31")]:
        rows.append(_q(9, "shares_diluted", start, end, 100_000_000))
        rows.append(_q(9, "ocf", start, end, 25.0))
    out = gate0.build_ttm(_facts(rows))
    assert out["ttm_shares_diluted"][0] == 100_000_000, "share count was summed, not averaged"
    assert out["ttm_ocf"][0] == 100.0, "a genuine flow must still be summed"
