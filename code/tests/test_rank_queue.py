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
        "reporting_currency": pl.Utf8,
        "gate0_framework_pass": pl.Boolean,
    }
    base = {"sic": "3500", "filing_form": "10-K", "fcf_after_sbc": 1e8,
            "store_defect_flags": "", "lane_count": 1, "lanes": "value",
            "reporting_currency": "USD", "gate0_framework_pass": True}
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


def test_framework_failures_sort_in_dx_after_all_clean_tiers_without_multiple():
    frame = _survivors([
        {"ticker": "FAILED", "market_cap": 1e9, "lane_count": 4, "gate0_framework_pass": False},
        {"ticker": "E", "market_cap": None},
        {"ticker": "D", "market_cap": 30e9},
        {"ticker": "A", "market_cap": 1e9},
    ]).with_columns(
        pl.when(pl.col("ticker") == "FAILED").then(pl.lit("tax_anomaly"))
        .otherwise(pl.lit("")).alias("framework_leg_failed"),
        pl.lit(0.01).alias("p_fcf_after_sbc_live"),
    )
    out = rq.rank(frame)
    assert _order(out) == ["A", "D", "E", "FAILED"]
    failed = out.filter(pl.col("ticker") == "FAILED").row(0, named=True)
    assert failed["tier"] == "DX"
    assert failed["p_fcf_after_sbc_rank"] is None
    assert failed["p_fcf_after_sbc_live"] is None
    assert "tax_anomaly" in failed["rank_reason"]


def test_provisional_follows_clean_in_its_tier_even_with_more_lanes():
    frame = _survivors([
        {"ticker": "PROV", "market_cap": 1e9, "lane_count": 5},
        {"ticker": "CLEAN", "market_cap": 2e9},
        {"ticker": "LARGE", "market_cap": 10e9},
    ]).with_columns((pl.col("ticker") == "PROV").alias("gate0_provisional"))
    out = rq.rank(frame)
    assert _order(out) == ["CLEAN", "PROV", "LARGE"]
    assert "PROVISIONAL" in out.filter(pl.col("ticker") == "PROV")["rank_reason"][0]


def test_rank_never_falls_back_to_a_mixed_currency_or_stale_live_multiple():
    from datetime import date

    frame = _survivors([
        {"ticker": "USD", "market_cap": 1e9},
        {"ticker": "EUR", "market_cap": 1e9, "reporting_currency": "EUR"},
        {"ticker": "UNKNOWN", "market_cap": 1e9, "reporting_currency": None},
    ]).with_columns(pl.lit(0.01).alias("p_fcf_after_sbc_live"))
    out = rq.rank(frame)
    assert out["p_fcf_after_sbc_rank"].to_list() == [10.0, None, None]
    rates = pl.DataFrame({"currency": ["EUR"], "usd_per_unit": [1.25], "as_of": [date(2026, 10, 20)]})
    out = rq.rank(frame, fx_rates=rates, today=date(2026, 10, 22))
    assert _order(out) == ["EUR", "USD", "UNKNOWN"]
    assert out["p_fcf_after_sbc_rank"].to_list() == [8.0, 10.0, None]


def test_lane_loader_carries_diagnosis_currency_and_outstanding_legs(tmp_path):
    pl.DataFrame([{"ticker": "FAIL", "rejected_because": "", "reporting_currency": "EUR",
                   "gate0_framework_pass": False, "framework_leg_failed": "sbc",
                   "gate0_provisional": True, "test_adjusted_vs_gaap": "NOT_EVALUABLE",
                   "sbc_10_15": True, "ttm_sbc_owed": True}]).write_csv(tmp_path / "shortlist_value.csv")
    out, _ = rq.load_survivors(tmp_path, ("shortlist_value.csv",))
    assert out.height == 1
    for field in ("reporting_currency", "framework_leg_failed", "gate0_provisional",
                  "test_adjusted_vs_gaap", "sbc_10_15", "ttm_sbc_owed"):
        assert field in out.columns


def test_direct_keeps_unresolved_framework_ranked_in_normal_tier_with_owed_flags():
    rows, _ = rq.direct_pass(_gate0([{"ticker": "NE", "gate0_framework_pass": ""}]), None, set())
    assert rows.height == 1
    rows = rows.with_columns(pl.lit(1e9).alias("market_cap"),
                             pl.lit(True).alias("dilution_attribution_owed"))
    ranked = rq.rank(rows)
    assert ranked["rank"][0] == 1
    assert ranked["tier"][0] == "A"
    assert ranked["gate0_framework_pass"][0] is None
    assert ranked["framework_not_evaluated"][0] is True
    assert ranked["dilution_attribution_owed"][0] is True
    assert ranked["framework_diagnosis"][0] is False


def test_null_framework_lane_survivor_ranks_in_normal_tier():
    frame = _survivors([{"ticker": "NE", "market_cap": 1e9, "gate0_framework_pass": None}]).with_columns(
        pl.lit("NOT_EVALUABLE").alias("test_dilution"), pl.lit(True).alias("dilution_attribution_owed"))
    ranked = rq.rank(frame)
    assert ranked["tier"][0] == "A"
    assert ranked["rank"][0] == 1
    assert ranked["framework_leg_unresolved"][0] == "dilution"
    assert ranked["dilution_attribution_owed"][0] is True


def test_authoritative_provisional_flag_is_not_overridden_by_extraction_conflict():
    frame = _survivors([{"ticker": "CONFLICT", "market_cap": 1e9}]).with_columns(
        pl.lit(False).alias("gate0_provisional"), pl.lit(True).alias("ttm_fcf_divergence"),
        pl.lit(-100.0).alias("ttm_fcf_after_sbc"), pl.lit(True).alias("ttm_suspect"))
    assert rq.rank(frame)["gate0_provisional"][0] is False


def test_lane_union_preserves_failure_and_ne_over_another_lanes_pass(tmp_path):
    for lane, verdict, status in (("value", True, "PASS"), ("accel", False, "FAIL")):
        pl.DataFrame([{"ticker": "X", "rejected_because": "", "gate0_framework_pass": verdict,
                       "test_tax_anomaly": status, "reporting_currency": "USD"}]).write_csv(
                           tmp_path / f"shortlist_{lane}.csv")
    out, _ = rq.load_survivors(tmp_path, ("shortlist_value.csv", "shortlist_accel.csv"))
    assert out["gate0_framework_pass"][0] is False
    assert out["test_tax_anomaly"][0] == "FAIL"
