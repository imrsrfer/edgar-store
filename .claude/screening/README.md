# Screening agents: Opus decides, Sonnet runs the numbers

Two Claude Code subagents for the Opportunity Screener, modelled on the
two Opportunity Screener routines (weekly `trig_01BVNkgoHoDxPuHNiKqETPfc` and the
Opus pass `trig_016Pk9vt6CJd3exTtYVRVPy3`).

| Agent | Model | Role |
|---|---|---|
| `.claude/agents/screening-decider.md` | `claude-opus-5-5` | Loads the job definition from the weekly routine and runs it as an OPUS RUN. It owns every call: AUDIT verdicts, OPUS-OWED and normal-row dispositions, MODE A/B/C, promotions, Notion writes, hand-off |
| `.claude/agents/screening-analyst.md` | `claude-sonnet-5-5` | Works only for the decider. It pulls store figures, does Step 0, runs the pipeline scripts, prices tickers, runs the four mandatory checks and does arithmetic, then returns a fixed-shape FACT PACK. It cannot write to Notion, IBKR, triggers or the repo |
| `.claude/screening/fact_pack.py` | none (stdlib) | Deterministic pulls the analyst uses. `store` prints age and the five column checks. `rows T…` prints a per-ticker fact pack, and with `--quote T=PRICE,CAP` adds the share-count gap and live P/FCF |

## Run

The decider must be the **main** agent, because subagents cannot spawn
subagents:

```
claude --agent screening-decider "Run the Opportunity Screener Opus pass."
```

Or set `"agent": "screening-decider"` in `.claude/settings.json`.

The job definition is **not copied** into these files. The decider fetches it
from the weekly routine each run, so the agents cannot drift from it. Notion
wins over the job text, and the job text wins over the agent files.

## Notes

- These files sit under `.claude/`, outside `code/` and `store/`, so
  `sync_to_repo.py` should not prune them.
- An unattended run needs Bash allow-rules for the analyst's commands
  (`python .claude/screening/fact_pack.py`, `python code/*.py`, `git clone`),
  or it will stop on permission prompts.
