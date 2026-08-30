"""Stage 2: parse the bulk companyfacts archive into one tidy table on disk.

    python build_facts.py                # full rebuild
    python build_facts.py --limit 500    # quick smoke test

The archive is streamed member by member and never unzipped to disk. Each
company's JSON is parsed in a worker process, its facts resolved to concepts
there, and only the small resolved rows cross the process boundary.

Fiscal-calendar handling is the delicate part and is documented on
:func:`build_period_labels`. We never align on calendar quarters, because that
silently drops every filer whose year does not end near 31 December.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime

import polars as pl

try:
    import orjson

    def _loads(payload):
        return orjson.loads(payload)

except ImportError:  # stdlib fallback; correct, just slower

    def _loads(payload):
        return json.loads(payload)


import re

from concepts import (
    ANNUAL_DAYS,
    CONCEPTS,
    FY,
    QUARTER_DAYS,
    UNIT_SHARES,
    WANTED_TAGS,
    YTD2_DAYS,
    YTD3_DAYS,
)
from edgar_lib import Paths, log_stage, manifest_download_date

# Only annual and quarterly reports. Amendments (10-K/A) are excluded so one
# filing is not counted twice under two accessions. 20-F/40-F are the annual
# equivalents for foreign private issuers (20-F: most non-US filers under
# ifrs-full; 40-F: the Canadian MJDS wrapper, often still us-gaap -- see
# README for the Brookfield Asset Management example). 6-K carries interim
# filings for the same issuers and, empirically, sometimes labels genuine
# Q1-Q3 fiscal periods the same way 10-Q does.
KEPT_FORMS = frozenset(("10-K", "10-Q", "20-F", "40-F", "6-K"))
ANNUAL_FORMS = frozenset(("10-K", "20-F", "40-F"))
QUARTERLY_FORMS = frozenset(("10-Q", "6-K"))

# XBRL taxonomies parsed. us-gaap facts must report in USD; ifrs-full facts
# may report in any currency (foreign private issuers routinely do -- see
# _ANY_CURRENCY below) and are never converted.
TAXONOMY_GAAP = "us-gaap"
TAXONOMY_IFRS = "ifrs-full"

_CURRENCY_UNIT = re.compile(r"^[A-Z]{3}$")

# Earliest fiscal year retained, per spec.
MIN_FISCAL_YEAR = 2019

# Sanity bounds on a fiscal year label. SEC's bulk data occasionally carries a
# corrupt ``fy`` field (e.g. a dollar value where a year belongs), which then
# poisons every fact sharing that filing's primary period end. Labels outside
# this range are rejected rather than silently mislabeling a whole period.
VALID_FISCAL_YEAR_MIN = 1990
VALID_FISCAL_YEAR_MAX = 2030

# Companies handed to a worker per task. Large enough to amortise IPC overhead.
CHUNK_SIZE = 200

# Resolved rows are flushed to a Parquet part every this many companies.
FLUSH_EVERY = 2000

FACTS_SCHEMA = {
    "cik": pl.Int64,
    "entity_name": pl.Utf8,
    "concept": pl.Utf8,
    "source_tag": pl.Utf8,
    "taxonomy": pl.Utf8,
    "unit": pl.Utf8,
    "fiscal_year": pl.Int32,
    "fiscal_period": pl.Utf8,
    "period_start": pl.Date,
    "period_end": pl.Date,
    "value": pl.Float64,
    "form": pl.Utf8,
    "accn": pl.Utf8,
    "filed": pl.Date,
    "restated": pl.Boolean,
}


def _parse_date(text):
    if not text:
        return None
    try:
        return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except (ValueError, IndexError):
        return None


def _unit_accepted(unit, taxonomy):
    """USD/shares always; ifrs-full also accepts any 3-letter currency code.

    Foreign private issuers routinely report in a non-USD currency (CAD, EUR,
    BRL and 15+ others appear in the archive) -- rejecting non-USD would
    silently drop a large share of them, exactly the invisibility this exists
    to fix. Nothing here converts a currency; it only decides what to keep.
    """
    if unit in ("USD", "shares"):
        return True
    return taxonomy == TAXONOMY_IFRS and bool(_CURRENCY_UNIT.match(unit))


def _extract_taxonomy_facts(taxonomy_node, taxonomy):
    facts = []
    for tag, node in taxonomy_node.items():
        if tag not in WANTED_TAGS:
            continue
        for unit, entries in (node.get("units") or {}).items():
            if not _unit_accepted(unit, taxonomy):
                continue
            for entry in entries:
                if entry.get("form") not in KEPT_FORMS:
                    continue
                end = _parse_date(entry.get("end"))
                if end is None or entry.get("val") is None:
                    continue
                facts.append(
                    {
                        "tag": tag,
                        "taxonomy": taxonomy,
                        "unit": unit,
                        "start": _parse_date(entry.get("start")),
                        "end": end,
                        "value": entry["val"],
                        "accn": entry.get("accn"),
                        "fy": entry.get("fy"),
                        "fp": entry.get("fp"),
                        "form": entry.get("form"),
                        "filed": _parse_date(entry.get("filed")),
                    }
                )
    return facts


def extract_raw_facts(payload):
    """Pull the us-gaap and ifrs-full facts we care about out of one company's
    document.

    No fiscal-year filter yet: comparative periods inside a recent filing are
    how we learn about older years. A company's facts are (empirically)
    always concentrated in one taxonomy or the other, never meaningfully
    split, so both are parsed unconditionally rather than guessed at.
    """
    all_facts = (payload.get("facts") or {})
    facts = _extract_taxonomy_facts(all_facts.get("us-gaap") or {}, TAXONOMY_GAAP)
    facts += _extract_taxonomy_facts(all_facts.get("ifrs-full") or {}, TAXONOMY_IFRS)
    return facts


def build_period_labels(facts):
    """Map each period-end date to the company's own fiscal (year, period).

    SEC's ``fy``/``fp`` describe *the filing a fact appeared in*, not the fact's
    own period: a prior-year comparative inside the FY2025 10-K still carries
    fy=2025. Keying on it directly mislabels every comparative.

    So we work out, per accession, which period is that filing's *primary* one --
    the latest period end it reports -- and take SEC's label for that. The result
    is an end-date -> label map built from the company's own calendar, with no
    reference to calendar quarters. Off-cycle filers (February, June, September,
    November year-ends) come through exactly like December ones.

    Ends never seen as a primary period fall back to an offset learned from the
    ones that were, which is what makes a January-ending retailer resolve to the
    prior fiscal year the way the company itself labels it.
    """
    by_accession = defaultdict(list)
    for fact in facts:
        if fact["accn"]:
            by_accession[fact["accn"]].append(fact)

    annual, quarterly = {}, {}
    for entries in by_accession.values():
        head = entries[0]
        fiscal_year, period, form = head["fy"], head["fp"], head["form"]
        if not isinstance(fiscal_year, int):
            continue
        if not (VALID_FISCAL_YEAR_MIN <= fiscal_year <= VALID_FISCAL_YEAR_MAX):
            # Corrupt fy for this whole accession. Skip it here so it never
            # becomes a period label; find_invalid_fiscal_years reports it.
            continue
        primary_end = max(entry["end"] for entry in entries)
        filed = head["filed"] or date.max
        if form in ANNUAL_FORMS and period == FY:
            # Two filings can share a primary end date. Keep the label from the
            # earliest one, which is the filing the period was actually current
            # in, so the result does not depend on iteration order.
            existing = annual.get(primary_end)
            if existing is None or filed < existing[1]:
                annual[primary_end] = (fiscal_year, filed)
        elif form in QUARTERLY_FORMS and period in ("Q1", "Q2", "Q3"):
            existing = quarterly.get(primary_end)
            if existing is None or filed < existing[1]:
                quarterly[primary_end] = ((fiscal_year, period), filed)

    annual = {end: label for end, (label, _) in annual.items()}
    quarterly = {end: label for end, (label, _) in quarterly.items()}

    return annual, quarterly, _year_offset(annual)


def _year_offset(annual):
    """Typical ``fiscal_year - calendar_year_of_end``, for unlabelled ends.

    Zero for nearly everyone; -1 for filers whose year ends in January and who
    call that period the prior fiscal year.
    """
    if not annual:
        return 0
    offsets = Counter(label - end.year for end, label in annual.items())
    return offsets.most_common(1)[0][0]


def _duration_kind(start, end):
    """Classify a duration fact as annual, quarterly, cumulative YTD, or neither.

    Six- and nine-month year-to-date spans are RETAINED under their own labels
    (2026-08-27). They must never be mistaken for a discrete quarter -- that is
    what the separate YTD2/YTD3 labels guarantee, since every existing filter
    in this pipeline matches on "Q1".."Q4" or "FY" and so cannot pick them up --
    but dropping them entirely was worse. Filers report operating cash flow only
    as YTD, so discarding them left Q1 as the only surviving quarterly cash-flow
    fact and made a genuine trailing-twelve-month figure unconstructible for
    all but 3 companies in the store.
    """
    if start is None:
        return None
    days = (end - start).days
    if ANNUAL_DAYS[0] <= days <= ANNUAL_DAYS[1]:
        return FY
    if QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]:
        return "Q"
    if YTD2_DAYS[0] <= days <= YTD2_DAYS[1]:
        return "YTD2"
    if YTD3_DAYS[0] <= days <= YTD3_DAYS[1]:
        return "YTD3"
    return None


def label_fact(fact, annual, quarterly, offset):
    """Attach (fiscal_year, fiscal_period) to one fact, or None if unusable."""
    end = fact["end"]

    if fact["start"] is None:
        # Instant: belongs to whichever fiscal period closes on this date.
        if end in annual:
            return annual[end], FY
        if end in quarterly:
            return quarterly[end]
        return end.year + offset, FY

    kind = _duration_kind(fact["start"], end)
    if kind == FY:
        return annual.get(end, end.year + offset), FY
    if kind == "Q":
        if end in quarterly:
            return quarterly[end]
        # A discrete fourth quarter is never filed on its own; it shares the
        # year-end date with the annual period.
        if end in annual:
            return annual[end], "Q4"
        return None
    if kind in ("YTD2", "YTD3"):
        # Cumulative from the fiscal-year start. Labelled by SPAN, not by the
        # quarter it happens to end in, so it can never be summed as a quarter.
        if end in quarterly:
            return quarterly[end][0], kind
        if end in annual:
            return annual[end], kind
        return end.year + offset, kind
    return None


def _pick_latest(candidates):
    """Most recently filed value, with the accession number as tie-break."""
    return max(candidates, key=lambda f: (f["filed"] or date.min, f["accn"] or ""))


def find_invalid_fiscal_years(facts):
    """Raw facts whose ``fy`` field falls outside the plausible calendar range.

    Reported rather than silently dropped: a corrupt ``fy`` in the source data
    (SEC's, not ours) poisons the primary-period label for every fact sharing
    that accession's period end, which is what made 29953000 show up as a
    company's latest fiscal year.
    """
    rejects = []
    for fact in facts:
        fy = fact.get("fy")
        if isinstance(fy, int) and not (
            VALID_FISCAL_YEAR_MIN <= fy <= VALID_FISCAL_YEAR_MAX
        ):
            rejects.append(
                {
                    "tag": fact["tag"],
                    "fiscal_year_raw": fy,
                    "form": fact.get("form"),
                    "accn": fact.get("accn"),
                    "period_end": fact["end"],
                }
            )
    return rejects


def resolve_concepts(facts, annual, quarterly, offset):
    """Collapse raw tag-level facts into one row per (concept, period).

    The first tag in a concept's chain that yields a value for that period wins,
    and the winning tag is recorded. A concept whose whole chain is absent
    produces no row at all -- never a zero.
    """
    index = defaultdict(list)  # (tag, fiscal_year, fiscal_period) -> facts
    for fact in facts:
        label = label_fact(fact, annual, quarterly, offset)
        if label is None:
            continue
        fiscal_year, period = label
        if fiscal_year is None or fiscal_year < MIN_FISCAL_YEAR:
            continue
        fact["fiscal_year"], fact["fiscal_period"] = fiscal_year, period
        index[(fact["tag"], fiscal_year, period)].append(fact)

    periods = {(year, period) for _, year, period in index}
    rows = []
    for concept in CONCEPTS:
        for fiscal_year, period in periods:
            row = _resolve_one(concept, fiscal_year, period, index)
            if row is not None:
                rows.append(row)
    return rows


def _resolve_one(concept, fiscal_year, period, index):
    """Resolve a single concept for a single period, or return None.

    The us-gaap chain is tried first, in full, before ever looking at
    ifrs_chain: a coincidentally same-named tag in the wrong taxonomy (e.g.
    both have a tag literally called "Goodwill") must not change which chain
    wins for a given company, and a pure us-gaap or pure ifrs-full filer only
    ever has facts under one of the two anyway.
    """
    if concept.components:
        row = _resolve_components(concept, fiscal_year, period, index)
        if row is not None:
            return row

    for tag in concept.chain:
        candidates = _matching(index, tag, fiscal_year, period, concept)
        if candidates:
            return _make_row(concept, _pick_latest(candidates), tag, candidates)
    for tag in concept.ifrs_chain:
        candidates = _matching(index, tag, fiscal_year, period, concept, any_currency=True)
        if candidates:
            return _make_row(concept, _pick_latest(candidates), tag, candidates)
    return None


def _resolve_components(concept, fiscal_year, period, index):
    """Sum a composite concept's components (total_debt, capex).

    Both parts present is the clean case for total_debt. When only one is
    present we still use it -- a company with no current maturities genuinely
    omits that tag -- but ``source_tag`` is marked ``(partial)``, so a partial
    figure is never mistaken for a complete one.

    ``partial_ok`` concepts suppress that marker. capex has eleven disjoint
    legs and virtually every filer reports one or two, so marking those
    "(partial)" would attach the degraded-data marker to the ordinary case and
    make it meaningless where it matters. The sum itself is unchanged: what
    the filer reported is added up, and what it did not report contributes
    nothing -- never zero-substituted, never guessed.
    """
    found = []
    for tag in concept.components:
        candidates = _matching(index, tag, fiscal_year, period, concept)
        if candidates:
            found.append((tag, _pick_latest(candidates), candidates))
    if not found:
        return None

    tags = "+".join(entry[0] for entry in found)
    if len(found) < len(concept.components) and not getattr(concept, "partial_ok", False):
        tags += " (partial)"
    anchor = max(found, key=lambda entry: abs(entry[1]["value"]))[1]
    row = _make_row(concept, anchor, tags, ())
    row["value"] = float(sum(entry[1]["value"] for entry in found))
    row["restated"] = any(len({c["value"] for c in entry[2]}) > 1 for entry in found)
    return row


def _matching(index, tag, fiscal_year, period, concept, any_currency=False):
    """Facts for one tag/period that also match the concept's unit and shape.

    any_currency (set only when trying an ifrs_chain tag) accepts any
    currency unit rather than requiring USD exactly -- share counts are
    still matched exactly regardless, there is no "any currency" for shares.
    """
    candidates = index.get((tag, fiscal_year, period))
    if not candidates:
        return ()
    wants_instant = concept.is_instant

    def unit_ok(fact):
        if concept.unit == UNIT_SHARES or not any_currency:
            return fact["unit"] == concept.unit
        return fact["unit"] != UNIT_SHARES

    return [
        fact
        for fact in candidates
        if unit_ok(fact) and (fact["start"] is None) == wants_instant
    ]


def _make_row(concept, chosen, source_tag, candidates):
    return {
        "concept": concept.name,
        "source_tag": source_tag,
        "taxonomy": chosen.get("taxonomy", TAXONOMY_GAAP),
        # The fact's own reported unit, not the concept's declared one: an
        # ifrs_chain match may be EUR/CAD/etc, never converted or relabeled.
        "unit": chosen.get("unit", concept.unit),
        "fiscal_year": chosen["fiscal_year"],
        "fiscal_period": chosen["fiscal_period"],
        "period_start": chosen["start"],
        "period_end": chosen["end"],
        "value": float(chosen["value"]),
        "form": chosen["form"],
        "accn": chosen["accn"],
        "filed": chosen["filed"],
        "restated": len({c["value"] for c in candidates}) > 1,
    }


def _modal_fiscal_year_end(annual):
    """The company's own year-end as MMDD, learned from its primary periods."""
    if not annual:
        return None
    return Counter(end.strftime("%m%d") for end in annual).most_common(1)[0][0]


# One open ZipFile per worker process. Reopening it per member would re-parse
# the archive's 18k-entry central directory every time, which costs far more
# than reading the member itself.
_ARCHIVE_CACHE = {}


def _archive(zip_path):
    archive = _ARCHIVE_CACHE.get(zip_path)
    if archive is None:
        archive = zipfile.ZipFile(zip_path)
        _ARCHIVE_CACHE[zip_path] = archive
    return archive


def process_member(args):
    """Worker entry point: parse one zip member into resolved concept rows."""
    zip_path, member = args
    try:
        with _archive(zip_path).open(member) as handle:
            payload = _loads(handle.read())
    except (zipfile.BadZipFile, KeyError, ValueError, OSError):
        return None

    cik = payload.get("cik")
    if cik is None:
        return None
    entity_name = payload.get("entityName")
    facts = extract_raw_facts(payload)
    if not facts:
        return {"cik": int(cik), "entity_name": entity_name, "rows": []}

    invalid_years = find_invalid_fiscal_years(facts)
    for reject in invalid_years:
        reject["cik"] = int(cik)
        reject["entity_name"] = entity_name

    annual, quarterly, offset = build_period_labels(facts)
    rows = resolve_concepts(facts, annual, quarterly, offset)
    for row in rows:
        row["cik"] = int(cik)
        row["entity_name"] = entity_name
    return {
        "cik": int(cik),
        "entity_name": entity_name,
        "rows": rows,
        "invalid_fiscal_years": invalid_years,
        "fiscal_year_end": _modal_fiscal_year_end(annual),
    }


def _members(archive_path, limit=None):
    with zipfile.ZipFile(archive_path) as archive:
        names = sorted(n for n in archive.namelist() if n.lower().endswith(".json"))
    return names[:limit] if limit else names


def parse_archive(paths, limit=None, workers=None):
    """Stream the archive through a process pool, writing Parquet parts."""
    archive_path = str(paths.companyfacts_zip)
    names = _members(archive_path, limit)
    workers = workers or max(1, (os.cpu_count() or 4) - 1)
    parts_dir = paths.root / "_parts"
    _reset_parts(parts_dir)

    print(f"  {len(names):,} companies in archive, {workers} workers")
    buffer, part_index, parsed = [], 0, 0
    fiscal_year_ends, names_by_cik = {}, {}
    invalid_years = []
    started = time.monotonic()

    tasks = ((archive_path, name) for name in names)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(process_member, tasks, chunksize=CHUNK_SIZE):
            if result is None:
                continue
            parsed += 1
            names_by_cik[result["cik"]] = result["entity_name"]
            if result.get("fiscal_year_end"):
                fiscal_year_ends[result["cik"]] = result["fiscal_year_end"]
            invalid_years.extend(result.get("invalid_fiscal_years") or [])
            buffer.extend(result["rows"])
            if parsed % FLUSH_EVERY == 0:
                part_index = _flush(buffer, parts_dir, part_index)
                buffer = []
                _tick(parsed, len(names), started)

    _flush(buffer, parts_dir, part_index)
    print()
    total_rows = _combine(parts_dir, paths.facts)
    _write_invalid_fiscal_years(invalid_years, paths.invalid_fiscal_years)
    return {
        "companies_parsed": parsed,
        "facts_written": total_rows,
        "names": names_by_cik,
        "fye": fiscal_year_ends,
        "invalid_fiscal_years": len(invalid_years),
    }


def _write_invalid_fiscal_years(rejects, destination):
    """Write the corrupt-fy report, or an empty (header-only) file if none."""
    schema = {
        "cik": pl.Int64,
        "entity_name": pl.Utf8,
        "tag": pl.Utf8,
        "fiscal_year_raw": pl.Int64,
        "form": pl.Utf8,
        "accn": pl.Utf8,
        "period_end": pl.Date,
    }
    frame = pl.DataFrame(rejects, schema=schema) if rejects else pl.DataFrame(schema=schema)
    frame.write_csv(destination)


def _reset_parts(parts_dir):
    if parts_dir.exists():
        for stale in parts_dir.glob("*.parquet"):
            stale.unlink()
    parts_dir.mkdir(parents=True, exist_ok=True)


def _flush(buffer, parts_dir, part_index):
    if not buffer:
        return part_index
    frame = pl.DataFrame(buffer, schema=FACTS_SCHEMA)
    frame.write_parquet(parts_dir / f"part_{part_index:04d}.parquet")
    return part_index + 1


def _tick(parsed, total, started):
    elapsed = time.monotonic() - started
    rate = parsed / elapsed if elapsed else 0
    remaining = (total - parsed) / rate / 60 if rate else 0
    print(
        f"  {parsed:,}/{total:,} ({rate:.0f}/s, ~{remaining:.1f} min left)".ljust(70),
        end="\r",
        flush=True,
    )


def _combine(parts_dir, destination):
    """Concatenate the Parquet parts into the final sorted table."""
    parts = sorted(parts_dir.glob("*.parquet"))
    if not parts:
        pl.DataFrame(schema=FACTS_SCHEMA).write_parquet(destination)
        return 0
    frame = pl.concat([pl.read_parquet(part) for part in parts], how="vertical")
    frame = frame.sort(["cik", "concept", "fiscal_year", "fiscal_period"])
    frame.write_parquet(destination)
    for part in parts:
        part.unlink()
    parts_dir.rmdir()
    return frame.height


def load_company_meta(paths):
    """Per-CIK sic / fiscal-year-end / exchange from the cached submissions pull."""
    records = {}
    if not paths.company_meta_jsonl.exists():
        return records
    with open(paths.company_meta_jsonl, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not record.get("error"):
                records[int(record["cik"])] = record
    return records


def load_tickers(paths):
    """CIK -> [ticker, exchange], merged across the two SEC ticker maps."""
    tickers = {}
    if paths.tickers_json.exists():
        for row in json.loads(paths.tickers_json.read_text(encoding="utf-8")).values():
            tickers.setdefault(int(row["cik_str"]), [row["ticker"], None])
    if paths.tickers_exchange_json.exists():
        payload = json.loads(paths.tickers_exchange_json.read_text(encoding="utf-8"))
        index = {name: i for i, name in enumerate(payload.get("fields", []))}
        for row in payload.get("data", []):
            cik = int(row[index["cik"]])
            entry = tickers.setdefault(cik, [row[index["ticker"]], None])
            if entry[1] is None:
                entry[1] = row[index["exchange"]]
    return tickers


META_SCHEMA = {
    "cik": pl.Int64,
    "ticker": pl.Utf8,
    "company_name": pl.Utf8,
    "sic": pl.Utf8,
    "sic_description": pl.Utf8,
    "fiscal_year_end": pl.Utf8,
    "exchange": pl.Utf8,
    "sec_download_date": pl.Date,
}


def build_meta(paths, names, fiscal_year_ends):
    """Assemble meta.parquet from the ticker maps, submissions cache, and facts."""
    tickers = load_tickers(paths)
    submissions = load_company_meta(paths)
    stamp = manifest_download_date(paths)
    download_date = datetime.strptime(stamp, "%Y-%m-%d").date() if stamp else None

    rows = []
    for cik in sorted(set(names) | set(tickers) | set(submissions)):
        ticker, exchange = tickers.get(cik, (None, None))
        submission = submissions.get(cik, {})
        exchanges = submission.get("exchanges") or []
        rows.append(
            {
                "cik": cik,
                "ticker": ticker,
                "company_name": names.get(cik) or submission.get("name"),
                "sic": submission.get("sic"),
                "sic_description": submission.get("sic_description"),
                # SEC's own year-end label first. For a 52/53-week filer the
                # date derived from filings drifts across a month boundary
                # (Apogee lands on 0302 some years, 0228 others), so the
                # entity-level label is the stabler identifier. Derived value
                # covers filers with no submissions record.
                "fiscal_year_end": submission.get("fiscal_year_end")
                or fiscal_year_ends.get(cik),
                "exchange": exchange or (exchanges[0] if exchanges else None),
                "sec_download_date": download_date,
            }
        )
    frame = pl.DataFrame(rows, schema=META_SCHEMA)
    frame.write_parquet(paths.meta)
    return frame.height


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="output root directory")
    parser.add_argument("--limit", type=int, default=None, help="cap companies")
    parser.add_argument("--workers", type=int, default=None, help="process count")
    args = parser.parse_args(argv)

    paths = Paths(args.root).ensure()
    if not paths.companyfacts_zip.exists():
        parser.error(f"{paths.companyfacts_zip} not found. Run fetch_edgar.py first.")

    started = time.monotonic()
    print(f"Building facts from {paths.companyfacts_zip.name}")
    stats = parse_archive(paths, limit=args.limit, workers=args.workers)
    log_stage(
        "build:facts",
        companies_parsed=f"{stats['companies_parsed']:,}",
        facts_written=f"{stats['facts_written']:,}",
        invalid_fiscal_years=f"{stats['invalid_fiscal_years']:,}",
    )

    companies = build_meta(paths, stats["names"], stats["fye"])
    log_stage(
        "build:meta",
        companies=f"{companies:,}",
        sec_download_date=manifest_download_date(paths),
    )
    log_stage("build", elapsed_min=f"{(time.monotonic() - started) / 60:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
