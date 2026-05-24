"""
Load Comtrade reference / mapping tables into MySQL.

This is a one-time (or refresh-when-changed) populator for the seven
lookup tables created by `comtrade_schema.sql`.  Handles two quirks
in the source CSVs that LOAD DATA INFILE doesn't get right:

  - cp1252 encoding (special chars in commodity descriptions, e.g. m²)
  - trailing comma on every data row -> 1 extra empty field per row
  - variable column counts in Country_Mapping_Data.csv (rows for
    historical entities like Former Yugoslavia have an extra
    entryExpiredDate column shoved in)

Reads connection settings from the same .env used by the loader.

Usage:
    python load_reference_tables.py            # load all tables
    python load_reference_tables.py country    # load just one
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_PORT = os.getenv("DB_PORT", "3306")

REFERENCE_DIR = Path(os.getenv("REFERENCE_DIR", "."))


def get_engine():
    missing = [k for k, v in dict(
        DB_USER=DB_USER, DB_PASS=DB_PASS, DB_HOST=DB_HOST, DB_NAME=DB_NAME,
    ).items() if not v]
    if missing:
        raise SystemExit(f"Missing env vars: {missing}")
    return create_engine(
        f"mysql+mysqlconnector://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
        future=True,
    )


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _parse_dt(value: str):
    """Parse 'YYYY-MM-DDTHH:MM:SS' or return None."""
    if not value or value.strip() == "":
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_bool(value: str) -> int:
    v = (value or "").strip().lower()
    if v in {"true", "1", "yes"}:
        return 1
    return 0


def _read_csv(path: Path, encoding: str):
    with path.open(encoding=encoding, newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = [r for r in reader if r and any(c.strip() for c in r)]
    return header, rows


# ---------------------------------------------------------------------
# Per-table loaders
# ---------------------------------------------------------------------

def load_simple_two_col(engine, table: str, code_col: str, desc_col: str,
                        csv_path: Path, encoding: str = "utf-8") -> int:
    """Load a 2-column lookup CSV (Frequency, Tradeflow, Transport, Consumption)."""
    _, rows = _read_csv(csv_path, encoding)
    records = [
        {"code": r[0].strip(), "desc": r[1].strip()}
        for r in rows if len(r) >= 2 and r[0].strip()
    ]
    sql = text(f"""
        REPLACE INTO {table} ({code_col}, {desc_col})
        VALUES (:code, :desc)
    """)
    with engine.begin() as conn:
        conn.execute(sql, records)
    return len(records)


def load_unit_quantity(engine, csv_path: Path) -> int:
    _, rows = _read_csv(csv_path, encoding="cp1252")
    records = []
    for r in rows:
        if len(r) < 3 or not r[0].strip():
            continue
        try:
            qty_code = int(r[0].strip())
        except ValueError:
            continue
        records.append({
            "qty_code": qty_code,
            "qty_abbr": r[1].strip(),
            "qty_description": r[2].strip(),
        })
    sql = text("""
        REPLACE INTO unit_quantity_mapping
            (qty_code, qty_abbr, qty_description)
        VALUES (:qty_code, :qty_abbr, :qty_description)
    """)
    with engine.begin() as conn:
        conn.execute(sql, records)
    return len(records)


def _looks_like_iso_alpha(value: str, length: int) -> bool:
    """True if value is exactly `length` characters of ASCII letters."""
    if value is None:
        return False
    v = value.strip()
    if len(v) != length:
        return False
    return all(c.isascii() and c.isalpha() for c in v)


def _looks_like_date(value: str) -> bool:
    return _parse_dt(value) is not None


def load_country(engine, csv_path: Path) -> int:
    """Country mapping has inconsistent column counts.

    Three observed shapes (after stripping the trailing-comma noise):
        8 fields:  reporterNote missing or iso3 missing
        9 fields:  standard layout matching the header
        10 fields: extra entryExpiredDate inserted before isGroup

    Field positions also shift when reporterNote or iso3 is missing.
    Rather than guessing positions, this parser locks down columns by
    content: isGroup is the last field (true/false), iso codes are
    pure ASCII letters of length 2/3, dates parse with fromisoformat,
    and the leading 4 fields (id, text, reporterCode, reporterDesc)
    are stable.

    ISO codes that don't pass strict validation (e.g. '_ZP', 'R4 ',
    or stray dates in the iso3 column) are stored as NULL rather than
    rejected -- this prevents data-too-long errors and keeps the
    country row.
    """
    _, rows = _read_csv(csv_path, encoding="cp1252")
    records = []
    for raw in rows:
        # Strip trailing empties (the file ends every row with ",")
        r = list(raw)
        while r and r[-1] == "":
            r = r[:-1]
        if len(r) < 6 or not r[0].strip():
            continue

        # Locked-down head: 4 fields always present
        country_code = r[0].strip()
        country_text = r[1].strip()
        # r[2] is duplicate reporterCode, r[3] is duplicate reporterDesc

        # Locked-down tail: isGroup is the last field
        is_group = _parse_bool(r[-1])

        # Middle = r[4:-1].  Walk it pulling out: reporter_note,
        # iso2, iso3, entry_effective_date, entry_expired_date.
        middle = [v.strip() for v in r[4:-1]]

        # Pull dates off the right end first (most reliable signal).
        entry_effective = None
        entry_expired = None
        while middle and _looks_like_date(middle[-1]):
            d = _parse_dt(middle.pop())
            if entry_expired is None:
                entry_expired = d
            else:
                # second date going right-to-left is the effective date,
                # the one we already popped is the expired date
                entry_effective = d
        if entry_effective is None and entry_expired is not None:
            # Only one date present -> it's the effective date, not expired
            entry_effective = entry_expired
            entry_expired = None

        # What remains in `middle` is: [reporter_note?, iso2?, iso3?]
        # Detect iso codes by content.
        iso2 = None
        iso3 = None
        # Scan from the right for an iso3 (3 alpha), then iso2 (2 alpha)
        # Use indexes so we can splice them out cleanly.
        i = len(middle) - 1
        while i >= 0:
            if iso3 is None and _looks_like_iso_alpha(middle[i], 3):
                iso3 = middle.pop(i)
                i -= 1
                continue
            if iso2 is None and _looks_like_iso_alpha(middle[i], 2):
                iso2 = middle.pop(i)
                i -= 1
                continue
            i -= 1

        # Anything still in `middle` is reporter_note (or junk we
        # don't want -- but for this dataset it's reporter_note).
        reporter_note = " ".join(middle).strip() or None

        records.append({
            "country_code": country_code,
            "country_text": country_text,
            "reporter_note": reporter_note,
            "iso_alpha_2": iso2,
            "iso_alpha_3": iso3,
            "entry_effective_date": entry_effective,
            "entry_expired_date": entry_expired,
            "is_group": is_group,
        })

    sql = text("""
        REPLACE INTO country_mapping (
            country_code, country_text, reporter_note,
            iso_alpha_2, iso_alpha_3,
            entry_effective_date, entry_expired_date, is_group
        ) VALUES (
            :country_code, :country_text, :reporter_note,
            :iso_alpha_2, :iso_alpha_3,
            :entry_effective_date, :entry_expired_date, :is_group
        )
    """)
    with engine.begin() as conn:
        conn.execute(sql, records)
    return len(records)


def load_commodity(engine, csv_path: Path) -> int:
    """Commodity codes -- cp1252 encoded, trailing comma per row."""
    _, rows = _read_csv(csv_path, encoding="cp1252")
    records = []
    for r in rows:
        while r and r[-1] == "":
            r = r[:-1]
        if len(r) < 5 or not r[0].strip():
            continue
        unit = r[5].strip() if len(r) >= 6 else None
        if unit and unit.lower() == "n/a":
            unit = None
        try:
            aggr_level = int(r[4].strip())
        except (ValueError, IndexError):
            aggr_level = 0
        records.append({
            "cmd_code": r[0].strip(),
            "cmd_text": r[1].strip(),
            "parent_code": r[2].strip() if r[2].strip() != "#" else None,
            "is_leaf": _parse_bool(r[3]),
            "aggr_level": aggr_level,
            "standard_unit_abbr": unit,
        })

    sql = text("""
        REPLACE INTO commodity_code_mapping (
            cmd_code, cmd_text, parent_code, is_leaf,
            aggr_level, standard_unit_abbr
        ) VALUES (
            :cmd_code, :cmd_text, :parent_code, :is_leaf,
            :aggr_level, :standard_unit_abbr
        )
    """)
    with engine.begin() as conn:
        # Insert in chunks to keep statements reasonable
        chunk = 500
        for i in range(0, len(records), chunk):
            conn.execute(sql, records[i:i + chunk])
    return len(records)


# ---------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------

LOADERS = {
    "frequency": lambda eng: load_simple_two_col(
        eng, "frequency_mapping", "freq_code", "freq_desc",
        REFERENCE_DIR / "Frequency_Mapping.csv",
    ),
    "tradeflow": lambda eng: load_simple_two_col(
        eng, "tradeflow_mapping", "flow_code", "flow_desc",
        REFERENCE_DIR / "Tradeflow_Mapping.csv",
    ),
    "transport": lambda eng: load_simple_two_col(
        eng, "transport_mapping", "mot_code", "mot_desc",
        REFERENCE_DIR / "Transport_Mapping.csv",
    ),
    "consumption": lambda eng: load_simple_two_col(
        eng, "consumption_mapping", "consumption_code", "consumption_desc",
        REFERENCE_DIR / "Consumption_Mapping.csv",
    ),
    "unit": lambda eng: load_unit_quantity(
        eng, REFERENCE_DIR / "Unit_Quantity_Mapping.csv",
    ),
    "country": lambda eng: load_country(
        eng, REFERENCE_DIR / "Country_Mapping_Data.csv",
    ),
    "commodity": lambda eng: load_commodity(
        eng, REFERENCE_DIR / "Commodity_Code_Mapping.csv",
    ),
}


def main(argv: list[str]) -> None:
    engine = get_engine()
    wanted = argv[1:] or list(LOADERS.keys())
    for name in wanted:
        if name not in LOADERS:
            print(f"Unknown table {name!r}.  Valid: {list(LOADERS)}")
            sys.exit(2)
        n = LOADERS[name](engine)
        print(f"  {name:12s}  {n:>6,} rows")


if __name__ == "__main__":
    main(sys.argv)