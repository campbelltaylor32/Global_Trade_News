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


def load_country(engine, csv_path: Path) -> int:
    """Country mapping has variable column counts.

    Header: id, text, reporterCode, reporterDesc, reporterNote,
            ISO2, ISO3, entryEffectiveDate, isGroup
    But some rows have entryExpiredDate inserted between
    entryEffectiveDate and isGroup, so we read positionally based on
    field count.
    """
    _, rows = _read_csv(csv_path, encoding="cp1252")
    records = []
    for r in rows:
        # Strip trailing empty fields produced by the trailing comma.
        while r and r[-1] == "":
            r = r[:-1]
        if len(r) < 7 or not r[0].strip():
            continue

        # Position 0..6 are stable: id, text, reporterCode,
        # reporterDesc, reporterNote, ISO2, ISO3.
        # Some rows omit reporterNote -> 6 stable cols and shift.
        # Heuristic: ISO2 is always 2 chars, ISO3 is always 3 chars.
        # Find ISO2 by scanning for a 2-char alpha field.
        iso2_idx = None
        for i in range(2, min(len(r) - 1, 6)):
            if len(r[i].strip()) == 2 and r[i].strip().isalpha():
                iso2_idx = i
                break
        if iso2_idx is None:
            # Fallback to header positions
            iso2_idx = 5

        country_code = r[0].strip()
        country_text = r[1].strip()
        reporter_note = r[4].strip() if iso2_idx >= 5 else None
        iso2 = r[iso2_idx].strip() if iso2_idx < len(r) else None
        iso3 = r[iso2_idx + 1].strip() if iso2_idx + 1 < len(r) else None

        # Dates: starting after ISO3
        rem = r[iso2_idx + 2:]
        entry_effective = _parse_dt(rem[0]) if len(rem) >= 1 else None
        entry_expired = None
        is_group = 0

        if len(rem) == 2:
            # entryEffective, isGroup
            is_group = _parse_bool(rem[1])
        elif len(rem) >= 3:
            # entryEffective, entryExpired, isGroup
            entry_expired = _parse_dt(rem[1])
            is_group = _parse_bool(rem[2])

        records.append({
            "country_code": country_code,
            "country_text": country_text,
            "reporter_note": reporter_note,
            "iso_alpha_2": iso2 or None,
            "iso_alpha_3": iso3 or None,
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
