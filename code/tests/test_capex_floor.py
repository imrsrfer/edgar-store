"""BORR investing floor and NVGS depreciation/TTM capex controls."""

import datetime as dt

import polars as pl
import pytest

import gate0
from concepts import CONCEPTS_BY_NAME, WANTED_TAGS
from edgar_lib import Paths


M = 1e6


def _annual(**updates):
    row = dict(cik=1, fiscal_year=2025, revenue=1000*M, ocf=250*M,
               sbc=11*M, capex=0.3*M, dep_amort=148*M,
               net_income=100*M, operating_income=150*M, equity=500*M,
               goodwill=0.0, intangibles=0.0, cash=100*M, total_debt=0.0,
               shares_diluted=50*M, tax_expense=20*M, pretax_income=120*M,
               acquisitions=0.0, buybacks=0.0)
    row.update(updates)
    return gate0.compute_metrics(pl.DataFrame([row])).row(0, named=True)


def _floor(**updates):
    row = dict(cik=1, ticker="BORR", capex=0.3*M, fcf_after_sbc=240.3*M,
               capex_broken=False, capex_suspect=False, investing_cf=-124.5*M,
               investing_unreconciled=True, investing_residual=-124.2*M)
    row.update(updates)
    return gate0._add_capex_investing_floor(pl.DataFrame([row])).row(0, named=True)


def test_borr_investing_floor_is_published_beside_original_values():
    row = _floor()
    assert row["capex_suspect"] is True
    assert row["capex_floor_from_investing"] == pytest.approx(124.5*M)
    assert row["fcf_after_sbc_capex_floor"] == pytest.approx(116.1*M)
    assert row["capex"] == 0.3*M
    assert row["fcf_after_sbc"] == 240.3*M


def test_us_foods_asset_heavy_closing_statement_is_unchanged():
    """USFD has $410M capex, $462M D&A, and a closing investing statement."""
    annual = _annual(ticker="USFD", capex=410*M, dep_amort=462*M,
                     revenue=35_000*M, ocf=1000*M)
    row = _floor(ticker="USFD", capex=410*M, capex_suspect=annual["capex_suspect"],
                 fcf_after_sbc=annual["fcf_after_sbc"], investing_cf=-410*M,
                 investing_unreconciled=False, investing_residual=0.0)
    assert annual["capex_suspect"] is False
    assert row["capex_suspect"] is False
    assert row["capex_floor_from_investing"] is None
    assert row["fcf_after_sbc_capex_floor"] is None
    assert row["capex"] == 410*M
    assert row["fcf_after_sbc"] == annual["fcf_after_sbc"]


def test_navigator_fy2024_zero_capex_is_broken():
    row = _annual(ticker="NVGS", revenue=586.961*M, ocf=201.662*M,
                  capex=0.0, dep_amort=132.725*M)
    assert row["capex_broken"] is True
    assert row["capex_suspect"] is True
    assert row["fcf_after_sbc"] is None


def test_navigator_fy2025_normal_capex_is_clean():
    row = _annual(ticker="NVGS", revenue=586.961*M, ocf=201.662*M,
                  capex=85.019*M, dep_amort=134.497*M)
    assert row["capex_suspect"] is False
    assert row["capex_vs_dep_amort"] == pytest.approx(85.019/134.497)


def test_asset_light_filer_above_one_percent_da_is_not_flagged():
    row = _annual(revenue=100*M, ocf=2*M, capex=3*M, dep_amort=10*M)
    assert row["capex_suspect"] is False


def test_dep_amort_chain_reaches_navigator_and_borr_tags():
    chain = CONCEPTS_BY_NAME["dep_amort"].chain
    assert chain[-2:] == ("DepreciationAndAmortization", "Depreciation")
    assert set(chain[-2:]) <= WANTED_TAGS


def _ttm(capex, revenue=614.474*M):
    values = {"ocf": 193.684*M, "capex": capex,
              "sbc": 2.151*M, "revenue": revenue, "dep_amort": 134.497*M}
    quarters = [(dt.date(2025, 7, 1), dt.date(2025, 9, 30)),
                (dt.date(2025, 10, 1), dt.date(2025, 12, 31)),
                (dt.date(2026, 1, 1), dt.date(2026, 3, 31)),
                (dt.date(2026, 4, 1), dt.date(2026, 6, 30))]
    rows = [dict(cik=1, concept=concept, fiscal_period="Q1", period_start=start,
                 period_end=end, value=value/4)
            for concept, value in values.items() if value is not None
            for start, end in quarters]
    return gate0.build_ttm(pl.DataFrame(rows)).row(0, named=True)


def test_navigator_ttm_small_capex_warns_and_retains_fcf():
    row = _ttm(1.328*M)
    assert row["ttm_capex_suspect"] is True
    assert row["ttm_capex_zero"] is False
    assert row["ttm_fcf_after_sbc"] == pytest.approx(190.205*M)


def test_zero_ttm_capex_nulls_fcf_even_after_sbc_resolution():
    row = _ttm(0.0)
    assert row["ttm_capex_zero"] is True
    assert row["ttm_capex_suspect"] is True
    assert row["ttm_fcf_after_sbc"] is None
    resolved = gate0.resolve_ttm_sbc(pl.DataFrame([{**row, "ttm_sbc": None,
                                                    "sbc": 0.0}], infer_schema_length=None))
    assert resolved["ttm_fcf_after_sbc"][0] is None


def test_ttm_zero_without_revenue_is_not_a_broken_capex_claim():
    row = _ttm(0.0, revenue=None)
    assert row["ttm_capex_zero"] is False
    assert row["ttm_fcf_after_sbc"] == pytest.approx((193.684-2.151)*M)


@pytest.fixture(scope="module")
def universe():
    paths = Paths()
    if not paths.facts.exists() or not paths.meta.exists():
        pytest.skip("facts.parquet/meta.parquet not built")
    return gate0.load_universe(paths)


def _live(universe, ticker):
    return universe.filter(pl.col("ticker") == ticker).row(0, named=True)


def test_borr_live_floor(universe):
    row = _live(universe, "BORR")
    assert row["capex_floor_from_investing"] == pytest.approx(124.5*M, rel=0.01)
    assert row["fcf_after_sbc_capex_floor"] == pytest.approx(116.1*M, rel=0.01)


def test_navigator_live_ttm_suspect(universe):
    row = _live(universe, "NVGS")
    assert row["ttm_capex_suspect"] is True
    if row["dep_amort"] is not None:  # old facts.parquet predates the tag addition
        assert row["dep_amort"] == pytest.approx(134.497*M, rel=0.01)


def test_navigator_live_fy2024_zero_capex(universe):
    facts = pl.read_parquet(Paths().facts).filter(
        (pl.col("cik") == 1581804) & (pl.col("fiscal_year") == 2024)
        & (pl.col("fiscal_period") == "FY")
    )
    row = gate0.compute_metrics(gate0.widen(facts)).row(0, named=True)
    assert row["capex"] == 0.0
    assert row["capex_broken"] is True
    assert row["capex_suspect"] is True
    assert row["fcf_after_sbc"] is None


def test_us_foods_live_investing_control(universe):
    row = _live(universe, "USFD")
    assert row["capex"] == pytest.approx(410*M)
    assert row["capex_suspect"] is False
    assert row["investing_unreconciled"] is False
    assert row["capex_floor_from_investing"] is None
    assert row["fcf_after_sbc_capex_floor"] is None
