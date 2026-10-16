"""One-time builder for the frozen Gate 0 fixture (audit 2026-10-15 #12).

NOT run by the test suite. It cuts facts.parquet / meta.parquet down to the
failure-mode filers the auditor named and writes SHA256SUMS. The expected
outputs in test_frozen_fixture.py are hand-verified and pinned by hand; they
are never regenerated from this script or from the implementation. Re-running
this changes the hashes, so test_fixture_hashes_are_pinned fails until a human
reviews the new fixture and updates SHA256SUMS on purpose.

    python tests/fixtures/build_fixture.py            # from edgar/
"""
import hashlib
import sys
from pathlib import Path

import polars as pl

HERE = Path(__file__).resolve().parent
OUT = HERE / "store"
SRC = HERE.parents[1]  # edgar/

# Required by the auditor, plus every sibling CIK a merge or duplicate
# resolution needs so the fixture reproduces the same single surviving row.
FIXTURE_CIKS = {
    34088: "XOM (successor)", 2115436: "XOM holdco, merged into 34088",
    1803599: "CNXC", 1030997: "WILCF", 1878057: "SGHC", 1764757: "DIDIY",
    1990950: "SELXF", 1594805: "SHOP", 1654126: "ZIM",
    2082866: "PNFP", 1115055: "PNFP sibling", 2115119: "CLBK", 1723596: "CLBK sibling",
    922864: "AIV", 1820878: "AIV sibling",
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ciks = list(FIXTURE_CIKS)
    for name in ("facts.parquet", "meta.parquet"):
        frame = pl.read_parquet(SRC / name).filter(pl.col("cik").is_in(ciks))
        frame.sort(frame.columns).write_parquet(OUT / name, statistics=False)
        print(f"{name}: {frame.height} rows")
    sums = "".join(f"{sha256(OUT / n)}  {n}\n" for n in ("facts.parquet", "meta.parquet"))
    (HERE / "SHA256SUMS").write_text(sums, encoding="utf-8")
    print(sums, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
