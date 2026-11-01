---
name: edgar-store-patterns
description: Conventions for changing the edgar-store repo (SEC EDGAR XBRL store, Gate 0, screen lanes, screening agents). Use when editing code/*.py, code/tests, code/README.md, .claude/agents or .claude/screening, writing commits or PRs here, or adding a store defect flag or Gate 0 column.
version: 1.0.0
source: local-git-analysis
analyzed_commits: 117
---

# edgar-store patterns

Taken from 117 commits (66 automated store syncs, 15 merges, 36 hand-written) up to 2026-11-01.

## Commit conventions

- **Hand-written commits use conventional prefixes**, consistently since mid-October 2026: `fix:` and `docs:` most often, then `test:`, `feat:`, `chore:` and `ci:`. Older commits are free-form imperatives ("Screening agents: …", "Add Opus screening-decider…"). Don't copy that older style.
- **Subjects name the behaviour and the real filers that showed it:** `fix: revenue picks the side-by-side total (SEZL, RUSHA)`, `feat: … (IONQ shape)`. Tickers in a subject are evidence, not decoration.
- **Bodies cite the review finding and its severity, then the measured blast radius from a full rebuild**, e.g. "Review findings 1 (HIGH) and 5 (MEDIUM)… 778 revenue fact rows across 277 filers move (634 down, 144 up)… gate0_pass: zero row changes." Say whether gate0 pass/status moved.
- **The last line is a trailer:** `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Never hand-write `store sync <UTC> (built <UTC>)` commits.** The external `sync_to_repo.py` publication gate writes them; they touch `MANIFEST.json`, `store/*` and the README hash only.
- **Orchestrated waves merge as `Merge W1: …` / `Merge F2: …`**: one worktree per wave, then a merge commit that names the wave.
- **Docs follow the code in their own commit**, `docs: document <new_column>`. A code change and its README update are often separate commits in the same PR.

## Architecture

```
code/
  fetch_edgar.py     download companyfacts.zip + ticker maps (raw/)
  build_facts.py     zip -> store/facts.parquet + meta.parquet (period labels, restatements, --as-of)
  concepts.py        tag chains per concept; first match wins, never summed across tags
  gate0.py           store -> store/gate0.csv (Gate 0 legs, TTM, flags); largest, most-changed module
  store_defects.py   fail-open detectors -> store_defect_flags column
  screen.py          lanes -> store/shortlist_<lane>.csv (value, accel, inflection, margin2y, ifrs, shorthist, unevaluated)
  rank_queue.py      ranking for the Review Queue
  build_prices.py    prices.csv (contract in PRICES_SCHEMA.md); fx.py for USD conversion
  edgar_lib.py       Paths (single data root), manifest helpers, log_stage
  scorecard.py       grades past dispositions vs later returns (outputs never go in the repo)
  tests/             pytest; fixtures/ = frozen, hash-pinned store slice
.claude/agents/      screening-decider (Opus, decides, writes Notion) + screening-analyst (Sonnet, read-only)
.claude/screening/   stdlib helper scripts for the analyst (fact_pack.py, run_cost.py)
store/               published, generated data. Only the sync gate writes it.
MANIFEST.json        store vintage, publication gate result, per-file hashes
```

## Data rules the code enforces (don't break them)

- **Missing is never zero.** A null stays null and nothing is computed from it. Tested-and-clean is `False`; not tested is `null`, often with a companion `*_tested` column. `ttm_unavailable` means NOT MEASURED, which is not "measured and clean".
- **A test whose input is absent is not a pass.** Use `NOT_MEASURED` / `ABSENT` (fact_pack.py) rather than a default.
- **Flags are vetoes or obligations, never qualifiers.** A defect flag can block or owe a check; it never promotes a name.
- **Fiscal calendars rule.** Never join on calendar quarters. Duration is classified by days (annual 340–400, quarter 80–100).
- **No portfolio data in the repo:** queue_ranked.csv, tracked.txt, scorecard output, decisions. Scripts that write such outputs refuse paths inside the repo.

## Workflows

### Add a store defect / Gate 0 column (the most common change)
1. Write the failing test first in a focused `code/tests/test_<topic>.py` (e.g. `test_share_count_live.py`, `test_capex_floor.py`, `test_latest_q_stale.py`).
2. Implement in `gate0.py` and/or `store_defects.py`. Touch `concepts.py` / `build_facts.py` only if the tag chain or resolution is wrong at the source.
3. Distinguish null (untested) from False (clean), and add a `*_tested` marker if a reader could confuse them.
4. Pin real filers in integration tests (live-store fixtures) plus a synthetic frozen case.
5. Run `cd code && python -m pytest -q` (the full suite is about 40 s; ~530–570 tests at the time of writing).
6. Document the column in `code/README.md`, and add it to the analyst's checks in `.claude/agents/screening-analyst.md` / the screener job table if runs must act on it.
7. Commit: `fix:` or `feat:` with tickers in the subject and the blast radius in the body; a separate `docs:` commit is fine.

### Change the screening agents
- Edit `.claude/agents/screening-*.md` (repo; the Opus pass clones `main` each run).
- The job definition itself lives in the cowork routine. Its canonical source is `Portfolio/routines/screener.md` outside this repo, delivered as a paste-ready file, never via RemoteTrigger update.
- `code/tests/test_agent_rules.py` holds contracts on the agent files. It skips when `.claude/` is absent.

### Store rebuild (operator flow, not a code commit)
fetch_edgar → build_facts → gate0 → build_prices → screen lanes → the external `sync_to_repo.py`, which commits only if the full suite passes with `EDGAR_REQUIRE_STORE=1`.

## Testing patterns

- **`conftest.py` redirects `DEFAULT_ROOT` to a per-session temp copy of the store and makes `Paths` refuse the real root.** Never bypass this. A 2026-09-09 test run truncated the live data_quality.csv. Any new entry-point test must go through `--root` / tmp paths.
- **Two tiers.** Frozen tests (`-m "not integration"`) are network-free and BLOCK merges in CI. Integration tests use a live-store fixture (`universe`, `annual_facts`, `gate0_csv_frame`, `live_rows`, …) or `@pytest.mark.integration`; they don't block CI but do block publication.
- **The frozen fixture** (`tests/fixtures/store`) is hash-pinned in `SHA256SUMS`. Expected outputs are hand-verified. Never regenerate them from the implementation.
- **Live-ticker cases retire when filings land.** When a new 10-Q fixes the shape a test pinned, drop that ticker with a `test:` commit (e.g. "retire LSTR … after its Q3 10-Q landed").
- **Network code has a provider seam** (e.g. `get_closes` in scorecard.py), and tests use fakes. No network in tests.
- **CI pins** `ubuntu-24.04` and Python `3.14.4` (`ubuntu-latest` has no 3.14.4 build).
- **Style:** functions under 50 lines, files under 800, immutable updates (build new lists/frames), polars for frames, stdlib only in `.claude/screening/`.
