"""
Load country latitude / longitude lookup data into the same MySQL DB
used by the UN Comtrade loader.

Creates:
  1. country_geo
      - one row per ISO alpha-3 country code
      - latitude / longitude columns
      - keyed on iso_alpha_3

  2. fact_trade_granular_v2_with_geo view
      - joins your main fact table to country_geo twice:
          reporter_iso -> reporter latitude/longitude
          partner_iso  -> partner latitude/longitude

Usage:
  python load_country_geo.py --csv country-longitude-latitude.csv

.env expected:
  DB_USER=root
  DB_PASS=your_password
  DB_HOST=your_host
  DB_PORT=3306
  DB_NAME=your_database

Optional:
  FACT_TABLE=fact_trade_granular_v2
  GEO_TABLE=country_geo
  CREATE_GEO_VIEW=true
"""

from __future__ import annotations

import argparse
import logging
import math
import os
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text, MetaData, Table
from sqlalchemy.dialects.mysql import insert as mysql_insert


# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

load_dotenv()

DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME")

FACT_TABLE = os.getenv("FACT_TABLE", "fact_trade_granular_v2")
GEO_TABLE = os.getenv("GEO_TABLE", "country_geo")
CREATE_GEO_VIEW = os.getenv("CREATE_GEO_VIEW", "true").lower() == "true"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# ---------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger("country_geo_loader")


# ---------------------------------------------------------------------
# DB CONNECTION
# ---------------------------------------------------------------------

def get_engine():
    missing = [
        k for k, v in {
            "DB_USER": DB_USER,
            "DB_PASS": DB_PASS,
            "DB_HOST": DB_HOST,
            "DB_NAME": DB_NAME,
        }.items()
        if not v
    ]

    if missing:
        raise ValueError(f"Missing required .env variables: {missing}")

    user = quote_plus(DB_USER)
    pw = quote_plus(DB_PASS)

    return create_engine(
        f"mysql+mysqlconnector://{user}:{pw}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=1,
        max_overflow=0,
        connect_args={
            "connection_timeout": 60,
            "autocommit": False,
        },
    )


# ---------------------------------------------------------------------
# TABLE / VIEW CREATION
# ---------------------------------------------------------------------

def create_country_geo_table(engine) -> None:
    sql = f"""
    CREATE TABLE IF NOT EXISTS {GEO_TABLE} (
        iso_alpha_3 VARCHAR(10) NOT NULL,
        iso_alpha_2 VARCHAR(10) NULL,
        country_name VARCHAR(255) NULL,
        iso_name VARCHAR(255) NULL,
        latitude DECIMAL(10, 6) NULL,
        longitude DECIMAL(10, 6) NULL,
        wikidata_id VARCHAR(50) NULL,
        wikidata_latitude DECIMAL(10, 6) NULL,
        wikidata_longitude DECIMAL(10, 6) NULL,
        wikidata_label VARCHAR(255) NULL,
        historical TINYINT NULL,
        iso_name_flag TINYINT NULL,
        source_file VARCHAR(255) NULL,
        loaded_at_utc TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

        PRIMARY KEY (iso_alpha_3),
        INDEX idx_country_geo_iso_alpha_2 (iso_alpha_2),
        INDEX idx_country_geo_country_name (country_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """

    with engine.begin() as conn:
        conn.execute(text(sql))

    logger.info("Ensured table exists: %s", GEO_TABLE)


def create_geo_view(engine) -> None:
    view_name = f"{FACT_TABLE}_with_geo"

    sql = f"""
    CREATE OR REPLACE VIEW {view_name} AS
    SELECT
        f.*,

        rg.country_name AS reporter_geo_country_name,
        rg.latitude AS reporter_latitude,
        rg.longitude AS reporter_longitude,

        pg.country_name AS partner_geo_country_name,
        pg.latitude AS partner_latitude,
        pg.longitude AS partner_longitude

    FROM {FACT_TABLE} f
    LEFT JOIN {GEO_TABLE} rg
        ON f.reporter_iso = rg.iso_alpha_3
    LEFT JOIN {GEO_TABLE} pg
        ON f.partner_iso = pg.iso_alpha_3;
    """

    with engine.begin() as conn:
        conn.execute(text(sql))

    logger.info("Created/updated view: %s", view_name)


# ---------------------------------------------------------------------
# DATA CLEANING
# ---------------------------------------------------------------------

def clean_country_geo(csv_path: Path) -> pd.DataFrame:
    """
    Reads country-longitude-latitude.csv and normalizes it to one row
    per ISO alpha-3 code.

    Source columns expected:
      Country, ISO-ALPHA-3, ISO-ALPHA-2, Latitude, Longitude,
      ISO-Name, Historical, WikiData_ID, WikiData_Latitude,
      WikiData_Longitude, WikiData_Label
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig")

    rename_map = {
        "Country": "country_name",
        "ISO-ALPHA-3": "iso_alpha_3",
        "ISO-ALPHA-2": "iso_alpha_2",
        "Latitude": "latitude",
        "Longitude": "longitude",
        "ISO-Name": "iso_name_flag",
        "Historical": "historical",
        "WikiData_ID": "wikidata_id",
        "WikiData_Latitude": "wikidata_latitude",
        "WikiData_Longitude": "wikidata_longitude",
        "WikiData_Label": "wikidata_label",
    }

    missing_cols = [c for c in rename_map if c not in df.columns]
    if missing_cols:
        raise ValueError(
            f"Input CSV is missing expected columns: {missing_cols}. "
            f"Available columns: {list(df.columns)}"
        )

    df = df.rename(columns=rename_map)

    keep_cols = [
        "iso_alpha_3",
        "iso_alpha_2",
        "country_name",
        "latitude",
        "longitude",
        "wikidata_id",
        "wikidata_latitude",
        "wikidata_longitude",
        "wikidata_label",
        "historical",
        "iso_name_flag",
    ]

    out = df[keep_cols].copy()

    # Strip object columns.
    string_cols = [
        "iso_alpha_3",
        "iso_alpha_2",
        "country_name",
        "wikidata_id",
        "wikidata_label",
    ]

    for col in string_cols:
        out[col] = out[col].astype(str).str.strip()
        out[col] = out[col].replace(
            {
                "": None,
                "nan": None,
                "NaN": None,
                "None": None,
                "<NA>": None,
            }
        )

    # Uppercase ISO codes.
    out["iso_alpha_3"] = out["iso_alpha_3"].astype(str).str.upper()
    out["iso_alpha_2"] = out["iso_alpha_2"].astype(str).str.upper()

    out["iso_alpha_3"] = out["iso_alpha_3"].replace(
        {"": None, "NAN": None, "NONE": None, "<NA>": None}
    )
    out["iso_alpha_2"] = out["iso_alpha_2"].replace(
        {"": None, "NAN": None, "NONE": None, "<NA>": None}
    )

    # Numeric fields.
    for col in [
        "latitude",
        "longitude",
        "wikidata_latitude",
        "wikidata_longitude",
    ]:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    # Integer flag fields.
    for col in ["historical", "iso_name_flag"]:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)

    # Drop rows without ISO alpha-3 because these cannot map to reporter_iso / partner_iso.
    before = len(out)
    out = out[out["iso_alpha_3"].notna()]
    out = out[out["iso_alpha_3"].astype(str).str.len() > 0]
    logger.info("Dropped %d rows without ISO alpha-3", before - len(out))

    # Prefer rows with usable lat/lon.
    out["has_lat_lon"] = out["latitude"].notna() & out["longitude"].notna()

    # Deduplicate by alpha-3.
    # Prefer:
    #   historical = 0
    #   iso_name_flag = 1
    #   has_lat_lon = True
    out = out.sort_values(
        by=["iso_alpha_3", "historical", "iso_name_flag", "has_lat_lon"],
        ascending=[True, True, False, False],
    )

    out = out.drop_duplicates(subset=["iso_alpha_3"], keep="first")

    # Add special Comtrade / UN aggregate code for World if your fact table uses W00.
    if "W00" not in set(out["iso_alpha_3"].astype(str)):
        world_row = {
            "iso_alpha_3": "W00",
            "iso_alpha_2": None,
            "country_name": "World",
            "latitude": None,
            "longitude": None,
            "wikidata_id": None,
            "wikidata_latitude": None,
            "wikidata_longitude": None,
            "wikidata_label": "World",
            "historical": 0,
            "iso_name_flag": 1,
            "has_lat_lon": False,
        }

        out = pd.concat(
            [out, pd.DataFrame([world_row])],
            ignore_index=True,
        )

    out["source_file"] = csv_path.name

    # The DB has both country_name and iso_name.
    # Use country_name as display name for both.
    out["iso_name"] = out["country_name"]

    final_cols = [
        "iso_alpha_3",
        "iso_alpha_2",
        "country_name",
        "iso_name",
        "latitude",
        "longitude",
        "wikidata_id",
        "wikidata_latitude",
        "wikidata_longitude",
        "wikidata_label",
        "historical",
        "iso_name_flag",
        "source_file",
    ]

    out = out[final_cols].copy()

    logger.info("Prepared %d unique ISO alpha-3 geo rows", len(out))

    return out


# ---------------------------------------------------------------------
# MYSQL VALUE CLEANUP
# ---------------------------------------------------------------------

def clean_value_for_mysql(value):
    """
    Convert pandas/numpy missing values into None so MySQL receives NULL,
    not bare nan.
    """
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if isinstance(value, float) and math.isnan(value):
        return None

    return value


def dataframe_to_mysql_records(df: pd.DataFrame) -> list[dict]:
    """
    Convert dataframe to list of dicts with all NaN / NaT / pd.NA converted to None.
    """
    clean_df = df.copy()

    # Force object type so None is preserved, not converted back to NaN.
    clean_df = clean_df.astype(object)

    records = []
    for row in clean_df.to_dict(orient="records"):
        records.append(
            {key: clean_value_for_mysql(value) for key, value in row.items()}
        )

    return records


# ---------------------------------------------------------------------
# LOAD DATA
# ---------------------------------------------------------------------

def load_country_geo(engine, df: pd.DataFrame) -> None:
    """
    Upsert rows into country_geo.

    If an iso_alpha_3 already exists, update its metadata / lat / lon.
    Converts NaN values to None so MySQL gets NULL instead of nan.
    """
    if df.empty:
        logger.warning("No geo rows to load.")
        return

    records = dataframe_to_mysql_records(df)

    metadata = MetaData()
    geo_table = Table(GEO_TABLE, metadata, autoload_with=engine)

    inserted_or_updated = 0
    chunk_size = 25

    with engine.begin() as conn:
        for start in range(0, len(records), chunk_size):
            chunk = records[start:start + chunk_size]

            stmt = mysql_insert(geo_table).values(chunk)

            update_cols = {
                "iso_alpha_2": stmt.inserted.iso_alpha_2,
                "country_name": stmt.inserted.country_name,
                "iso_name": stmt.inserted.iso_name,
                "latitude": stmt.inserted.latitude,
                "longitude": stmt.inserted.longitude,
                "wikidata_id": stmt.inserted.wikidata_id,
                "wikidata_latitude": stmt.inserted.wikidata_latitude,
                "wikidata_longitude": stmt.inserted.wikidata_longitude,
                "wikidata_label": stmt.inserted.wikidata_label,
                "historical": stmt.inserted.historical,
                "iso_name_flag": stmt.inserted.iso_name_flag,
                "source_file": stmt.inserted.source_file,
            }

            stmt = stmt.on_duplicate_key_update(**update_cols)

            result = conn.execute(stmt)
            inserted_or_updated += result.rowcount or 0

    logger.info(
        "Loaded geo table %s. Input rows=%d, affected rows=%d",
        GEO_TABLE,
        len(df),
        inserted_or_updated,
    )


# ---------------------------------------------------------------------
# QA CHECKS
# ---------------------------------------------------------------------

def run_qa_checks(engine) -> None:
    with engine.begin() as conn:
        geo_count = conn.execute(
            text(f"SELECT COUNT(*) FROM {GEO_TABLE}")
        ).scalar()

        logger.info("%s row count: %s", GEO_TABLE, geo_count)

        fact_exists = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = :fact_table
                """
            ),
            {"fact_table": FACT_TABLE},
        ).scalar()

        if not fact_exists:
            logger.warning(
                "Fact table %s does not exist in this DB, skipping fact join QA.",
                FACT_TABLE,
            )
            return

        missing_reporters = conn.execute(text(f"""
            SELECT f.reporter_iso, COUNT(*) AS n_rows
            FROM {FACT_TABLE} f
            LEFT JOIN {GEO_TABLE} g
                ON f.reporter_iso = g.iso_alpha_3
            WHERE f.reporter_iso IS NOT NULL
              AND g.iso_alpha_3 IS NULL
            GROUP BY f.reporter_iso
            ORDER BY n_rows DESC
            LIMIT 25
        """)).fetchall()

        missing_partners = conn.execute(text(f"""
            SELECT f.partner_iso, COUNT(*) AS n_rows
            FROM {FACT_TABLE} f
            LEFT JOIN {GEO_TABLE} g
                ON f.partner_iso = g.iso_alpha_3
            WHERE f.partner_iso IS NOT NULL
              AND g.iso_alpha_3 IS NULL
            GROUP BY f.partner_iso
            ORDER BY n_rows DESC
            LIMIT 25
        """)).fetchall()

    if missing_reporters:
        logger.warning("Reporter ISO codes missing from %s:", GEO_TABLE)
        for iso, n in missing_reporters:
            logger.warning("  reporter_iso=%s rows=%s", iso, n)
    else:
        logger.info("All reporter_iso values map to %s", GEO_TABLE)

    if missing_partners:
        logger.warning("Partner ISO codes missing from %s:", GEO_TABLE)
        for iso, n in missing_partners:
            logger.warning("  partner_iso=%s rows=%s", iso, n)
    else:
        logger.info("All partner_iso values map to %s", GEO_TABLE)


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to country latitude/longitude CSV.",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)

    engine = get_engine()

    logger.info("Loading country geo data from %s", csv_path)
    logger.info("Target DB table: %s", GEO_TABLE)
    logger.info("Fact table for QA/view: %s", FACT_TABLE)

    create_country_geo_table(engine)

    geo_df = clean_country_geo(csv_path)

    # Debug check: make sure no raw NaN survives before insert.
    records = dataframe_to_mysql_records(geo_df)
    bad_values = []
    for i, row in enumerate(records):
        for key, value in row.items():
            if isinstance(value, float) and math.isnan(value):
                bad_values.append((i, key))

    if bad_values:
        raise ValueError(f"NaN values still present before MySQL insert: {bad_values[:10]}")

    load_country_geo(engine, geo_df)

    if CREATE_GEO_VIEW:
        create_geo_view(engine)

    run_qa_checks(engine)

    logger.info("Country geo load complete.")


if __name__ == "__main__":
    main()