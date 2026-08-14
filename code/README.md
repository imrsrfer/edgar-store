# Local EDGAR fundamentals store + Gate 0 screener

A refreshable local store of GAAP fundamentals for every US SEC filer, plus a
screener that runs a fixed set of quality tests across the whole universe in one
pass. No database, no API keys, no cloud — Parquet files on disk.

Measured on this machine: **20,105 companies parsed, 1.82M facts, in 1.2 min**;
the screener runs over the universe in **under a second**.

---

## Install

```
python -m pip install polars pyarrow requests orjson pytest
```

Python 3.11+. `orjson` is optional but roughly halves parse time; the pipeline
falls back to the standard library without it.

## Run

Three stages, in order. Every stage is safe to re-run.

```
cd edgar
python fetch_edgar.py       # ~15 min cold, seconds when cached
python build_facts.py       # ~1.2 min
python gate0.py             # <1 s
```

Outputs land in `C:\Users\Fer\claude\Projects\Portfolio\edgar\` (override with
`--root` on any stage):

```
edgar/
  raw/              untouched downloads, cached; never re-fetched unless --force
  facts.parquet     tidy long table, one row per company/concept/period
  meta.parquet      cik, ticker, name, sic, fiscal year end, exchange, download date
  gate0.csv                the screener output (active filers only)
  data_quality.csv         every company where a required concept was missing, and which
  inactive_filers.csv      filers whose latest fiscal year predates --min-fiscal-year
  invalid_fiscal_years.csv facts SEC's own bulk data mislabelled with a corrupt fy value
```

## Refresh

SEC rebuilds `companyfacts.zip` daily.

```
python fetch_edgar.py --force     # re-pull the bulk archive (~1.4 GB)
python build_facts.py
python gate0.py
```

`--force` re-downloads everything. To refresh only the bulk facts and keep the
per-company metadata cache (which rarely changes and costs ~15 min), delete
`raw/companyfacts.zip` and re-run `fetch_edgar.py` without `--force`.

Interrupting `fetch_edgar.py` is safe. The bulk download resumes from its
`.part` file via an HTTP Range request, and the per-company metadata pull skips
every CIK already in `raw/company_meta.jsonl`.

`meta.parquet` carries `sec_download_date` so staleness is visible rather than
assumed.

---

## Data sources

**Primary — bulk company facts.**
`https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip`, ~1.4 GB,
one JSON per filer containing every XBRL fact that filer ever reported. Streamed
member by member; never unzipped to disk, never loaded whole into memory.

This is the primary path specifically because the **frames API is
calendar-aligned** and silently omits companies whose fiscal year does not end
near 31 December. That is not a small tail — it drops filers with June,
September, November and February year-ends. In this build, **over 15% of filers
with a known year-end are off-cycle**, which is why the frames API is not used
as a default anywhere in this pipeline and why `tests/test_gate0.py` pins APOG
(Apogee, ends the Saturday nearest end-February) as a regression test.

If you ever add a frames-API top-up
(`https://data.sec.gov/api/xbrl/frames/us-gaap/{TAG}/USD/{PERIOD}.json`), treat
it as a light supplement only and remember it cannot see off-cycle filers.

**Ticker↔CIK map.** `company_tickers.json` and `company_tickers_exchange.json` —
the bulk facts carry no tickers.

**Per-company metadata.** SIC codes are not in `companyfacts.zip` or the ticker
maps, and SEC's bulk `submissions.zip` returns **403** (verified — the whole
`daily-index/bulk/` path is blocked, directory listing included). Since the
default `--exclude-sic 6000-6799` screen needs SIC, the pipeline pulls it
per-CIK from `https://data.sec.gov/submissions/CIK##########.json`, restricted
to the ticker universe (7,999 companies) and cached append-only in
`raw/company_meta.jsonl`. That is the ~15 min cold cost, paid once.

### SEC request rules

Enforced in one place, `edgar_lib.py`:

- `User-Agent: Fer imrsrfer@gmail.com` on every request — mandatory; requests
  without it are rejected with 403.
- Rate limited to 9 req/s (under SEC's ceiling of 10) by a shared thread-safe
  limiter, so concurrency never breaches the cap.
- Exponential backoff on 429/502/503/504, capped at 60 s, six attempts.
- `Accept-Encoding: gzip, deflate` always sent.

Note: for a gzip-encoded response, `Content-Length` describes the *compressed*
stream, not the bytes written to disk. Range resumption and size verification
are therefore applied only to uncompressed responses; compressed ones restart
cleanly. (Getting this wrong is what makes a JSON download appear to be
"795,660 of 218,443 bytes".)

---

## Correctness rules

These matter more than speed, and each is enforced and tested.

**1. Tag chains are explicit, and the tag used is always recorded.**
Every concept has an ordered fallback chain; the first tag yielding a value for
that company-period wins, and `source_tag` records which. A silently substituted
tag is the main failure mode of a screener like this, so it is never invisible.

**2. Missing is never zero.**
If no tag in a chain matches, the value is **null**, not `0`. Nulls propagate
through every calculation: a company missing `sbc` has *unknown* FCF-after-SBC,
not higher FCF. Each test resolves to one of three states — `PASS`, `FAIL`,
`NOT_EVALUABLE` — rather than a boolean, because "the inputs were missing" and
"the company failed" are different claims (a bank with no operating-income
subtotal is `NOT_EVALUABLE` on `ni_vs_oi`, never `FAIL`). `gate0_pass` is true
only when the three load-bearing tests — `tangible_book`, `income_quality`,
`fcf_after_sbc` — are all `PASS`; the other tests are reported but do not gate
the verdict.

*Consequence worth knowing:* many small caps never tag `Goodwill` at all, so
their `tangible_book` is `NOT_EVALUABLE` rather than `PASS`. In this build that
is a large group — large enough that `goodwill`/`intangibles` are now in
`REQUIRED_CONCEPTS` and show up in `data_quality.csv`, even though a missing
goodwill tag is usually a legitimate zero. `--assume-absent-zero` opts into
treating an absent `goodwill` / `intangibles` / `total_debt` tag as zero. It is
**off by default** because conflating "absent" with "zero" manufactures
tangible-book passes and flatters net cash — exactly the false positives this
screen exists to catch. Any row where a value was imputed this way carries a
non-empty `imputed_fields`, and such a row can never show `gate0_pass = True`
unless `--allow-imputed` is also passed.

**3. Fiscal calendars are respected; nothing joins on calendar quarter.**
This is the subtle part. SEC's `fy`/`fp` fields describe *the filing a fact
appeared in*, not the fact's own period — a prior-year comparative inside the
FY2025 10-K still carries `fy=2025`. Keying on it directly mislabels every
comparative.

Instead, for each accession the pipeline identifies that filing's *primary*
period (its latest period end) and adopts SEC's label for it, building an
end-date → fiscal-label map from the company's own calendar. Where two filings
share a primary end date, the earliest-filed one wins, so results do not depend
on iteration order. Ends never seen as a primary period fall back to an offset
learned from the ones that were — which is what makes a January-ending retailer
resolve to the prior fiscal year, the way the company labels it itself.

Duration facts are classified by length: 340–400 days is annual, 80–100 days is
a discrete quarter. Nine-month year-to-date figures fall in the gap and are
dropped rather than mistaken for a quarter.

**4. Restatements resolve to the most recent filing.**
For each `(cik, concept, fiscal_year)` the row from the most recently filed
accession wins, with the accession number as tie-break. `restated = true` marks
any period where filings disagreed on the value.

**5. `data_quality.csv` lists what the screen could not see** — every company
missing a required concept, and which one. It also flags companies where the
balance-sheet and cash-flow measures of inorganic growth disagree by more than
20% (`acq_cf_bs_disagreement` — see rule 6).

**6. Companies that stopped filing are excluded, not counted as unscored.**
`companyfacts.zip` contains every entity that ever filed, including decades of
dead, delisted and deregistered ones. `--min-fiscal-year` (default 2024) keeps
them out of `gate0.csv`'s screened universe entirely; they land in
`inactive_filers.csv` instead. A company that was never a screening candidate
is not the same thing as one the screen failed to evaluate.

**7. Inorganic growth is read off the balance sheet, not the cash-flow tag.**
`PaymentsToAcquireBusinessesNetOfCashAcquired` is frequently absent even for
serial acquirers, which is how a missed acquisition line makes a company look
organic. `bs_acq_intensity` — the year-over-year change in
`goodwill + intangibles`, divided by revenue — uses data already in the store
and does not depend on that tag. The cash-flow-based `acq_intensity` is kept as
a cross-check; when the two disagree by more than 20% the company is flagged in
`data_quality.csv`.

---

## Column reference

### `facts.parquet` — one row per company/concept/period

| Column | Meaning |
|---|---|
| `cik` | SEC central index key (the real join key; tickers are not universal) |
| `entity_name` | Company name as it appears in the bulk facts |
| `concept` | Normalised concept name, e.g. `revenue`, `ocf`, `sbc` |
| `source_tag` | **The XBRL tag actually used.** `A+B` for a summed composite; `(partial)` when only one component of `total_debt` was reported |
| `unit` | `USD`, or `shares` for share counts |
| `fiscal_year` | The company's own fiscal year, never a calendar year |
| `fiscal_period` | `FY`, or `Q1`–`Q4` |
| `period_start` | Start date, null for balance-sheet instants |
| `period_end` | Period end / balance-sheet date |
| `value` | The reported figure |
| `form` | `10-K` or `10-Q` (amendments excluded, so no double counting) |
| `accn` | Accession number of the filing this value came from |
| `filed` | Filing date, which is what decides restatement precedence |
| `restated` | True when filings disagreed on this period's value |

### `meta.parquet`

| Column | Meaning |
|---|---|
| `cik`, `ticker`, `company_name` | Identity. `ticker` is null for filers absent from SEC's map |
| `sic`, `sic_description` | Industry code; null outside the ticker universe |
| `fiscal_year_end` | 4-char `MMDD`, e.g. `1231`, `0301`. **Keeps its leading zero** |
| `exchange` | Primary listing |
| `sec_download_date` | When the bulk archive was pulled — check this before trusting the numbers |

### `gate0.csv`

Identity and verdict:

| Column | Meaning |
|---|---|
| `gate0_pass` | True only when `tangible_book`, `income_quality`, `fcf` are all `PASS`, and `imputed_fields` is empty (or `--allow-imputed`) |
| `gate0_status` | `pass` / `fail` / `unknown`, from the load-bearing tests only |
| `gate0_not_evaluable` | Comma-separated list of tests (from all six) that were `NOT_EVALUABLE`, not just the load-bearing ones |
| `imputed_fields` | Comma-separated list of concepts assumed rather than reported (only non-empty under `--assume-absent-zero`) |
| `market_cap` | From `--mktcap-csv`; null when not supplied (prices are never invented) |
| `fcf_after_sbc_multiple` | `market_cap / fcf_after_sbc`; the sort key when caps are supplied |

Computed metrics, all for the latest reported fiscal year:

| Column | Formula |
|---|---|
| `tangible_book` | `equity - goodwill - intangibles` |
| `income_quality` | `ocf / net_income` |
| `fcf` | `ocf - capex` |
| `fcf_after_sbc` | `ocf - capex - sbc` |
| `sbc_pct_revenue` | `sbc / revenue` |
| `net_cash` | `cash - total_debt` |
| `effective_tax` | `tax_expense / pretax_income` |
| `ni_vs_oi` | `net_income / operating_income` |
| `acq_intensity` | `acquisitions / revenue` (cash-flow tag; frequently absent) |
| `bs_acq_intensity` | `Δ(goodwill + intangibles) / revenue`, year over year (balance-sheet cross-check; primary inorganic-growth signal) |
| `buyback_pct_fcf` | `buybacks / fcf_after_sbc` |
| `fcf_per_share` | `fcf_after_sbc / shares_diluted` |

Any ratio with a null input or a zero denominator is null, never zero and never
infinity.

Each test has both a legacy boolean `fail_*` column and a three-state
`test_*` column (`PASS` / `FAIL` / `NOT_EVALUABLE`); use `test_*` going
forward, `fail_*` is kept for backward compatibility:

| `fail_*` / `test_*` | Fails when |
|---|---|
| `fail_tangible_book` / `test_tangible_book` | `tangible_book < 0` (load-bearing) |
| `fail_income_quality` / `test_income_quality` | `income_quality < 0.80` (load-bearing) |
| `fail_fcf` / `test_fcf` | `fcf_after_sbc <= 0` (load-bearing) |
| `fail_sbc` / `test_sbc` | `sbc_pct_revenue > 0.15` |
| `warn_sbc` | `sbc_pct_revenue > 0.10` (warning only, not part of the verdict) |
| `fail_ni_over_oi` / `test_ni_vs_oi` | `net_income > operating_income` |
| `fail_tax_anomaly` / `test_tax_anomaly` | `effective_tax <= 0.05` |
| `warn_inorganic` | `bs_acq_intensity > 0.05` in any of the last 3 years (warning only) |
| `acq_cf_bs_disagreement` | `acq_intensity` and `bs_acq_intensity` disagree by >20% in any of the last 3 years (warning only) |

Trends, because direction matters more than level:

| Column | Meaning |
|---|---|
| `tangible_book_yrs_negative` | Count of negative years out of the last 5 |
| `revenue_cagr_3y`, `revenue_cagr_5y` | Compound revenue growth; null unless both endpoints are known and positive |
| `fcf_per_share_cagr_5y` | Compound growth in FCF per share — the sort key when no market caps are supplied |
| `operating_margin_latest`, `operating_margin_5y_ago`, `operating_margin_delta` | Margin level and drift |
| `income_quality_3y_avg` | Mean `ocf / net_income` over 3 years |
| `income_quality_direction` | `rising` / `falling` / `flat` |

TTM columns (`ttm_revenue`, `ttm_ocf`, `ttm_fcf_after_sbc`, …) are populated only
where four discrete quarters genuinely exist. Many filers report year-to-date
rather than discrete quarters, and a discrete Q4 is never filed on its own, so
these are frequently null by design rather than stitched from mismatched periods.

Finally, `source_tag_*` columns echo the XBRL tag chosen for each concept that
drives a verdict (`equity`, `goodwill`, `intangibles`, `ocf`, `capex`, `sbc`,
`net_income`, `operating_income`).

---

## CLI

```
python gate0.py --min-mktcap 500e6 --max-mktcap 5e9 --mktcap-csv caps.csv
python gate0.py --tickers MCRI,SKYW,CPRX
python gate0.py --tickers 1369568                  # by CIK
python gate0.py --include-financials
python gate0.py --exclude-sic 6000-6799,7370
python gate0.py --assume-absent-zero
python gate0.py --min-fiscal-year 2023
```

| Flag | Effect |
|---|---|
| `--exclude-sic` | SIC ranges to drop. Defaults to `6000-6799` — banks, brokers, insurers and REITs, where "FCF" is balance-sheet flow and every FCF multiple is meaningless |
| `--include-financials` | Keep them anyway |
| `--mktcap-csv` | A `ticker,market_cap` CSV. Market caps are not in EDGAR; without this the per-share and growth columns are still emitted and all multiples stay null |
| `--min-mktcap` / `--max-mktcap` | Cap band. Requires `--mktcap-csv`, and errors rather than silently returning nothing |
| `--tickers` | Ad-hoc lookup by ticker or bare CIK, same columns. Bypasses `--min-fiscal-year` too, since a lookup should find what you asked for. Reports anything it could not match |
| `--assume-absent-zero` | Treat an absent `goodwill` / `intangibles` / `total_debt` tag as zero. Off by default; see rule 2 above |
| `--allow-imputed` | Let a row with a non-empty `imputed_fields` show `gate0_pass = True`. Off by default |
| `--min-fiscal-year` | Exclude filers whose latest fiscal year predates this (default `2024`). Excluded rows go to `inactive_filers.csv`, not into the unscored count |

**Sorting:** ascending by `fcf_after_sbc_multiple` when market caps are supplied,
otherwise descending by `fcf_per_share_cagr_5y`.

**A note on tickers.** SEC's ticker map does not cover every filer that reports
XBRL facts. Catalyst Pharmaceuticals (CIK 1369568) is one live example: fully
present in `facts.parquet` with seven fiscal years of data, but absent from both
ticker maps, so `--tickers CPRX` cannot find it. That is why `--tickers` accepts
CIKs and prints what it failed to match.

---

## Tests

```
cd edgar
python -m pytest tests/ -q
```

21 tests, two layers. Unit tests over the fiscal-calendar logic and
null-propagation rules run anywhere with no data. Integration tests pin
hand-verified figures from real filings and skip themselves until
`build_facts.py` has run:

- **MCRI** (Monarch Casino, CIK 907242) FY2025 — tangible book ≈ +$510.7M,
  income quality ≈ 1.63, SBC ≈ 1.5% of revenue, goodwill $25.11M and flat across
  FY2021–FY2025 sourced from the `Goodwill` tag every year.
- **PRGS** FY2025 — tangible book ≈ −$1,415M, `fail_tangible_book` true.
- **CNXC** FY2025 — tangible book ≈ −$2,888M, `fail_tangible_book` true.
- **APOG** — present at all, with a non-December year-end. The regression test
  for the frames-API calendar bug.

### One place the spec and the data disagree

The spec pins "net cash positive" for MCRI. Monarch reports **no debt tag at
all** — not `LongTermDebtNoncurrent`, not `LongTermDebtCurrent`, not the combined
tag. It is genuinely debt-free, carrying only operating leases. Under rule 2
(missing is never zero) `total_debt` is therefore null and `net_cash` is
*unknown*, not positive.

Both behaviours are tested. `test_mcri_is_debt_free_so_net_cash_needs_the_opt_in`
asserts `net_cash` is null by default and becomes the correct positive figure
under `--assume-absent-zero`. If you would rather absent debt read as zero
everywhere, make `--assume-absent-zero` the default in `gate0.py` — but note it
also changes how `goodwill` and `intangibles` behave, and with them the
tangible-book verdict for every filer that omits those tags.

---

## Files

| File | Role |
|---|---|
| `concepts.py` | Concept → XBRL tag chains. The one place to edit when a tag needs adding |
| `edgar_lib.py` | Paths, polite HTTP (User-Agent, rate limit, backoff, resumable download), manifest I/O |
| `fetch_edgar.py` | Stage 1 — download and cache raw SEC data |
| `build_facts.py` | Stage 2 — parse the archive into `facts.parquet` + `meta.parquet` |
| `gate0.py` | Stage 3 — compute the tests, write `gate0.csv` + `data_quality.csv` |
| `screen.py` | Stage 4 — apply the shortlisting screens (`--lane main/shorthist/ifrs/inflection`) to `gate0.csv`. See its module docstring and `PRICES_SCHEMA.md` |
| `PRICES_SCHEMA.md` | The `ticker,price,ma_200,market_cap,as_of` contract `screen.py --price-csv` expects |
| `tests/test_gate0.py` | Unit and pinned-value tests |
