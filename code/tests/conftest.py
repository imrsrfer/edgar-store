"""Put the pipeline modules on sys.path, and keep the suite out of the real store.

🔴 THE PRODUCTION DATA ROOT IS NOT A TEST FIXTURE.

``Paths`` derives every path the pipeline writes from a single root, and that
root defaults to :data:`edgar_lib.DEFAULT_ROOT` -- the live store. Any test that
reaches an entry point without pinning ``--root`` therefore writes into
production. ``gate0.main`` does exactly that: ``--out`` redirects only gate0.csv,
while data_quality.csv, inactive_filers.csv and duplicate_filers.csv still land
on the real root. On 2026-09-09 a plain ``pytest`` run truncated data_quality.csv
from 4,301 rows to 1 and emptied inactive_filers.csv. It stayed out of the remote
only because gate0.py happened to run after pytest that day; reverse the ordering
and sync_to_repo.py ships the gutted files.

The fix is a redirect, not a rule nobody enforces:

1. ``DEFAULT_ROOT`` is repointed at a per-session temporary root, seeded with a
   byte-identical copy of the real store's top-level files. Reads see exactly
   what they saw before -- the integration tests still pin real filings, and the
   pass/fail/skip result is unchanged. Writes land in the copy and are discarded.
2. ``Paths.__init__`` then refuses the real root outright, so a future test that
   passes ``root=`` explicitly fails loudly instead of quietly corrupting the
   store. Belt and braces, which is the right posture for a failure mode whose
   only symptom is a smaller file.

``raw/`` is deliberately NOT copied: it holds companyfacts.zip and no test reads
it. ``Paths.ensure()`` recreates it empty inside the temporary root.
"""

import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# Audit 2026-10-15 #12: frozen vs integration (auditor recommendation 2026-10-16)
#
#   pytest -m "not integration"   frozen, network-free; BLOCKS code merges (CI)
#   pytest -m integration         reads the live store; NON-blocking in CI,
#                                 but BLOCKS publication: sync_to_repo.py runs it
#                                 with EDGAR_REQUIRE_STORE=1 before committing.
# A test is integration when it uses a live-store fixture below, or carries
# @pytest.mark.integration because it builds Paths() on the store itself.
# --------------------------------------------------------------------------
LIVE_STORE_FIXTURES = {"universe", "universe_zeroed", "annual_facts", "gate0_csv_frame", "live_rows"}
MISSING_STORE = "not built"  # the skip text every live-store fixture uses


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: reads the live store, not the frozen fixture")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if LIVE_STORE_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integration)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Under the publication gate, a missing store is a failure, not a skip."""
    outcome = yield
    report = outcome.get_result()
    if (os.environ.get("EDGAR_REQUIRE_STORE") == "1" and report.skipped
            and MISSING_STORE in str(report.longrepr)):
        report.outcome = "failed"


@pytest.fixture(autouse=True)
def _no_network_when_frozen(monkeypatch):
    """EDGAR_FROZEN=1 (CI): any socket connect is a test failure."""
    if os.environ.get("EDGAR_FROZEN") == "1":
        def refuse(*_a, **_k):
            raise RuntimeError("network access in a frozen test")
        monkeypatch.setattr(socket.socket, "connect", refuse)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import edgar_lib  # noqa: E402

# Captured at import time, before anything is patched: the root the suite must
# never touch. Resolved so a case- or separator-variant spelling still matches.
PRODUCTION_ROOT = Path(edgar_lib.DEFAULT_ROOT).resolve()

# Seeded into the temporary root so read-only fixtures behave identically.
# Suffix-based rather than a hand-maintained name list: a list that has to be
# updated when the pipeline grows an output is a list that will be wrong once.
SEEDED_SUFFIXES = (".csv", ".parquet", ".jsonl", ".json", ".md")


def _is_within(candidate: Path, parent: Path) -> bool:
    """True when *candidate* is *parent* or sits underneath it."""
    try:
        resolved = candidate.resolve()
    except OSError:  # pragma: no cover - unresolvable path is not the real root
        return False
    return resolved == parent or parent in resolved.parents


@pytest.fixture(scope="session", autouse=True)
def isolated_data_root(tmp_path_factory):
    """Point the whole suite at a disposable copy of the store."""
    root = tmp_path_factory.mktemp("edgar_store")

    if PRODUCTION_ROOT.is_dir():
        for source in sorted(PRODUCTION_ROOT.iterdir()):
            if source.is_file() and source.suffix.lower() in SEEDED_SUFFIXES:
                # copyfile, not copy2 -- the store carries read-only files
                # (prices.csv), and inheriting that bit would make the copy
                # unwritable for the very tests that need to write to it.
                shutil.copyfile(source, root / source.name)

    original_init = edgar_lib.Paths.__init__

    def guarded_init(self, root_arg=None):
        original_init(self, root_arg)
        if _is_within(self.root, PRODUCTION_ROOT):
            raise RuntimeError(
                f"Test attempted to build Paths on the production data root "
                f"({self.root}). Every path the pipeline writes derives from "
                f"this root, so the run would have written into the live store. "
                f"Pass a tmp_path root, or rely on the isolated_data_root "
                f"fixture in tests/conftest.py."
            )

    patch = pytest.MonkeyPatch()
    patch.setattr(edgar_lib, "DEFAULT_ROOT", root)
    patch.setattr(edgar_lib.Paths, "__init__", guarded_init)
    try:
        yield root
    finally:
        patch.undo()
