"""rank_queue.py: the stated ranking rule, pinned case by case."""

import polars as pl

import rank_queue as rq


def _survivors(rows):
    schema = {
        "ticker": pl.Utf8,
        "sic": pl.Utf8,
        "filing_form": pl.Utf8,
        "market_cap": pl.Float64,
        "fcf_after_sbc": pl.Float64,
        "store_defect_flags": pl.Utf8,
        "lane_count": pl.Int64,
        "lanes": pl.Utf8,
    }
    base = {"sic": "3500", "filing_form": "10-K", "fcf_after_sbc": 1e8,
            "store_defect_flags": "", "lane_count": 1, "lanes": "value"}
    return pl.DataFrame([{**base, **r} for r in rows], schema=schema)


def _order(ranked):
    return ranked.filter(~pl.col("tracked")).sort("rank")["ticker"].to_list()


def test_tiers_follow_the_measured_keep_rates():
    ranked = rq.rank(
        _survivors(
            [
                {"ticker": "MEGA", "market_cap": 50e9},
                {"ticker": "LARGE", "market_cap": 10e9},
                {"ticker": "MICRO", "market_cap": 200e6},
                {"ticker": "SMID", "market_cap": 2e9},
                {"ticker": "NOCAP", "market_cap": None},
            ]
        )
    )
    assert _order(ranked) == ["SMID", "MICRO", "LARGE", "MEGA", "NOCAP"]
    tiers = dict(ranked.select("ticker", "tier").iter_rows())
    assert tiers == {"SMID": "A", "MICRO": "B", "LARGE": "C", "MEGA": "D", "NOCAP": "E"}


def test_foreign_and_financial_in_band_names_are_not_tier_a():
    ranked = rq.rank(
        _survivors(
            [
                {"ticker": "FPI", "market_cap": 2e9, "filing_form": "20-F"},
                {"ticker": "BANK", "market_cap": 2e9, "sic": "6022"},
                {"ticker": "US", "market_cap": 2e9},
            ]
        )
    )
    tiers = dict(ranked.select("ticker", "tier").iter_rows())
    assert tiers["US"] == "A"
    assert tiers["FPI"] == "B"
    assert tiers["BANK"] == "B"


def test_fail_open_defect_sorts_after_clean_within_tier_but_not_across():
    ranked = rq.rank(
        _survivors(
            [
                {"ticker": "BROKEN", "market_cap": 2e9, "lane_count": 3,
                 "store_defect_flags": "shares_history_scale_break"},
                {"ticker": "CLEAN", "market_cap": 2e9},
                {"ticker": "BIG", "market_cap": 10e9, "lane_count": 3},
            ]
        )
    )
    assert _order(ranked) == ["CLEAN", "BROKEN", "BIG"]


def test_fail_closed_defects_do_not_demote():
    ranked = rq.rank(
        _survivors(
            [
                {"ticker": "DEBTFLOOR", "market_cap": 2e9, "lane_count": 2,
                 "store_defect_flags": "total_debt_partial,capex_gross_of_proceeds"},
                {"ticker": "PLAIN", "market_cap": 2e9},
            ]
        )
    )
    assert _order(ranked) == ["DEBTFLOOR", "PLAIN"]


def test_lane_overlap_then_cheapness_nulls_last():
    ranked = rq.rank(
        _survivors(
            [
                {"ticker": "CHEAP", "market_cap": 1e9, "fcf_after_sbc": 1e8},     # 10x
                {"ticker": "DEAR", "market_cap": 4e9, "fcf_after_sbc": 1e8},      # 40x
                {"ticker": "LOSS", "market_cap": 1e9, "fcf_after_sbc": -5e7},     # n/a
                {"ticker": "OVERLAP", "market_cap": 4e9, "lane_count": 2},
            ]
        )
    )
    # BELFB rule: a missing or negative multiple never sorts as cheap.
    assert _order(ranked) == ["OVERLAP", "CHEAP", "DEAR", "LOSS"]


def test_tracked_names_are_labelled_not_dropped_and_nothing_is_capped():
    rows = [{"ticker": f"T{i}", "market_cap": 1e9 + i} for i in range(40)]
    ranked = rq.rank(_survivors(rows), tracked=["t0", "T1"])
    assert ranked.height == 40
    tracked = ranked.filter(pl.col("tracked"))
    assert set(tracked["ticker"]) == {"T0", "T1"}
    assert tracked["rank"].null_count() == 2
    # §G-1 / Fork J: never cap the queue. Every untracked row is ranked.
    assert ranked.filter(~pl.col("tracked"))["rank"].to_list() == list(range(1, 39))
    assert "beyond_cap" not in ranked.columns


def test_refill_name_sorts_after_every_lane_name_in_its_tier():
    rows = [
        {"ticker": "LANE", "market_cap": 2e9, "fcf_after_sbc": 1e7},          # 200x
        {"ticker": "REFILL", "market_cap": 2e9, "lane_count": 0, "lanes": "direct",
         "fcf_after_sbc": 4e8},                                              # 5x
        {"ticker": "BIGLANE", "market_cap": 10e9},
    ]
    assert _order(rq.rank(_survivors(rows))) == ["LANE", "REFILL", "BIGLANE"]


def _gate0(rows):
    base = {"filing_form": "10-K", "sic": "3500", "gate0_framework_pass": "true",
            "revenue": "2e8", "net_income_suspect": "false", "shares_scale_suspect": "false",
            "ttm_fcf_after_sbc": "1e7", "revenue_cagr_3y": "0.05",
            "fcf_per_share_cagr_3y": "0.10", "fcf_per_share_cagr_5y": "", "fcf_inflection": "false",
            "shares_diluted": "1e8"}
    return pl.DataFrame([{**base, **r} for r in rows]).with_columns(
        pl.all().cast(pl.Utf8)
    ).with_columns(pl.all().replace("", None))


def test_direct_pass_reproduces_each_refill_cut():
    gate0 = _gate0([
        {"ticker": "GOOD"},
        {"ticker": "INLANE"},
        {"ticker": "FAILLEG", "gate0_framework_pass": "false"},
        {"ticker": "BANK", "sic": "6022"},
        {"ticker": "FPI", "filing_form": "20-F"},
        {"ticker": "SMALL", "revenue": "1e7"},
        {"ticker": "SCALE", "shares_scale_suspect": "true"},
        {"ticker": "BURN", "ttm_fcf_after_sbc": "-5"},
        {"ticker": "SHRINK", "revenue_cagr_3y": "-0.02"},
        {"ticker": "FCFDOWN", "fcf_per_share_cagr_3y": "-0.10"},
        {"ticker": "TURN", "fcf_per_share_cagr_3y": "-0.10", "fcf_inflection": "true"},
        {"ticker": "NOGROWTH", "revenue_cagr_3y": "", "fcf_per_share_cagr_3y": ""},
    ])
    prices = pl.DataFrame({"ticker": ["GOOD", "TURN"], "price": [10.0, 5.0],
                           "market_cap_supplied": [2e9, None]})
    rows, funnel = rq.direct_pass(gate0, prices, {"INLANE"})
    assert sorted(rows["ticker"].to_list()) == ["GOOD", "NOGROWTH", "TURN"]
    measured = dict(rows.select("ticker", "growth_not_measured").iter_rows())
    assert measured == {"GOOD": False, "NOGROWTH": True, "TURN": False}
    caps = dict(rows.select("ticker", "market_cap").iter_rows())
    assert caps["GOOD"] == 2e9            # supplied cap wins
    assert caps["TURN"] == 5.0 * 1e8      # derived from price x shares
    assert caps["NOGROWTH"] is None       # unpriced -> tier E, never guessed
    assert [n for _, n in funnel] == [10, 9, 8, 7, 6, 5, 4, 3, 2]
    assert set(rows["lanes"]) == {"direct"} and set(rows["lane_count"]) == {0}


def test_load_survivors_merges_lanes(tmp_path):
    def lane(name, rows):
        pl.DataFrame(rows).write_csv(tmp_path / name)

    lane("shortlist_value.csv", [
        {"cik": 1, "ticker": "AAA", "market_cap": 2e9, "rejected_because": ""},
        {"cik": 2, "ticker": "BBB", "market_cap": 2e9, "rejected_because": "growth"},
    ])
    lane("shortlist_accel.csv", [
        {"cik": 1, "ticker": "aaa", "market_cap": 2e9, "rejected_because": ""},
    ])
    merged, missing = rq.load_survivors(
        tmp_path, ("shortlist_value.csv", "shortlist_accel.csv", "shortlist_main.csv")
    )
    assert missing == ["shortlist_main.csv"]
    assert merged.height == 1
    row = merged.row(0, named=True)
    assert row["lanes"] == "accel+value" and row["lane_count"] == 2
