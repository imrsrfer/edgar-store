"""The suite must never be able to write into the production data root.

This is the regression test for the 2026-09-09 corruption: ``gate0.main`` was
called without ``--root``, so ``--out`` redirected gate0.csv while
data_quality.csv (4,301 rows -> 1) and inactive_filers.csv (emptied) were
written straight into the live store. A green suite proved nothing there --
nothing went red when it happened, and the only symptom was a smaller file.

So the isolation in tests/conftest.py gets its own tests. Without them the
fixture is a comment: someone deletes it, every test still passes, and the
store is silently writable again.
"""

from __future__ import annotations

import pytest

from conftest import PRODUCTION_ROOT
from edgar_lib import Paths


def test_default_root_is_redirected_away_from_production(isolated_data_root):
    """A bare ``Paths()`` -- what every entry point falls back to -- is disposable."""
    assert Paths().root == isolated_data_root
    assert Paths().root != PRODUCTION_ROOT


def test_explicit_production_root_is_refused():
    """Passing the real root by hand fails loudly rather than quietly writing."""
    with pytest.raises(RuntimeError, match="production data root"):
        Paths(PRODUCTION_ROOT)


def test_subdirectory_of_production_root_is_refused():
    """The guard covers the whole subtree, not just an exact string match."""
    with pytest.raises(RuntimeError, match="production data root"):
        Paths(PRODUCTION_ROOT / "raw")


def test_gate0_main_side_outputs_land_in_the_isolated_root(isolated_data_root, tmp_path):
    """The exact call that caused the corruption, pinned.

    ``--out`` is honoured, and the three files it does NOT cover --
    data_quality.csv, inactive_filers.csv, duplicate_filers.csv -- must appear
    under the isolated root. Their presence there is the proof they are no
    longer being written to the store.
    """
    import gate0

    paths = Paths()
    if not paths.facts.exists() or not paths.meta.exists():
        pytest.skip("facts.parquet/meta.parquet not built; run build_facts.py first")

    out = tmp_path / "gate0.csv"
    price_csv = tmp_path / "prices.csv"
    price_csv.write_text("ticker,price,ma_200\nMCRI,85.50,80.00\n")

    assert gate0.main(
        ["--tickers", "MCRI", "--price-csv", str(price_csv), "--out", str(out)]
    ) == 0

    assert out.exists()
    for side_output in (paths.data_quality, paths.inactive_filers, paths.duplicate_filers):
        assert side_output.exists(), f"{side_output.name} was not written to the isolated root"
        assert PRODUCTION_ROOT not in side_output.parents
