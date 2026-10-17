"""Coverage board builder (audit 2026-10-15 D5, auditor 2026-10-16).

Frozen and network-free: synthetic page texts only (the repo is public), run
through the real coverage_builder.js with node.
  - the same page text gives IDENTICAL records whether it arrived as a saved
    notion-fetch file, an MCP content array, a JSON string or inline plain text
  - partial failures never refresh a timestamp on retained data
  - a truncated or unparseable source keeps the previous rows and is FAILED
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

EDGAR = Path(__file__).resolve().parent.parent
BUILDER = EDGAR / "coverage_builder.js"
PARSER = EDGAR / "coverage_parser.js"
NODE = shutil.which("node")
T0 = "2026-10-01T00:00:00.000Z"

if NODE is None:
    if os.environ.get("EDGAR_FROZEN") == "1":
        raise RuntimeError("node is required for the coverage builder tests in CI")
    pytest.skip("node not installed", allow_module_level=True)


def queue_page(first, last, total=None, drop=()):
    """A queue page in the enhanced-markdown table shape parseQueue reads."""
    rows = "".join(
        f"<tr><td>R{i}</td><td>**T{i}** (Name {i} Inc.) · US SMID</td><td>unreviewed</td>"
        f"<td>${i}.0B · P/FCF-SBC {10 + i}.0x · (+{i}%)</td><td>x</td><td>-</td></tr>\n"
        for i in range(first, last + 1) if i not in drop
    )
    return f"**Rows R{first}–R{last} of {total or last}**\n<table>\n{rows}</table>\n"


CONSOLE = (
    "intro\n# 🎣 PIPELINE — live levels\n<table>\n"
    "<tr><td>Ticker</td><td>Level</td><td>Tier</td><td>Stage</td></tr>\n"
    "<tr><td>**AAA**</td><td>**$50.00**</td><td>Tier 1 / 2%</td><td>Buy-eligible</td></tr>\n"
    "</table>\n# 👁️ WATCHLIST — no live level\n"
    "- **BBB** (Bee Co) -- 2026-09-01 add. Tier 2. Re-look: margin recovers.\n"
)


def prev_snapshot(path, pages):
    """Previous snapshot: each page label with its rows, all stamped T0."""
    queue, labels = [], []
    for label, rows in pages.items():
        labels.append(label)
        queue += [dict(id=f"R{i}", t=f"OLD{i}", page=label, status="unreviewed") for i in rows]
    snap = {"asof": {"queue": T0, "console": T0, "excl": T0, "spot": T0},
            "pages": labels, "queue": queue,
            "pipeline": [{"t": "OLDP", "lvl": 1}], "watch": [{"t": "OLDW"}]}
    path.write_text(json.dumps(snap), encoding="utf-8")
    return path


def run(tmp_path, *args, prev):
    out = tmp_path / "out.json"
    r = subprocess.run([NODE, str(BUILDER), "--parser", str(PARSER), "--prev", str(prev),
                        "--out", str(out), *args], capture_output=True, text=True)
    return r.returncode, json.loads(out.read_text(encoding="utf-8")), r.stdout


@pytest.mark.parametrize("shape", ["saved_text_field", "mcp_content_array", "json_string", "inline_plain"])
def test_every_input_shape_yields_identical_records(tmp_path, shape):
    page = queue_page(1, 3)
    wrap = {
        "saved_text_field": json.dumps({"metadata": {"type": "page"}, "text": page}),
        "mcp_content_array": json.dumps([{"type": "text", "text": page}]),
        "json_string": json.dumps(page),
        "inline_plain": page,
    }[shape]
    f = tmp_path / "q.txt"
    f.write_text(wrap, encoding="utf-8")
    mode = "inline" if shape == "inline_plain" else "file"
    prev = prev_snapshot(tmp_path / "prev.json", {})
    code, out, _ = run(tmp_path, "--queue", f"Rows R1–R3|{f}|{mode}", prev=prev)

    baseline_file = tmp_path / "base.txt"
    baseline_file.write_text(page, encoding="utf-8")
    _, base, _ = run(tmp_path, "--queue", f"Rows R1–R3|{baseline_file}", prev=prev)
    assert code == 0
    assert out["queue"] == base["queue"]
    assert [q["t"] for q in out["queue"]] == ["T1", "T2", "T3"]
    assert out["freshness"][0]["status"] == f"fresh ({mode})"


def test_retained_pages_keep_their_old_timestamp(tmp_path):
    """Partial refresh: page A fresh, page B not supplied -> B stays at T0."""
    prev = prev_snapshot(tmp_path / "prev.json", {"Rows R1–R3": [1, 2, 3], "Rows R4–R6": [4, 5, 6]})
    f = tmp_path / "a.txt"
    f.write_text(queue_page(1, 3), encoding="utf-8")
    code, out, _ = run(tmp_path, "--queue", f"Rows R1–R3|{f}", prev=prev)
    assert code == 0
    assert out["asof"]["pages"]["Rows R4–R6"] == T0
    assert out["asof"]["pages"]["Rows R1–R3"] != T0
    assert out["asof"]["queue"] == T0                 # the OLDEST page, never "now"
    assert [q["t"] for q in out["queue"] if q["page"] == "Rows R4–R6"] == ["OLD4", "OLD5", "OLD6"]
    status = {f["source"]: f["status"] for f in out["freshness"]}
    assert status["queue: Rows R4–R6"] == "STALE (retained)"
    assert out["asof"]["console"] == T0 and out["asof"]["excl"] == T0


def test_truncated_page_is_failed_and_keeps_previous_rows(tmp_path):
    """Header says R1–R3 but a row is missing (a truncated inline copy)."""
    prev = prev_snapshot(tmp_path / "prev.json", {"Rows R1–R3": [1, 2, 3]})
    f = tmp_path / "a.txt"
    f.write_text(queue_page(1, 3, drop=(2,)), encoding="utf-8")
    code, out, stdout = run(tmp_path, "--queue", f"Rows R1–R3|{f}|inline", prev=prev)
    assert code == 2
    assert [q["t"] for q in out["queue"]] == ["OLD1", "OLD2", "OLD3"]
    assert out["asof"]["pages"]["Rows R1–R3"] == T0
    assert out["freshness"][0]["status"] == "FAILED"
    assert "header says 3" in out["freshness"][0]["detail"]
    assert "FAILED" in stdout


def test_unparseable_console_keeps_previous_pipeline_and_timestamp(tmp_path):
    prev = prev_snapshot(tmp_path / "prev.json", {})
    bad = tmp_path / "console.json"
    bad.write_text(json.dumps({"unexpected": "shape"}), encoding="utf-8")
    code, out, _ = run(tmp_path, "--console", str(bad), prev=prev)
    assert code == 2
    assert out["pipeline"] == [{"t": "OLDP", "lvl": 1}]
    assert out["asof"]["console"] == T0


def test_good_console_refreshes_only_the_console(tmp_path):
    prev = prev_snapshot(tmp_path / "prev.json", {"Rows R1–R3": [1, 2, 3]})
    f = tmp_path / "console.json"
    f.write_text(json.dumps([{"type": "text", "text": CONSOLE}]), encoding="utf-8")
    code, out, _ = run(tmp_path, "--console", str(f), prev=prev)
    assert code == 0
    assert [p["t"] for p in out["pipeline"]] == ["AAA"]
    assert [w["t"] for w in out["watch"]] == ["BBB"]
    assert out["asof"]["console"] != T0
    assert out["asof"]["queue"] == T0 and out["asof"]["excl"] == T0


def test_partial_spots_are_reported_partial(tmp_path):
    prev = prev_snapshot(tmp_path / "prev.json", {})
    snap = json.loads(prev.read_text())
    snap["pipeline"] = [{"t": "AAA"}, {"t": "BBB"}]
    prev.write_text(json.dumps(snap))
    spots = tmp_path / "spots.json"
    spots.write_text(json.dumps({"AAA": {"spot": 10, "spotTs": "2026-10-16T00:00:00Z"}}))
    _, out, _ = run(tmp_path, "--spots", str(spots), prev=prev)
    status = {f["source"]: (f["status"], f["detail"]) for f in out["freshness"]}
    assert status["spots"] == ("PARTIAL", "1/2 priced")
    assert "spot" not in out["pipeline"][1]          # BBB keeps no invented price
