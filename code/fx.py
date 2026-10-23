"""Offline reporting-currency conversion for USD market-cap multiples.

Optional fx.csv columns: currency, usd_per_unit, as_of (YYYY-MM-DD).
Rates must be positive, finite, and dated within the last seven days.
"""

from datetime import date, timedelta
from pathlib import Path

import polars as pl

FX_MAX_AGE_DAYS = 7


def load_fx(path):
    """Load an optional local rate table; never fetch or infer a currency."""
    if not Path(path).exists():
        return None
    rates = pl.read_csv(path, infer_schema_length=0)
    required = {"currency", "usd_per_unit", "as_of"}
    if not required.issubset(rates.columns):
        raise SystemExit(f"{path}: FX requires currency,usd_per_unit,as_of")
    rates = rates.select(
        pl.col("currency").str.strip_chars().str.to_uppercase(),
        pl.col("usd_per_unit").cast(pl.Float64, strict=False),
        pl.col("as_of").str.to_date(strict=False),
    )
    if rates["currency"].is_duplicated().any():
        raise SystemExit(f"{path}: duplicate FX currencies are ambiguous")
    return rates


def add_usd_fcf(frame, rates=None, today=None):
    """Carry the dated conversion and withhold cash when FX is owed."""
    today = today or date.today()
    if "reporting_currency" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias("reporting_currency"))
    # Recompute on every run; a previous lane's conversion may now be stale.
    frame = frame.drop([c for c in ("fx_usd_per_unit", "fx_as_of", "fx_owed", "fcf_after_sbc_usd")
                        if c in frame.columns])
    currency = pl.col("reporting_currency").cast(pl.String).str.strip_chars().str.to_uppercase()
    frame = frame.with_columns(currency.alias("_fx_currency"))
    if rates is not None:
        frame = frame.join(rates.select(
            pl.col("currency").alias("_fx_currency"),
            pl.col("usd_per_unit").cast(pl.Float64, strict=False).alias("_fx_rate"),
            pl.col("as_of").cast(pl.String).str.to_date(strict=False).alias("_fx_date"),
        ), on="_fx_currency", how="left")
    else:
        frame = frame.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("_fx_rate"),
            pl.lit(None, dtype=pl.Date).alias("_fx_date"),
        )
    known = pl.col("_fx_currency").is_not_null() & ~pl.col("_fx_currency").is_in(["", "UNKNOWN", "UNRESOLVED"])
    unresolved = (pl.col("currency_unresolved").cast(pl.String).str.to_lowercase().is_in(["true", "1"]).fill_null(False)
                  if "currency_unresolved" in frame.columns else pl.lit(False))
    usd = (pl.col("_fx_currency") == "USD").fill_null(False) & ~unresolved
    usable = (known & ~unresolved & pl.col("_fx_rate").is_finite()
              & (pl.col("_fx_rate") > 0)
              & pl.col("_fx_date").is_between(today - timedelta(days=FX_MAX_AGE_DAYS), today)).fill_null(False)
    frame = frame.with_columns(
        pl.when(usd).then(1.0).when(usable).then(pl.col("_fx_rate")).otherwise(None).alias("fx_usd_per_unit"),
        pl.when(~usd & usable).then(pl.col("_fx_date")).otherwise(None).alias("fx_as_of"),
        (~(usd | usable)).alias("fx_owed"),
    )
    cash = (pl.col("fcf_after_sbc").cast(pl.Float64, strict=False)
            if "fcf_after_sbc" in frame.columns else pl.lit(None, dtype=pl.Float64))
    return frame.with_columns((cash * pl.col("fx_usd_per_unit")).alias("fcf_after_sbc_usd")).drop(
        "_fx_currency", "_fx_rate", "_fx_date")
