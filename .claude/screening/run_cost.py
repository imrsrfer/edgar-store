#!/usr/bin/env python3
"""Meter for a screener run: what a Claude Code transcript actually consumed. Standard library only.

    python .claude/screening/run_cost.py <transcript.jsonl | session-id> [--json]

Reads the session transcript (JSONL) plus its subagent transcripts and prints
one line for the Run Log, so the decider's EFFICIENCY line is measured, not
estimated. Tokens only, no dollar figures: prices change, tokens are the truth.
It reads, it never writes anything but stdout.

Transcript format (Claude Code, observed): one JSON object per line. Assistant
entries carry message.id, message.model and message.usage (input_tokens,
cache_creation_input_tokens, cache_read_input_tokens, output_tokens). Tool
calls are tool_use blocks (id, name) in assistant content; the answer is a
tool_result block (tool_use_id, content as str or a list of text blocks) in a
later user entry. Subagent transcripts live in `<session>/subagents/agent-*.jsonl`
beside the main file and carry isSidechain true; a sidechain entry inside the
main file is treated the same way.

Conventions:

- DEDUPLICATION: a message streamed in several entries repeats the same
  message.id and usage. Each message.id is counted once, using the entry with
  the largest output_tokens (the final chunk); a tie keeps the later entry.
  Entries without an id are never merged.
- family comes from message.model: opus / sonnet / haiku, else "other".
- a message.id that never carries a usage block is counted in `usage_missing`,
  not as zero tokens.
- malformed JSON lines are skipped and counted in `malformed`.
- Notion load is the byte size of tool_result content for tools whose name
  contains notion-fetch, notion-search or notion-query, split main agent vs
  subagent (the analyst). Reads only: notion-update-* is not counted.
- context peak is the largest input + cache_read + cache_creation of one main
  agent turn, per model family.
"""

import argparse
import json
import os
import sys
from pathlib import Path

FAMILIES = ("opus", "sonnet", "haiku")
OTHER = "other"
TOKEN_FIELDS = {
    "input": "input_tokens",
    "cache_creation": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
    "output": "output_tokens",
}
NOTION_MARKERS = ("notion-fetch", "notion-search", "notion-query")
KB = 1024


def family(model):
    name = (model or "").lower()
    return next((f for f in FAMILIES if f in name), OTHER)


def resolve_transcript(arg):
    """A path to a .jsonl, or a session id found under CLAUDE_CONFIG_DIR/projects."""
    path = Path(arg)
    if path.is_file():
        return path
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    for hit in sorted((config / "projects").glob(f"*/{arg}.jsonl")):
        return hit
    raise FileNotFoundError(f"no transcript for {arg!r} under {config / 'projects'}")


def transcript_files(main):
    """(path, is_subagent_file) for the main transcript and its subagent files."""
    files = [(main, False)]
    files += [(p, True) for p in sorted((main.parent / main.stem / "subagents").glob("*.jsonl"))]
    return files


def read_entries(path, is_sub, counters):
    """Yield (entry, from_subagent); malformed lines are counted, not fatal."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                entry = None
            if not isinstance(entry, dict):
                counters["malformed"] += 1
                continue
            yield entry, is_sub or entry.get("isSidechain") is True


def usage_of(message):
    usage = message.get("usage")
    return usage if isinstance(usage, dict) else None


def output_of(record):
    return int((record["usage"] or {}).get("output_tokens") or 0)


def keep_message(records, key, record):
    """Dedup rule: per message.id keep the entry with the largest output_tokens."""
    old = records.get(key)
    if old is None or old["usage"] is None:
        records[key] = record
    elif record["usage"] is not None and output_of(record) >= output_of(old):
        records[key] = record


def content_bytes(content):
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        text = block.get("text") if isinstance(block, dict) else None
        total += len((text if isinstance(text, str) else json.dumps(block)).encode("utf-8"))
    return total


def collect(main):
    """One pass over every file: deduped assistant records, tool names, tool results."""
    counters = {"malformed": 0}
    records, tool_names, results = {}, {}, []
    for path, is_sub in transcript_files(main):
        for entry, sub in read_entries(path, is_sub, counters):
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            if entry.get("type") == "assistant":
                key = message.get("id") or object()
                keep_message(records, key, {"usage": usage_of(message), "model": message.get("model"), "sub": sub})
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_names[block.get("id")] = block.get("name") or ""
            elif isinstance(message.get("content"), list):
                results += [(b, sub) for b in message["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    return counters, records, tool_names, results


def tally_tokens(records):
    tokens, peak, missing = {}, {}, 0
    for record in records.values():
        usage = record["usage"]
        if usage is None:
            missing += 1
            continue
        fam = family(record["model"])
        row = tokens.setdefault(fam, dict.fromkeys(TOKEN_FIELDS, 0))
        for name, field in TOKEN_FIELDS.items():
            row[name] += int(usage.get(field) or 0)
        if not record["sub"]:
            context = sum(int(usage.get(f) or 0) for f in TOKEN_FIELDS.values() if f != "output_tokens")
            peak[fam] = max(peak.get(fam, 0), context)
    return tokens, peak, missing


def tally_notion(tool_names, results):
    main_bytes = sub_bytes = largest = 0
    for block, sub in results:
        if not any(m in tool_names.get(block.get("tool_use_id"), "") for m in NOTION_MARKERS):
            continue
        size = content_bytes(block.get("content"))
        largest = max(largest, size)
        if sub:
            sub_bytes += size
        else:
            main_bytes += size
    return {"main_bytes": main_bytes, "subagent_bytes": sub_bytes, "largest_bytes": largest}


def measure(main):
    counters, records, tool_names, results = collect(Path(main))
    tokens, peak, missing = tally_tokens(records)
    return {
        "tokens": tokens,
        "peak_context": peak,
        "notion": tally_notion(tool_names, results),
        "malformed": counters["malformed"],
        "usage_missing": missing,
    }


def fmt_tokens(n):
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{round(n / 1000)}k"
    return f"{n / 1_000_000:.1f}M"


def fmt_kb(size):
    kb = size / KB
    if size == 0:
        return "0 KB"
    return f"{kb:.1f} KB" if kb < 10 else f"{kb:.0f} KB"


def format_line(result):
    parts = []
    for fam in (*FAMILIES, OTHER):
        t = result["tokens"].get(fam)
        if t and any(t.values()):
            parts.append(f"{fam} in {fmt_tokens(t['input'])} / cache-read {fmt_tokens(t['cache_read'])}"
                         f" / cache-write {fmt_tokens(t['cache_creation'])} / out {fmt_tokens(t['output'])}")
    peak = " · ".join(f"peak ctx {f} {fmt_tokens(v)}" for f, v in result["peak_context"].items() if v)
    n = result["notion"]
    notion = (f"notion {fmt_kb(n['main_bytes'] + n['subagent_bytes'])} (opus {fmt_kb(n['main_bytes'])}, "
              f"analyst {fmt_kb(n['subagent_bytes'])}; largest {fmt_kb(n['largest_bytes'])})")
    tail = [f"malformed {result['malformed']}"] if result["malformed"] else []
    tail += [f"usage_missing {result['usage_missing']}"] if result["usage_missing"] else []
    return " · ".join(["COST", *parts, *([peak] if peak else []), notion, *tail])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Report what a screener run consumed.")
    parser.add_argument("transcript", help="transcript .jsonl path or session id")
    parser.add_argument("--json", action="store_true", help="print JSON instead of the Run Log line")
    args = parser.parse_args(argv)
    try:
        result = measure(resolve_transcript(args.transcript))
    except OSError as exc:
        print(f"run_cost: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2) if args.json else format_line(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
