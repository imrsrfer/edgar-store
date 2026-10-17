"""Screener single-writer lease (audit 2026-10-15 D6, auditor 2026-10-16).

The auditor's closure evidence, reproduced against the real run_lock.py:
  - simultaneous starts (manual, Coverage board, hand-off) yield ONE owner
  - a duplicate request while a run is active writes nothing
  - a crashed owner is succeeded after TTL + GRACE (crash recovery)
  - an expired owner cannot write after its successor took over
"""
import multiprocessing as mp
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_lock as rl  # noqa: E402

T0 = 1_800_000_000.0


@pytest.fixture
def clock(monkeypatch):
    def at(t):
        monkeypatch.setenv("RUN_LOCK_NOW", str(t))
    at(T0)
    return at


def _contend(d, i, barrier, out):
    barrier.wait()
    code, msg = rl.acquire(Path(d), f"cse_{i}", "opus" if i % 2 else "sonnet")
    out.put((code, msg))


def test_simultaneous_starts_yield_exactly_one_owner(tmp_path):
    n = 20
    barrier, out = mp.Barrier(n), mp.Queue()
    procs = [mp.Process(target=_contend, args=(str(tmp_path), i, barrier, out)) for i in range(n)]
    for p in procs:
        p.start()
    results = [out.get(timeout=60) for _ in procs]
    for p in procs:
        p.join()
    winners = [m for c, m in results if c == 0]
    assert winners == ["1"]
    assert sum(c == rl.BUSY for c, _ in results) == n - 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["lease.1"]


def test_duplicate_request_while_active_is_refused(tmp_path, clock):
    assert rl.acquire(tmp_path, "cse_a", "opus") == (0, "1")
    clock(T0 + 60 * 60)                      # an hour in, still beating
    assert rl.guard(tmp_path, 1)[0] == 0
    clock(T0 + 150 * 60)                     # 90 min after the last beat: < TTL + GRACE
    code, msg = rl.acquire(tmp_path, "cse_b", "sonnet")
    assert code == rl.BUSY and "token 1" in msg


def test_crashed_owner_is_succeeded_after_ttl_plus_grace(tmp_path, clock):
    rl.acquire(tmp_path, "cse_a", "opus")
    clock(T0 + rl.TTL + rl.GRACE - 1)
    assert rl.acquire(tmp_path, "cse_b", "sonnet")[0] == rl.BUSY
    clock(T0 + rl.TTL + rl.GRACE)
    assert rl.acquire(tmp_path, "cse_b", "sonnet") == (0, "2")


def test_expired_owner_cannot_write_after_successor(tmp_path, clock):
    rl.acquire(tmp_path, "cse_a", "opus")
    clock(T0 + rl.TTL)                       # A stalled: its own guard already refuses
    assert rl.guard(tmp_path, 1)[0] == rl.LOST
    clock(T0 + rl.TTL + rl.GRACE)
    assert rl.acquire(tmp_path, "cse_b", "sonnet") == (0, "2")
    clock(T0 + rl.TTL + rl.GRACE + 1)
    code, msg = rl.guard(tmp_path, 1)        # A wakes up later: still refused
    assert code == rl.LOST and "token 2" in msg
    assert rl.release(tmp_path, 1)[0] == rl.LOST   # and cannot release B's lease
    assert rl.guard(tmp_path, 2)[0] == 0


def test_guard_keeps_a_long_run_alive(tmp_path, clock):
    rl.acquire(tmp_path, "cse_a", "opus")
    for k in range(1, 6):                    # 5 rows, 80 min apart: 400 min total
        clock(T0 + k * 80 * 60)
        assert rl.guard(tmp_path, 1)[0] == 0
        assert rl.acquire(tmp_path, "cse_b", "sonnet")[0] == rl.BUSY


def test_hand_off_release_then_receiver_acquires(tmp_path, clock):
    rl.acquire(tmp_path, "cse_a", "opus")
    assert rl.release(tmp_path, 1)[0] == 0
    assert rl.guard(tmp_path, 1)[0] == rl.LOST     # the firer can write nothing after release
    clock(T0 + 5)
    assert rl.acquire(tmp_path, "cse_b", "sonnet") == (0, "2")


def test_nothing_is_ever_deleted_or_rewritten(tmp_path, clock):
    rl.acquire(tmp_path, "cse_a", "opus")
    lease = (tmp_path / "lease.1").read_bytes()
    clock(T0 + 60)
    rl.guard(tmp_path, 1)
    clock(T0 + 120)
    rl.guard(tmp_path, 1)
    rl.release(tmp_path, 1)
    assert (tmp_path / "lease.1").read_bytes() == lease
    assert (tmp_path / "beat.1").read_text().splitlines() == [str(T0 + 60), str(T0 + 120)]
    assert (tmp_path / "end.1").exists()


def test_cli_exit_codes(tmp_path):
    run = lambda *a: subprocess.run([sys.executable, str(Path(rl.__file__)), *a, "--dir", str(tmp_path)],
                                    capture_output=True, text=True)
    r = run("acquire", "--session", "cse_a", "--model", "opus")
    assert (r.returncode, r.stdout.strip()) == (0, "1")
    assert run("acquire", "--session", "cse_b").returncode == rl.BUSY
    assert run("guard", "--token", "1").returncode == 0
    assert run("release", "--token", "1").returncode == 0
    assert run("guard", "--token", "1").returncode == rl.LOST
