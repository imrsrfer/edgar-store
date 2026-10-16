"""Audit 2026-10-15 #10: --dry-run must not copy, write a manifest, or run git."""
import sys

import sync_to_repo


def test_dry_run_mutates_nothing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "store").mkdir(parents=True)
    (repo / "store" / "stale.csv").write_text("x")
    monkeypatch.setattr(sync_to_repo, "REPO", repo)
    monkeypatch.setattr(sync_to_repo, "CODE_SRC", tmp_path)
    monkeypatch.setattr(sync_to_repo, "STORE_SRC", tmp_path)
    git_calls = []
    monkeypatch.setattr(sync_to_repo, "run", lambda *a, **k: git_calls.append(a))
    monkeypatch.setattr(sys, "argv", ["sync_to_repo.py", "--dry-run"])

    sync_to_repo.main()

    assert git_calls == []
    assert sorted(p.name for p in repo.rglob("*")) == ["stale.csv", "store"]
