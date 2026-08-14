# `prices.csv` contract

Used by `screen.py --price-csv <path>` (the `--min-mktcap`/`--max-mktcap` band
and the momentum screen both depend on it). This file is expected to be
produced by hand, weekly. Read this before editing it.

## Header

The file must have exactly this header, in this order:

```
ticker,price,ma_200,market_cap,as_of
```

| Column | Required | Type | Meaning |
|---|---|---|---|
| `ticker` | **yes** | string | Matched case-insensitively (uppercased on load) against `gate0.csv`'s own `ticker` column. Exact match only. |
| `price` | **yes** | number | Current price, in USD. Never converted from another currency -- if the underlying company's `reporting_currency` isn't USD, that mismatch is on the reader, not silently fixed here. |
| `ma_200` | no | number | 200-day moving average, USD. If a row omits it, that row's momentum screen is skipped (`momentum_not_evaluated = True`) -- it is never silently treated as a pass. |
| `market_cap` | no | number | Pre-computed market cap, USD. If a row omits it, `screen.py` derives `market_cap = price * shares_diluted` from the store's own share count and sets `market_cap_derived = True` on that row -- the store's share count lags real-time buybacks, so a supplied cap is always preferred when you have one. |
| `as_of` | **yes** | ISO date (`YYYY-MM-DD`) | The date the price is from. Rows older than 7 days are kept but printed as a warning at load time -- old prices are not an error, but a screen run against them should say so. |

## Validation (on load, fails loudly)

- **Missing required column** (`ticker`, `price`, or `as_of`): `screen.py` exits immediately with an error naming the missing column(s). Nothing is screened.
- **Missing/unparseable `as_of`** on any row: exits immediately, naming the row count. A price with no date attached cannot be judged fresh or stale, so it is refused outright rather than assumed current.
- **Duplicate `ticker`**: exits immediately, naming the duplicated ticker(s). `screen.py` will not guess which of two rows for the same company is the current one -- fix the file.
- **Unknown ticker** (not present in the `gate0.csv` universe): not an error. The row is written to `prices_unmatched.csv` (in the data root) and excluded from the run, so a typo or a delisted/renamed ticker is visible rather than silently dropped.
- **Stale `as_of`** (more than 7 days old): not an error. Printed as a warning; the row is still used.

## Worked example

```csv
ticker,price,ma_200,market_cap,as_of
CPA,142.50,131.20,,2026-08-08
MSFT,412.30,398.10,3060000000000,2026-08-08
XYZQ,10.00,,,2026-08-08
```

- **CPA**: `market_cap` omitted -> derived as `142.50 * shares_diluted` from the store, `market_cap_derived = True`. `ma_200` present -> momentum evaluated normally.
- **MSFT**: `market_cap` supplied directly -> used as-is, `market_cap_derived = False`. Prefer this whenever you actually have a real-time cap; it accounts for buybacks the store's `shares_diluted` (from the last filed 10-K/10-Q) cannot see yet.
- **XYZQ**: no `ma_200` -> `momentum_not_evaluated = True` for this row specifically, regardless of what the CLI's `--price-csv` flag did for every other row. If `XYZQ` isn't an actual ticker in `gate0.csv`, this row also lands in `prices_unmatched.csv`.

## What this file is not

- Not a source of truth for shares outstanding -- that comes from the store's own `shares_diluted` (from filed financials). This file supplies price only (and, optionally, a fresher market cap or a 200-day average).
- Not currency-converting. `price` and `market_cap` here are always USD; a company whose `reporting_currency` in `gate0.csv` is not USD will produce a cross-currency multiple if you mix them naively -- that is a read-time judgment call, not something this loader resolves for you.
- Not a place to infer a missing price from a prior run. A ticker absent from this week's file has no price this week, full stop -- `screen.py` never carries a stale price forward.
