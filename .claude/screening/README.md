# Screening agents: Opus decides, Sonnet runs the numbers

One collaborative task, not a second system. The **Opus pass** cowork routine
(`trig_016Pk9vt6CJd3exTtYVRVPy3`) runs as the decider, and the decider works
with one Sonnet analyst per run. The weekly routine
(`trig_01BVNkgoHoDxPuHNiKqETPfc`) is unchanged: it still holds the job
definition, runs its Sonnet pass, and hands off to the Opus pass.

| File | Model | Role |
|---|---|---|
| `.claude/agents/screening-decider.md` | `claude-opus-5-5` | Loads the job definition from the weekly routine and runs it as an OPUS RUN. It owns every call and every Notion write |
| `.claude/agents/screening-analyst.md` | `claude-sonnet-5-5` | Works only for the decider. It does Step 0, pricing, the four mandatory checks, script runs, arithmetic and read-only Notion LOOKUPs, and replies with short fact packs and verbatim extracts. Its tool list contains only read tools |
| `.claude/screening/fact_pack.py` | none (stdlib) | Deterministic store pulls: `store` prints age and the column checks; `rows T… [--quote T=PRICE,CAP]` prints per-ticker fact packs |

## How it runs

- **Cowork (the normal path):** fire the Opus pass routine, or let the weekly
  task's hand-off fire it. Its prompt clones this repo, copies the agent files
  into the session's `./.claude/agents/`, and runs as the decider. If the
  analyst type does not load, the decider falls back to a general-purpose
  Sonnet agent carrying the analyst instructions, and says so on line 1.
- **Locally (testing only):** `claude --agent screening-decider "Run the
  Opportunity Screener Opus pass."` Do not run it alongside the routine. Rows
  are owned by their Notion state, but two runs would still double-spend.

## Cost design (after the 2026-10-08 test: $2.52 per row against $0.89 Opus-only)

1. **One analyst per run**, kept alive with `SendMessage`, instead of one per
   ticker. Every spawn is a cold start.
2. **An explicit tool list** on the analyst. It used to inherit every connector
   (Figma, Vercel, Carta, Gmail …) on each start. Server-level names
   (`mcp__Stocklake`) cover a whole server, and both naming styles
   (`mcp__X__`, `mcp__claude_ai_X__`) are listed, since unknown names are ignored.
3. **Short replies**: about 12 lines per ticker, no JSON, no pasted pages.
4. **Notion through the analyst.** The console is ~65k tokens, and Opus
   re-read it on every turn. The analyst reads it whole and returns a verbatim
   digest. This departs from the SHARED HEADER's "read the whole console"
   line, and every run declares it on line 1 as `console: analyst digest`.

Every run's Run Log block ends with an `EFFICIENCY` line (rows, analysts
spawned, messages, metrics re-verified, disagreements, whether Opus fetched the
console). Compare cost per dispositioned row against the $0.89 Opus-only
baseline.

## Prices and the sweep cycle (Fer, 2026-10-09)

**Price sources, in order.** Bulk: the committed `store/prices.csv`, built on
Fer's PC with each store rebuild. Live, per decided row: IBKR for price and
200d MA, stockanalysis for market cap, with a 2% two-source check. Stocklake
is used only when both fail for a ticker (200 tickers a day, silent ticker
swaps).

**Fer's store rebuild, on his PC, about once per earnings season:**
```
python fetch_edgar.py --force
python build_facts.py
python gate0.py
python build_prices.py --from-framework-pass --max-age-days 7
python sync_to_repo.py
```
`build_prices.py` writes `<root>/prices.csv`, which the sync commits as
`store/prices.csv`.

**The cycle ends.** One cycle per store build: sweep, then MODE B until the
queue is empty, then one watchlist re-look, then `SWEEP STATE: COMPLETE` on the
Review Queue header. Runs idle until the next rebuild. Refill (`direct`) names
are queued only on request.
