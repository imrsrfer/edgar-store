"""Screener single-writer lease (audit 2026-10-15 D6, auditor 2026-10-16).

The auditor's closure evidence, reproduced against the real run_lock.py:
  - simultaneous starts (manual, Coverage board, hand-off) yield ONE owner
  - a duplicate request while a run is active writes nothing
  - a crashed owner is succeeded after TTL + GRACE (crash recovery)
  - an expired owner cannot write after its successor took over
"""
import subprocess
import sys
import time
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


# Separate processes, all released at the same wall-clock instant. Not
# multiprocessing: on Python 3.14 Linux its forkserver needs a socket, which the
# frozen job blocks.
CONTEND = """
import sys, time
sys.path.insert(0, sys.argv[1])
import run_lock
from pathlib import Path
while time.time() < float(sys.argv[3]):
    pass
code, msg = run_lock.acquire(Path(sys.argv[2]), "cse_" + sys.argv[4], "opus")
print(code, msg)
"""


def test_simultaneous_starts_yield_exactly_one_owner(tmp_path):
    n = 20
    go = time.time() + 5
    procs = [subprocess.Popen([sys.executable, "-c", CONTEND, str(Path(rl.__file__).parent), str(tmp_path),
                               str(go), str(i)], stdout=subprocess.PIPE, text=True) for i in range(n)]
    results = [tuple(p.communicate(timeout=60)[0].split(" ", 1)) for p in procs]
    results = [(int(c), m.strip()) for c, m in results]
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


def test_a_stall_inside_guard_cannot_return_ok_after_a_successor(tmp_path, clock, monkeypatch):
    # Auditor W2 (2026-10-21): A passes guard's checks at beat age just under TTL, stalls before appending
    # its beat, B acquires once that old beat is TTL + GRACE old, then A resumes. A must get LOST, not OK.
    rl.acquire(tmp_path, "cse_a", "opus")
    real_append = rl._append_beat

    def stalled_append(d, n):
        clock(T0 + rl.TTL + rl.GRACE)                      # A is frozen here; B arrives
        assert rl.acquire(tmp_path, "cse_b", "sonnet") == (0, "2")
        real_append(d, n)                                  # A resumes and appends its beat

    monkeypatch.setattr(rl, "_append_beat", stalled_append)
    clock(T0 + rl.TTL - 1)
    code, msg = rl.guard(tmp_path, 1)
    assert code == rl.LOST and "token 2" in msg
    monkeypatch.setattr(rl, "_append_beat", real_append)
    assert rl.guard(tmp_path, 2)[0] == 0                   # B is the owner


def test_a_stall_after_the_beat_cannot_return_ok(tmp_path, clock, monkeypatch):
    # Auditor W3 (2026-10-21) pause 1: beat appended, then a 6,000 s stall before the final checks.
    rl.acquire(tmp_path, "cse_a", "opus")
    real_append = rl._append_beat

    def append_then_stall(d, n):
        t = real_append(d, n)
        clock(T0 + 6_000)
        return t

    monkeypatch.setattr(rl, "_append_beat", append_then_stall)
    code, msg = rl.guard(tmp_path, 1)
    assert code == rl.LOST and "stalled" in msg


def test_a_stall_between_the_final_checks_cannot_return_ok(tmp_path, clock, monkeypatch):
    # Auditor W3 pause 2: latest-token check passes, stall, successor acquires, then the released check.
    rl.acquire(tmp_path, "cse_a", "opus")
    real_ended, calls = rl.ended, []

    def ended_after_successor(d, n):
        calls.append(n)
        if len(calls) == 2:                                # the post-beat released check
            clock(T0 + rl.TTL + rl.GRACE + 6_000)
            assert rl.acquire(tmp_path, "cse_b", "sonnet") == (0, "2")
        return real_ended(d, n)

    monkeypatch.setattr(rl, "ended", ended_after_successor)
    code, msg = rl.guard(tmp_path, 1)
    assert code == rl.LOST


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
