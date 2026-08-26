"""Regression tests for the 2026-08-26 capacity changes to screen.py.

Three gates were removing names for reasons the Framework does not authorise.
Each test below pins the CORRECTED behaviour and, where a tempting-but-wrong
alternative exists, pins that it was not taken.
"""

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import screen  # noqa: E402


QUALITY_CLEAN = {
    "income_quality": 1.40,
    "sbc_pct_revenue": 0.02,
    "tangible_book": 500_000_000.0,
    "tangible_book_yrs_negative": 0,
    "warn_inorganic": False,
}


def _quality(**over):
    survivors, _ = screen.apply_quality(pl.DataFrame([{**QUALITY_CLEAN, **over}]))
    return survivors.height == 1


# --------------------------------------------------------------------------
# Fix 1 -- warn_inorganic is a warning, not a disqualifier
# --------------------------------------------------------------------------


def test_inorganic_warning_no_longer_rejects():
    """43 otherwise-clean names were being killed by this flag -- more than
    the entire surviving shortlist."""
    assert _quality(warn_inorganic=True) is True


def test_inorganic_flag_is_not_replaced_by_a_growth_override():
    """🔴 The tempting fix is to admit a flagged name when revenue CAGR clears
    a bar. It is CIRCULAR -- total revenue includes ACQUIRED revenue, so it
    certifies as organic exactly the roll-ups the flag exists to catch. On the
    2026-08-25 store, 22 of the 38 names a >5% override would admit had
    acquisition intensity above 5% of revenue (LNTH 48%, NICE 29.1%, AYI
    27.4%, FSS 23.0%). Pinned so nobody reintroduces the override: quality
    must not read any revenue-growth column at all."""
    import inspect

    source = inspect.getsource(screen.apply_quality)
    body = source.split('"""')[-1]
    for forbidden in ("revenue_cagr", "_effective_cagr", "rev_cagr"):
        assert forbidden not in body, (
            f"apply_quality references {forbidden}: a growth override cannot "
            "settle the organic/inorganic split"
        )


def test_real_quality_legs_still_reject():
    assert _quality(income_quality=0.5) is False
    assert _quality(sbc_pct_revenue=0.15) is False
    assert _quality(tangible_book=-1.0) is False


# --------------------------------------------------------------------------
# Fix 5a -- tangible-book history relaxed to 1, NOT dropped
# --------------------------------------------------------------------------


def test_one_prior_negative_tangible_book_year_is_admitted():
    assert _quality(tangible_book_yrs_negative=1) is True


def test_chronic_negative_tangible_book_still_rejects():
    """Dropping this leg entirely would contradict the book's own logged
    standard -- FLO and UPBD sit in discarded.csv with the re-look condition
    'tangible book positive for two consecutive years'."""
    assert _quality(tangible_book_yrs_negative=3) is False
    assert _quality(tangible_book_yrs_negative=4) is False


# --------------------------------------------------------------------------
# Fix 3 -- momentum tags, it does not kill
# --------------------------------------------------------------------------


def _momentum(pct, mode):
    frame = pl.DataFrame([{"ticker": "LOPE", "pct_vs_200ma": pct}])
    return screen.apply_momentum(frame, True, mode=mode)


def test_below_200d_ma_reaches_the_shortlist_tagged():
    """LOPE -- the console's own strongest find -- sits ~11% below its 200d MA
    and the pipeline could never see it."""
    survivors, rejected = _momentum(-0.11, "flag")
    assert survivors.height == 1
    assert rejected.height == 0
    assert survivors["drawdown_undiagnosed"][0] is True


def test_above_200d_ma_is_not_tagged():
    survivors, _ = _momentum(0.21, "flag")
    assert survivors["drawdown_undiagnosed"][0] is False


def test_hard_mode_restores_the_old_rejection():
    survivors, rejected = _momentum(-0.11, "hard")
    assert survivors.height == 0
    assert rejected["rejected_because"][0] == "momentum"


def test_flag_is_the_default():
    import inspect

    assert inspect.signature(screen.apply_momentum).parameters["mode"].default == "flag"


# --------------------------------------------------------------------------
# The value lane (added 2026-08-26) -- quality at a discount.
#
# It drops the COMPOUNDING bars and the margin-DELTA test, which is what
# makes the main lane blind to flat-but-cheap quality. It does not drop the
# floor under the business, and it does not drop profitability.
# --------------------------------------------------------------------------


VALUE_ROW = {
    "growth_basis": "5y",
    "revenue_cagr_5y": 0.005,
    "revenue_cagr_3y": None,
    "fcf_per_share_cagr_5y": 0.01,
    "fcf_per_share_cagr_3y": None,
    "fcf_inflection": False,
    "fcf_per_share_delta_abs": None,
}


def _growth(lane, **over):
    survivors, _ = screen.apply_growth(pl.DataFrame([{**VALUE_ROW, **over}]), lane)
    return survivors.height == 1


def test_flat_but_not_shrinking_passes_value_and_fails_main():
    """0.5% revenue growth and 1% FCF/share growth: too slow to compound, not
    shrinking. This is the cohort the main lane structurally cannot reach."""
    assert _growth("value") is True
    assert _growth("main") is False


def test_shrinking_revenue_is_rejected_by_the_value_lane():
    """🔴 The book already discarded FIZZ on exactly this: a nominal FCF yield
    'is not a genuine margin of safety against a shrinking earnings base.'
    A cheapness ranking with no floor would rank the traps FIRST, because a
    shrinking business is cheap on trailing numbers by construction."""
    assert _growth("value", revenue_cagr_5y=-0.05) is False


def test_shrinking_fcf_per_share_is_rejected_by_the_value_lane():
    assert _growth("value", fcf_per_share_cagr_5y=-0.02) is False


def test_value_lane_requires_profitability_but_not_expansion():
    """A stable high-margin business is what this lane exists to find, so the
    margin DELTA test comes off. The LEVEL test does not -- without it the
    lane admits unprofitable companies and ranks them by a P/FCF multiple,
    which is the PERF failure of 2026-08-07 rebuilt somewhere new."""
    flat = pl.DataFrame([{"operating_margin_latest": 0.28, "operating_margin_delta": -0.01}])
    survivors, _ = screen.apply_margin_profitable(flat)
    assert survivors.height == 1
    survivors, _ = screen.apply_margin_expansion(flat)
    assert survivors.height == 0, "the main lane's delta test should still reject this"

    lossmaking = pl.DataFrame(
        [{"operating_margin_latest": -0.025, "operating_margin_delta": 0.30}]
    )
    survivors, rejected = screen.apply_margin_profitable(lossmaking)
    assert survivors.height == 0
    assert rejected["rejected_because"][0] == "value:unprofitable"


# --------------------------------------------------------------------------
# The cheapness ranking -- where the null trap lives
# --------------------------------------------------------------------------


def _ranked(rows):
    return screen.rank_by_cheapness(pl.DataFrame(rows))["ticker"].to_list()


def test_nulls_and_negatives_sort_LAST_not_first():
    """🔴 On 2026-08-06 a blank cell topped a P/FCF sort and BELFB reached a
    shortlist on a multiple that did not exist. A lane whose entire output IS
    a cheapness ranking puts that trap at position one by construction. A
    negative multiple is a loss, not a bargain, and sorts with the nulls."""
    order = _ranked([
        {"ticker": "NULLCAP", "market_cap": None, "fcf_after_sbc": 10.0},
        {"ticker": "CHEAP", "market_cap": 100.0, "fcf_after_sbc": 20.0},
        {"ticker": "LOSS", "market_cap": 100.0, "fcf_after_sbc": -5.0},
        {"ticker": "DEAR", "market_cap": 900.0, "fcf_after_sbc": 20.0},
        {"ticker": "NULLFCF", "market_cap": 100.0, "fcf_after_sbc": None},
    ])
    assert order[0] == "CHEAP"
    assert order[1] == "DEAR"
    assert set(order[2:]) == {"NULLCAP", "LOSS", "NULLFCF"}


def test_multiple_is_recomputed_on_the_live_cap():
    out = screen.rank_by_cheapness(
        pl.DataFrame([{"ticker": "X", "market_cap": 1_000.0, "fcf_after_sbc": 50.0}])
    )
    assert out["p_fcf_after_sbc_live"][0] == 20.0
    assert out["fcf_after_sbc_yield"][0] == 0.05


def test_unknown_momentum_is_not_reported_as_a_drawdown():
    """🔴 The first version of the flag wrote (~ok).fill_null(True), which
    labelled a name with NO 200-day average as being in a drawdown -- the
    missing-input-versus-failed-test error, inside the function added to stop
    a gate from conflating exactly those two."""
    frame = pl.DataFrame([{"ticker": "NOMA", "pct_vs_200ma": None}])
    survivors, rejected = screen.apply_momentum(frame, True, mode="flag")
    assert survivors.height == 1 and rejected.height == 0
    assert survivors["drawdown_undiagnosed"][0] is False
    assert survivors["momentum_unknown"][0] is True
