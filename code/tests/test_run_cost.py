"""run_cost meter: synthetic transcripts only, never real session content."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".claude/screening/run_cost.py"
if not SCRIPT.is_file():
    pytest.skip("no .claude/screening beside this code tree", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("run_cost", SCRIPT)
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)


def usage(i=0, cw=0, cr=0, out=0):
    return {"input_tokens": i, "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": cr, "output_tokens": out}


def asst(mid, model, u, tool=None, side=False):
    content = [{"type": "tool_use", "id": tool[0], "name": tool[1], "input": {}}] if tool else [{"type": "text", "text": "x"}]
    message = {"id": mid, "model": model, "role": "assistant", "content": content}
    if u is not None:
        message["usage"] = u
    return {"type": "assistant", "isSidechain": side, "message": message}


def result(tool_id, text, side=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": text}
    return {"type": "user", "isSidechain": side, "message": {"role": "user", "content": [block]}}


def write(path, entries, extra_lines=()):
    lines = [json.dumps(e) for e in entries] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def test_duplicate_message_id_counted_once(tmp_path):
    p = write(tmp_path / "s.jsonl", [
        asst("m1", OPUS, usage(1, 100, 200, 10)),
        asst("m1", OPUS, usage(1, 100, 200, 10)),
        asst("m2", OPUS, usage(2, 0, 50, 5)),
    ])
    r = rc.measure(p)
    assert r["tokens"]["opus"] == {"input": 3, "cache_creation": 100, "cache_read": 250, "output": 15}


def test_duplicate_id_keeps_final_chunk_with_largest_output(tmp_path):
    p = write(tmp_path / "s.jsonl", [
        asst("m1", OPUS, usage(1, 0, 0, 4)), asst("m1", OPUS, usage(1, 0, 0, 40)),
        asst("m1", OPUS, usage(1, 0, 0, 9)),
    ])
    assert rc.measure(p)["tokens"]["opus"]["output"] == 40


def test_sidechain_inline_and_subagent_files_attributed_by_model(tmp_path):
    main = write(tmp_path / "sess.jsonl", [
        asst("m1", OPUS, usage(1, 0, 0, 10)),
        asst("s1", SONNET, usage(2, 0, 0, 20), side=True),
    ])
    sub = tmp_path / "sess" / "subagents"
    sub.mkdir(parents=True)
    write(sub / "agent-a.jsonl", [asst("s2", SONNET, usage(4, 0, 0, 30), side=True)])
    r = rc.measure(main)
    assert r["tokens"]["opus"]["output"] == 10
    assert r["tokens"]["sonnet"] == {"input": 6, "cache_creation": 0, "cache_read": 0, "output": 50}


def test_families_other_and_haiku(tmp_path):
    p = write(tmp_path / "s.jsonl", [
        asst("a", "claude-haiku-5-5", usage(1)), asst("b", "<synthetic>", usage(2)),
    ])
    t = rc.measure(p)["tokens"]
    assert t["haiku"]["input"] == 1 and t["other"]["input"] == 2


def test_notion_bytes_split_main_vs_subagent_and_largest(tmp_path):
    main = write(tmp_path / "sess.jsonl", [
        asst("m1", OPUS, usage(1), tool=("t1", "mcp__claude_ai_Notion__notion-fetch")),
        result("t1", "a" * 2048),
        asst("m2", OPUS, usage(1), tool=("t2", "mcp__claude_ai_Notion__notion-update-page")),
        result("t2", "z" * 9999),
        asst("s1", SONNET, usage(1), tool=("t3", "mcp__Notion__notion-search"), side=True),
        result("t3", "b" * 4096, side=True),
    ])
    sub = tmp_path / "sess" / "subagents"
    sub.mkdir(parents=True)
    write(sub / "agent-a.jsonl", [
        asst("s9", SONNET, usage(1), tool=("t4", "mcp__Notion__notion-query-data-sources"), side=True),
        result("t4", [{"type": "text", "text": "c" * 1000}], side=True),
    ])
    n = rc.measure(main)["notion"]
    assert n["main_bytes"] == 2048
    assert n["subagent_bytes"] == 4096 + 1000
    assert n["largest_bytes"] == 4096


def test_context_peak_is_main_agent_only(tmp_path):
    p = write(tmp_path / "s.jsonl", [
        asst("m1", OPUS, usage(5, 100, 1000, 9)),
        asst("m2", OPUS, usage(5, 0, 3000, 9)),
        asst("s1", SONNET, usage(5, 0, 90000, 9), side=True),
    ])
    assert rc.measure(p)["peak_context"] == {"opus": 3005}


def test_malformed_lines_counted_not_fatal_and_missing_usage_counted(tmp_path):
    p = write(tmp_path / "s.jsonl", [
        asst("m1", OPUS, usage(1, 0, 0, 1)), asst("m2", OPUS, None),
    ], extra_lines=["{not json", "[1, 2]"])
    r = rc.measure(p)
    assert r["malformed"] == 2
    assert r["usage_missing"] == 1
    assert r["tokens"]["opus"]["input"] == 1
    line = rc.format_line(r)
    assert "malformed 2" in line and "usage_missing 1" in line


def test_missing_usage_on_duplicate_id_with_usage_is_not_missing(tmp_path):
    p = write(tmp_path / "s.jsonl", [asst("m1", OPUS, None), asst("m1", OPUS, usage(7))])
    r = rc.measure(p)
    assert r["usage_missing"] == 0 and r["tokens"]["opus"]["input"] == 7


def test_session_id_lookup_under_config_dir(tmp_path, monkeypatch):
    proj = tmp_path / "projects" / "C--some-project"
    proj.mkdir(parents=True)
    write(proj / "abc-123.jsonl", [asst("m1", OPUS, usage(1))])
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert rc.resolve_transcript("abc-123") == proj / "abc-123.jsonl"
    with pytest.raises(FileNotFoundError):
        rc.resolve_transcript("nope")


def test_latest_picks_newest_main_transcript_not_subagent(tmp_path, monkeypatch):
    import os
    proj = tmp_path / "projects" / "-home-claude"
    proj.mkdir(parents=True)
    old, new = proj / "old-session.jsonl", proj / "new-session.jsonl"
    write(old, [asst("m1", OPUS, usage(1))])
    write(new, [asst("m2", OPUS, usage(1))])
    sub = proj / "new-session" / "subagents" / "agent-1.jsonl"
    sub.parent.mkdir(parents=True)
    write(sub, [asst("s1", "claude-sonnet-5-5", usage(1), side=True)])
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))
    os.utime(sub, (3_000, 3_000))  # newest file overall, but a subagent's
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert rc.resolve_transcript("latest") == new


def test_latest_with_no_transcripts_raises(tmp_path, monkeypatch):
    (tmp_path / "projects").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        rc.resolve_transcript("latest")


def test_line_format_and_json_cli(tmp_path, capsys):
    p = write(tmp_path / "s.jsonl", [
        asst("m1", OPUS, usage(12000, 210000, 1_900_000, 31000)),
        asst("s1", SONNET, usage(1000, 0, 0, 2000), side=True),
    ])
    line = rc.format_line(rc.measure(p))
    assert line.startswith("COST · opus in 12k / cache-read 1.9M / cache-write 210k / out 31k")
    assert "sonnet in 1.0k" in line
    assert "peak ctx opus 2.1M" in line
    assert "notion 0 KB" in line
    assert rc.main([str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tokens"]["opus"]["output"] == 31000
    assert rc.main([str(p)]) == 0
    assert capsys.readouterr().out.startswith("COST · ")
