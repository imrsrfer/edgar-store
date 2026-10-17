"""Single-writer lease for the Opportunity Screener (audit 2026-10-15 D6).

Every Screener run (Sonnet weekly task or Opus pass, however it was started:
manual, Coverage board or hand-off) must hold this lease before its first Notion
write. The two triggers share one lease, so Opus and Sonnet never write at once.

    python3 run_lock.py acquire --dir D --session S --model M   -> prints TOKEN, exit 0
                                                                   exit 3 = BUSY (do no writes)
    python3 run_lock.py guard   --dir D --token N               -> exit 0 = still the owner
                                                                   exit 4 = LOST (stop writing)
    python3 run_lock.py release --dir D --token N
    python3 run_lock.py status  --dir D

Why this is compare-and-set and not read-then-write: acquiring creates
lease.<n+1> with O_CREAT|O_EXCL, which the filesystem grants to exactly one
caller. Nothing is ever deleted or overwritten (the Cowork mount refuses deletes):
beats are appended, a release is a new end.<n> file.

Safety rule. `guard` refuses once the owner's last beat is TTL old; a successor
may take over only once that beat is TTL + GRACE old. A successful guard appends
a fresh beat, so an owner that guarded at time g cannot be succeeded before
g + TTL + GRACE. The owner therefore must finish the single Notion write that
follows a guard inside that window (minutes against 100 min), and the clocks of
the runs that share the lease must agree to within GRACE.
ponytail: one lease file per run accumulates in the lock dir (no deletes allowed); a few KB a year.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

TTL = 90 * 60     # guard at least this often (every row write guards)
GRACE = 10 * 60   # extra wait before a crashed owner is succeeded
BUSY, LOST = 3, 4


def now():
    return float(os.environ.get("RUN_LOCK_NOW") or time.time())   # override for tests only


def latest(d):
    ns = [int(m.group(1)) for p in d.iterdir() if (m := re.fullmatch(r"lease\.(\d+)", p.name))]
    return max(ns, default=0)


def last_beat(d, n):
    """Newest timestamp for lease n. An unreadable or still-empty lease counts as fresh."""
    try:
        stamp = json.loads((d / f"lease.{n}").read_text())["acquired"]
    except (OSError, ValueError, KeyError):
        return now()
    beats = d / f"beat.{n}"
    if beats.exists():
        for line in beats.read_text().splitlines():
            try:
                stamp = max(stamp, float(line))
            except ValueError:
                pass   # a half-written last line
    return stamp


def holder(d, n):
    try:
        return json.loads((d / f"lease.{n}").read_text())
    except (OSError, ValueError):
        return {}


def ended(d, n):
    return (d / f"end.{n}").exists()


def acquire(d, session, model):
    n = latest(d)
    if n and not ended(d, n) and now() - last_beat(d, n) < TTL + GRACE:
        h = holder(d, n)
        return BUSY, f"BUSY held by token {n} ({h.get('model')} {h.get('session')}), last beat {int(now() - last_beat(d, n))}s ago"
    try:
        fd = os.open(d / f"lease.{n + 1}", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return BUSY, f"BUSY lost the race for token {n + 1}"
    os.write(fd, json.dumps({"session": session, "model": model, "acquired": now()}).encode())
    os.close(fd)
    return 0, str(n + 1)


def guard(d, n):
    if not (d / f"lease.{n}").exists():
        return LOST, f"LOST no lease {n}"
    if latest(d) > n:
        return LOST, f"LOST token {latest(d)} has taken over"
    if ended(d, n):
        return LOST, f"LOST token {n} was released"
    age = now() - last_beat(d, n)
    if age >= TTL:
        return LOST, f"LOST last beat {int(age)}s ago (limit {TTL}s)"
    with open(d / f"beat.{n}", "a") as f:
        f.write(f"{now()}\n")
    return 0, f"OK token {n}"


def release(d, n):
    if latest(d) != n:
        return LOST, f"LOST token {latest(d)} is newer; nothing to release"
    try:
        os.close(os.open(d / f"end.{n}", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        pass
    return 0, f"RELEASED token {n}"


def status(d):
    n = latest(d)
    if not n:
        return 0, "FREE no lease taken yet"
    state = "released" if ended(d, n) else f"last beat {int(now() - last_beat(d, n))}s ago"
    return 0, f"token {n} {holder(d, n)} {state}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["acquire", "guard", "release", "status"])
    ap.add_argument("--dir", required=True)
    ap.add_argument("--token", type=int)
    ap.add_argument("--session", default="?")
    ap.add_argument("--model", default="?")
    a = ap.parse_args(argv)
    d = Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)
    if a.cmd in ("guard", "release") and a.token is None:
        ap.error("--token is required")
    code, msg = {"acquire": lambda: acquire(d, a.session, a.model),
                 "guard": lambda: guard(d, a.token),
                 "release": lambda: release(d, a.token),
                 "status": lambda: status(d)}[a.cmd]()
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
