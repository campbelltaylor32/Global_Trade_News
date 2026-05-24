"""
load_search_terms.py
─────────────────────────────────────────────────────────────────────
One-shot loader for the commodity_search_terms table.

Reads the CSV produced by generate_search_terms.py (or hand-edited by
you), ensures any missing chapter-level cmd_codes exist as placeholders
in commodity_code_mapping (so the FK on commodity_search_terms won't
reject inserts), then INSERT IGNOREs the rows.

Re-running is safe: the UNIQUE KEY on
(cmd_code, search_term, language) ignores duplicates so you can edit
the CSV and re-run without manual cleanup.

Usage
─────
  # Default: load commodity_search_terms.csv from current dir
  python load_search_terms.py

  # Custom path
  python load_search_terms.py --csv path/to/terms.csv

  # Wipe-and-reload (caution: drops any manual additions you made
  # directly in MySQL that aren't in the CSV)
  python load_search_terms.py --replace
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text

load_dotenv()

DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME")


def make_engine():
    missing = [k for k, v in dict(
        DB_USER=DB_USER, DB_PASS=DB_PASS, DB_HOST=DB_HOST, DB_NAME=DB_NAME,
    ).items() if not v]
    if missing:
        raise SystemExit(f"Missing DB env vars: {missing}")
    return create_engine(
        f"mysql+mysqlconnector://{quote_plus(DB_USER)}:{quote_plus(DB_PASS)}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}",
        pool_pre_ping=True,
    )


def ensure_chapter_rows(engine, cmd_codes: set[str]) -> None:
    """Insert placeholder rows into commodity_code_mapping for any
    chapter code in the CSV that isn't already there."""
    if not cmd_codes:
        return
    with engine.begin() as conn:
        existing = {
            row[0] for row in conn.execute(
                text("SELECT cmd_code FROM commodity_code_mapping "
                     "WHERE cmd_code IN :codes").bindparams(
                    bindparam("codes", expanding=True)
                ),
                {"codes": list(cmd_codes)},
            )
        }
        missing = cmd_codes - existing
        if not missing:
            return
        print(f"  Inserting {len(missing)} placeholder chapter rows into "
              f"commodity_code_mapping: "
              f"{', '.join(sorted(missing)[:15])}"
              f"{'...' if len(missing) > 15 else ''}")
        conn.execute(
            text(
                "INSERT IGNORE INTO commodity_code_mapping "
                "(cmd_code, cmd_text, parent_code, is_leaf, aggr_level) "
                "VALUES (:cmd_code, :cmd_text, NULL, 0, 2)"
            ),
            [{"cmd_code": c, "cmd_text": f"HS Chapter {c} (placeholder)"}
             for c in sorted(missing)],
        )


def load_csv(path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            cmd_code = (r.get("cmd_code") or "").strip()
            term     = (r.get("search_term") or "").strip()
            if not cmd_code or not term:
                continue
            rows.append({
                "cmd_code":    cmd_code,
                "search_term": term,
                "term_type":   (r.get("term_type") or "primary").strip(),
                "language":    (r.get("language") or "en").strip(),
                "priority":    int(r.get("priority") or 5),
                "is_active":   int(r.get("is_active") or 1),
                "source":      (r.get("source") or "").strip() or None,
                "notes":       (r.get("notes")  or "").strip() or None,
            })
    return rows


def insert_terms(engine, rows: list[dict], replace: bool) -> None:
    sql = text("""
        INSERT IGNORE INTO commodity_search_terms
            (cmd_code, search_term, term_type, language,
             priority, is_active, source, notes)
        VALUES
            (:cmd_code, :search_term, :term_type, :language,
             :priority, :is_active, :source, :notes)
    """)
    with engine.begin() as conn:
        if replace:
            print("  --replace: truncating commodity_search_terms first")
            conn.execute(text("DELETE FROM commodity_search_terms"))
        # Chunk to keep statement size sane
        chunk = 500
        for i in range(0, len(rows), chunk):
            conn.execute(sql, rows[i:i + chunk])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="commodity_search_terms.csv",
                    help="Path to the search terms CSV "
                         "(default: ./commodity_search_terms.csv)")
    ap.add_argument("--replace", action="store_true",
                    help="DELETE existing rows before inserting (will "
                         "drop any manual MySQL-side additions)")
    args = ap.parse_args()

    path = Path(args.csv)
    if not path.exists():
        raise SystemExit(f"CSV not found: {path}")

    rows = load_csv(path)
    if not rows:
        raise SystemExit(f"No rows in {path}")
    print(f"  Loaded {len(rows)} rows from {path}")

    engine = make_engine()
    ensure_chapter_rows(engine, {r["cmd_code"] for r in rows})
    insert_terms(engine, rows, replace=args.replace)

    with engine.begin() as conn:
        total = conn.execute(
            text("SELECT COUNT(*) FROM commodity_search_terms")
        ).scalar()
        active = conn.execute(
            text("SELECT COUNT(*) FROM commodity_search_terms "
                 "WHERE is_active = 1")
        ).scalar()
        codes = conn.execute(
            text("SELECT COUNT(DISTINCT cmd_code) "
                 "FROM commodity_search_terms WHERE is_active = 1")
        ).scalar()

    print(f"  Done.  commodity_search_terms: {total} total, "
          f"{active} active across {codes} cmd_codes.")


if __name__ == "__main__":
    main()
