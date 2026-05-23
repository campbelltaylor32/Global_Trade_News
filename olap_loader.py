"""
olap_loader.py
==============
Reads unprocessed raw records from comtrade_oltp (MySQL 8.4) and
loads them into the OLAP model.

Pipeline
--------
1.  Claim a pending batch from load_manifest (status → 'loading').
2.  Pull all raw_trade_records for that batch (is_processed = 0).
3.  Upsert all dimension tables from the denormalized desc columns.
4.  Insert new fact rows into fact_trade_granular.
5.  Mark raw rows is_processed = 1.
6.  Update load_manifest status → 'loaded'.
7.  Repeat until no pending batches remain.

Assumptions
-----------
- OLAP is also MySQL 8.4 on GCP (adjust get_olap_connection() if not).
- OLAP dimension tables already exist (created by your existing schema).
- This loader is idempotent: re-running a loaded batch is safe because
  upserts are used for dimensions and fact inserts check for duplicates.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

import mysql.connector
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# OLTP connection
OLTP_HOST     = os.environ.get("OLTP_HOST", "127.0.0.1")
OLTP_PORT     = int(os.environ.get("OLTP_PORT", 3306))
OLTP_DB       = os.environ.get("OLTP_DB", "comtrade_oltp")
OLTP_USER     = os.environ.get("OLTP_USER", "comtrade")
OLTP_PASSWORD = os.environ.get("OLTP_PASSWORD")

# OLAP connection
OLAP_HOST     = os.environ.get("OLAP_HOST", "127.0.0.1")
OLAP_PORT     = int(os.environ.get("OLAP_PORT", 3306))
OLAP_DB       = os.environ.get("OLAP_DB", "comtrade_olap")
OLAP_USER     = os.environ.get("OLAP_USER", "comtrade")
OLAP_PASSWORD = os.environ.get("OLAP_PASSWORD")

BATCH_SIZE    = 5_000   # rows to process per commit cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------
def get_oltp_connection() -> mysql.connector.MySQLConnection:
    return mysql.connector.connect(
        host=OLTP_HOST, port=OLTP_PORT, database=OLTP_DB,
        user=OLTP_USER, password=OLTP_PASSWORD, autocommit=False,
    )

def get_olap_connection() -> mysql.connector.MySQLConnection:
    return mysql.connector.connect(
        host=OLAP_HOST, port=OLAP_PORT, database=OLAP_DB,
        user=OLAP_USER, password=OLAP_PASSWORD, autocommit=False,
    )


# ---------------------------------------------------------------------------
# Batch claim helpers
# ---------------------------------------------------------------------------
def claim_next_batch(oltp_conn) -> Optional[dict]:
    """
    Atomically claim one pending batch from load_manifest.
    Returns the manifest row dict or None if nothing is pending.
    """
    cur = oltp_conn.cursor(dictionary=True)
    # Lock the row so concurrent loaders don't double-claim
    cur.execute(
        """
        SELECT id, batch_id, period
        FROM load_manifest
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT 1
        FOR UPDATE
        """
    )
    row = cur.fetchone()
    if row is None:
        oltp_conn.rollback()
        return None

    cur.execute(
        "UPDATE load_manifest SET status = 'loading', started_at = NOW() WHERE id = %s",
        (row["id"],),
    )
    oltp_conn.commit()
    return row


def mark_manifest_loaded(oltp_conn, manifest_id: int, rows_loaded: int):
    cur = oltp_conn.cursor()
    cur.execute(
        """
        UPDATE load_manifest
        SET status = 'loaded', rows_loaded = %s, completed_at = NOW()
        WHERE id = %s
        """,
        (rows_loaded, manifest_id),
    )
    oltp_conn.commit()


def mark_manifest_failed(oltp_conn, manifest_id: int, error: str):
    cur = oltp_conn.cursor()
    cur.execute(
        """
        UPDATE load_manifest
        SET status = 'failed', error_message = %s, completed_at = NOW()
        WHERE id = %s
        """,
        (error, manifest_id),
    )
    oltp_conn.commit()


# ---------------------------------------------------------------------------
# Fetch raw records from OLTP
# ---------------------------------------------------------------------------
def fetch_raw_batch(oltp_conn, batch_id: str) -> pd.DataFrame:
    cur = oltp_conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT *
        FROM raw_trade_records
        WHERE fetch_batch_id = %s AND is_processed = 0
        """,
        (batch_id,),
    )
    rows = cur.fetchall()
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def mark_raw_processed(oltp_conn, ids: list[int]):
    if not ids:
        return
    fmt = ",".join(["%s"] * len(ids))
    cur = oltp_conn.cursor()
    cur.execute(
        f"UPDATE raw_trade_records SET is_processed = 1 WHERE id IN ({fmt})",
        ids,
    )
    oltp_conn.commit()


# ---------------------------------------------------------------------------
# Dimension upserts  (OLAP)
# Each function upserts from the denormalized raw columns into the
# corresponding OLAP dimension table.  Adjust column names to match
# your actual OLAP schema.
# ---------------------------------------------------------------------------

def upsert_country_mapping(olap_conn, df: pd.DataFrame):
    """Upsert reporters and partners into country_mapping."""
    cur = olap_conn.cursor()
    sql = """
        INSERT INTO country_mapping (country_code, country_desc)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE country_desc = VALUES(country_desc)
    """
    # Reporters
    reporters = (
        df[["reporter_code", "reporter_desc"]]
        .dropna(subset=["reporter_code"])
        .drop_duplicates()
        .values.tolist()
    )
    # Partners
    partners = (
        df[["partner_code", "partner_desc"]]
        .dropna(subset=["partner_code"])
        .drop_duplicates()
        .values.tolist()
    )
    cur.executemany(sql, reporters + partners)
    olap_conn.commit()


def upsert_commodity_code_mapping(olap_conn, df: pd.DataFrame):
    cur = olap_conn.cursor()
    sql = """
        INSERT INTO commodity_code_mapping (cmd_code, cmd_desc)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE cmd_desc = VALUES(cmd_desc)
    """
    records = (
        df[["cmd_code", "cmd_desc"]]
        .dropna(subset=["cmd_code"])
        .drop_duplicates()
        .values.tolist()
    )
    cur.executemany(sql, records)
    olap_conn.commit()


def upsert_tradeflow_mapping(olap_conn, df: pd.DataFrame):
    cur = olap_conn.cursor()
    sql = """
        INSERT INTO tradeflow_mapping (flow_code, flow_desc)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE flow_desc = VALUES(flow_desc)
    """
    records = (
        df[["flow_code", "flow_desc"]]
        .dropna(subset=["flow_code"])
        .drop_duplicates()
        .values.tolist()
    )
    cur.executemany(sql, records)
    olap_conn.commit()


def upsert_transport_mapping(olap_conn, df: pd.DataFrame):
    if "mot_code" not in df.columns:
        return
    cur = olap_conn.cursor()
    sql = """
        INSERT INTO transport_mapping (mot_code, mot_desc)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE mot_desc = VALUES(mot_desc)
    """
    records = (
        df[["mot_code", "mot_desc"]]
        .dropna(subset=["mot_code"])
        .drop_duplicates()
        .values.tolist()
    )
    cur.executemany(sql, records)
    olap_conn.commit()


def upsert_frequency_mapping(olap_conn, df: pd.DataFrame):
    if "freq_code" not in df.columns:
        return
    cur = olap_conn.cursor()
    sql = """
        INSERT INTO frequency_mapping (freq_code)
        VALUES (%s)
        ON DUPLICATE KEY UPDATE freq_code = VALUES(freq_code)
    """
    records = df[["freq_code"]].dropna().drop_duplicates().values.tolist()
    cur.executemany(sql, records)
    olap_conn.commit()


def upsert_unit_quantity_mapping(olap_conn, df: pd.DataFrame):
    if "qty_unit_code" not in df.columns:
        return
    cur = olap_conn.cursor()
    sql = """
        INSERT INTO unit_quantity_mapping (qty_unit_code, qty_unit_abbr)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE qty_unit_abbr = VALUES(qty_unit_abbr)
    """
    records = (
        df[["qty_unit_code", "qty_unit_abbr"]]
        .dropna(subset=["qty_unit_code"])
        .drop_duplicates()
        .values.tolist()
    )
    cur.executemany(sql, records)
    olap_conn.commit()


# ---------------------------------------------------------------------------
# Fact insert  (OLAP)
# ---------------------------------------------------------------------------
_FACT_INSERT_SQL = """
    INSERT IGNORE INTO fact_trade_granular (
        period, reporter_code, partner_code, cmd_code, flow_code,
        mot_code, customs_code, type_code, freq_code, cl_code,
        primary_value, net_wgt, gross_wgt, qty_est,
        qty_unit_code, fetch_batch_id
    ) VALUES (
        %(period)s, %(reporter_code)s, %(partner_code)s, %(cmd_code)s, %(flow_code)s,
        %(mot_code)s, %(customs_code)s, %(type_code)s, %(freq_code)s, %(cl_code)s,
        %(primary_value)s, %(net_wgt)s, %(gross_wgt)s, %(qty_est)s,
        %(qty_unit_code)s, %(fetch_batch_id)s
    )
"""

def insert_facts(olap_conn, df: pd.DataFrame):
    """Insert fact rows. INSERT IGNORE skips exact duplicates."""
    fact_cols = [
        "period", "reporter_code", "partner_code", "cmd_code", "flow_code",
        "mot_code", "customs_code", "type_code", "freq_code", "cl_code",
        "primary_value", "net_wgt", "gross_wgt", "qty_est",
        "qty_unit_code", "fetch_batch_id",
    ]
    for col in fact_cols:
        if col not in df.columns:
            df[col] = None

    df = df.where(pd.notnull(df), None)
    records = df[fact_cols].to_dict("records")

    cur = olap_conn.cursor()
    for i in range(0, len(records), BATCH_SIZE):
        chunk = records[i : i + BATCH_SIZE]
        cur.executemany(_FACT_INSERT_SQL, chunk)
        olap_conn.commit()
        logger.info("  Facts inserted: %d / %d", min(i + BATCH_SIZE, len(records)), len(records))


# ---------------------------------------------------------------------------
# Process one batch end-to-end
# ---------------------------------------------------------------------------
def process_batch(manifest_row: dict, oltp_conn, olap_conn):
    batch_id    = manifest_row["batch_id"]
    manifest_id = manifest_row["id"]

    logger.info("Processing batch %s (period=%s)", batch_id, manifest_row.get("period"))

    try:
        df = fetch_raw_batch(oltp_conn, batch_id)
        if df.empty:
            logger.warning("Batch %s has no unprocessed rows — skipping.", batch_id)
            mark_manifest_loaded(oltp_conn, manifest_id, 0)
            return

        logger.info("  Raw rows fetched: %d", len(df))

        # 1. Upsert all dimensions
        upsert_country_mapping(olap_conn, df)
        upsert_commodity_code_mapping(olap_conn, df)
        upsert_tradeflow_mapping(olap_conn, df)
        upsert_transport_mapping(olap_conn, df)
        upsert_frequency_mapping(olap_conn, df)
        upsert_unit_quantity_mapping(olap_conn, df)
        logger.info("  Dimensions upserted.")

        # 2. Insert facts
        insert_facts(olap_conn, df)
        logger.info("  Facts loaded.")

        # 3. Mark raw rows processed
        raw_ids = df["id"].tolist()
        mark_raw_processed(oltp_conn, raw_ids)
        logger.info("  Marked %d raw rows as processed.", len(raw_ids))

        # 4. Update manifest
        mark_manifest_loaded(oltp_conn, manifest_id, len(df))
        logger.info("Batch %s complete — %d rows loaded.", batch_id, len(df))

    except Exception as exc:
        logger.error("Batch %s failed: %s", batch_id, exc)
        mark_manifest_failed(oltp_conn, manifest_id, str(exc))
        raise


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run():
    """Process all pending batches in load_manifest."""
    oltp_conn = get_oltp_connection()
    olap_conn = get_olap_connection()

    try:
        processed = 0
        while True:
            manifest_row = claim_next_batch(oltp_conn)
            if manifest_row is None:
                logger.info("No pending batches — done. Total batches processed: %d", processed)
                break
            process_batch(manifest_row, oltp_conn, olap_conn)
            processed += 1
    finally:
        oltp_conn.close()
        olap_conn.close()


if __name__ == "__main__":
    run()
