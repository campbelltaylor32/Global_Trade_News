"""
oltp_loader.py
==============
Fetches trade data from the UN Comtrade API and inserts raw records
into comtrade_oltp (MySQL 8.4).

Design notes
------------
1.  GRANULARITY   cmdCode='AG6' — all 6-digit HS subheadings in one call.
2.  PARTNERS      partnerCode=None — all individual partner countries.
3.  CHUNK         One API call per (period, reporter_code, flow_code).
4.  FALLBACK      If a chunk hits the 100k-row cap, retry per HS chapter
                  (cmdCode='01'..'97').
5.  DAILY BUDGET  Persisted in comtrade_oltp.fetch_budget; stops cleanly
                  at MAX_DAILY_CALLS and resumes the next day.
6.  RESUMABLE     comtrade_oltp.fetch_log + load_manifest track every
                  chunk; completed chunks are skipped on re-run.
7.  BACKOFF       Exponential retry on transient errors; quota errors
                  stop the day gracefully.
8.  ENRICHED      All API columns kept verbatim — no transformation here.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import date, datetime
from typing import Optional

import comtradeapicall
import mysql.connector
import pandas as pd
from mysql.connector import pooling

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------
SUBSCRIPTION_KEY   = os.environ["COMTRADE_SUBSCRIPTION_KEY"]
DB_HOST            = os.environ.get("OLTP_HOST", "127.0.0.1")
DB_PORT            = int(os.environ.get("OLTP_PORT", 3306))
DB_NAME            = os.environ.get("OLTP_DB", "comtrade_oltp")
DB_USER            = os.environ.get("OLTP_USER", "comtrade")
DB_PASSWORD        = os.environ.get("OLTP_PASSWORD")

TYPE_CODE          = "C"          # Commodities
FREQ_CODE          = "A"          # Annual
CL_CODE            = "HS"         # Harmonised System
CMD_CODE_ALL       = "AG6"        # All 6-digit HS subheadings
PARTNER_CODE       = None         # All partner countries
MAX_RECORDS        = 100_000      # Comtrade hard cap per call
MAX_DAILY_CALLS    = 490          # Leave a small buffer under the 500 limit
RETRY_ATTEMPTS     = 4
RETRY_BACKOFF_SEC  = 5
ROW_CAP_THRESHOLD  = 100_000      # If response == this, assume truncation

# HS chapters used for the per-chapter fallback (01–97, zero-padded)
HS_CHAPTERS = [f"{i:02d}" for i in range(1, 98)]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class DailyQuotaExceeded(Exception):
    pass

class NonRetryableClientError(Exception):
    pass


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_connection() -> mysql.connector.MySQLConnection:
    return mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        autocommit=False,
    )


# ---------------------------------------------------------------------------
# Budget helpers  (persisted in comtrade_oltp.fetch_budget)
# ---------------------------------------------------------------------------
def get_today_budget(conn) -> dict:
    today = date.today().isoformat()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT calls_made, calls_limit FROM fetch_budget WHERE budget_date = %s",
        (today,),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            """
            INSERT INTO fetch_budget (budget_date, calls_made, calls_limit)
            VALUES (%s, 0, %s)
            ON DUPLICATE KEY UPDATE budget_date = budget_date
            """,
            (today, MAX_DAILY_CALLS),
        )
        conn.commit()
        return {"calls_made": 0, "calls_limit": MAX_DAILY_CALLS}
    return row


def increment_budget(conn) -> int:
    """Increment today's call counter. Returns new calls_made."""
    today = date.today().isoformat()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE fetch_budget
        SET calls_made = calls_made + 1, last_updated = NOW()
        WHERE budget_date = %s
        """,
        (today,),
    )
    conn.commit()
    cur.execute(
        "SELECT calls_made FROM fetch_budget WHERE budget_date = %s", (today,)
    )
    return cur.fetchone()[0]


def remaining_budget(conn) -> int:
    b = get_today_budget(conn)
    return b["calls_limit"] - b["calls_made"]


# ---------------------------------------------------------------------------
# Manifest / fetch_log helpers
# ---------------------------------------------------------------------------
def chunk_already_done(conn, period: str, reporter_code: str,
                        flow_code: str, cmd_code: str) -> bool:
    """Return True if this exact chunk completed successfully before."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id FROM fetch_log
        WHERE period = %s
          AND reporter_code = %s
          AND flow_code = %s
          AND cmd_code = %s
          AND status = 'success'
        LIMIT 1
        """,
        (period, reporter_code, flow_code, cmd_code),
    )
    return cur.fetchone() is not None


def log_fetch_start(conn, period, reporter_code, flow_code,
                    cmd_code, batch_id) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO fetch_log
            (period, reporter_code, flow_code, cmd_code,
             status, attempts, started_at, batch_id)
        VALUES (%s, %s, %s, %s, 'pending', 0, NOW(), %s)
        """,
        (period, reporter_code, flow_code, cmd_code, batch_id),
    )
    conn.commit()
    return cur.lastrowid


def log_fetch_end(conn, log_id: int, status: str,
                  attempts: int, rows: int, error: Optional[str] = None):
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE fetch_log
        SET status = %s, attempts = %s, rows_returned = %s,
            error_message = %s, completed_at = NOW()
        WHERE id = %s
        """,
        (status, attempts, rows, error, log_id),
    )
    conn.commit()


def upsert_load_manifest(conn, batch_id: str, period: str,
                          status: str, raw_rows: int = 0):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO load_manifest (batch_id, period, status, raw_rows_available, created_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
            status = VALUES(status),
            raw_rows_available = raw_rows_available + VALUES(raw_rows_available)
        """,
        (batch_id, period, status, raw_rows),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Raw record insertion
# ---------------------------------------------------------------------------
_INSERT_SQL = """
    INSERT INTO raw_trade_records (
        period, reporter_code, partner_code, cmd_code, flow_code,
        mot_code, customs_code, type_code, freq_code, cl_code,
        primary_value, net_wgt, gross_wgt, qty_est,
        qty_unit_code, qty_unit_abbr,
        reporter_desc, partner_desc, cmd_desc, flow_desc, mot_desc,
        fetched_at, fetch_batch_id, is_processed, fetch_log_id
    ) VALUES (
        %(period)s, %(reporterCode)s, %(partnerCode)s, %(cmdCode)s, %(flowCode)s,
        %(motCode)s, %(customsCode)s, %(typeCode)s, %(freqCode)s, %(clCode)s,
        %(primaryValue)s, %(netWgt)s, %(grossWgt)s, %(qtyEstimation)s,
        %(qtyUnitCode)s, %(qtyUnitAbbr)s,
        %(reporterDesc)s, %(partnerDesc)s, %(cmdDesc)s, %(flowDesc)s, %(motDesc)s,
        NOW(), %(fetch_batch_id)s, 0, %(fetch_log_id)s
    )
"""

def insert_records(conn, df: pd.DataFrame, batch_id: str, log_id: int):
    """Bulk-insert a DataFrame of Comtrade rows into raw_trade_records."""
    if df.empty:
        return

    # Ensure all expected columns exist (fill missing with None)
    expected = [
        "period", "reporterCode", "partnerCode", "cmdCode", "flowCode",
        "motCode", "customsCode", "typeCode", "freqCode", "clCode",
        "primaryValue", "netWgt", "grossWgt", "qtyEstimation",
        "qtyUnitCode", "qtyUnitAbbr",
        "reporterDesc", "partnerDesc", "cmdDesc", "flowDesc", "motDesc",
    ]
    for col in expected:
        if col not in df.columns:
            df[col] = None

    df = df.where(pd.notnull(df), None)
    records = df[expected].to_dict("records")
    for r in records:
        r["fetch_batch_id"] = batch_id
        r["fetch_log_id"]   = log_id

    cur = conn.cursor()
    cur.executemany(_INSERT_SQL, records)
    conn.commit()
    logger.info("  Inserted %d rows (batch=%s)", len(records), batch_id)


# ---------------------------------------------------------------------------
# API call helpers
# ---------------------------------------------------------------------------
def _looks_like_quota_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("429", "quota", "rate limit", "too many"))

def _looks_like_url_too_long(exc: BaseException) -> bool:
    return "414" in str(exc) or "url too long" in str(exc).lower()


def fetch_once(*, period, reporter_code, flow_code, cmd_code) -> pd.DataFrame:
    df = comtradeapicall.getFinalData(
        SUBSCRIPTION_KEY,
        typeCode=TYPE_CODE,
        freqCode=FREQ_CODE,
        clCode=CL_CODE,
        period=str(period),
        reporterCode=str(reporter_code),
        cmdCode=cmd_code,
        flowCode=str(flow_code),
        partnerCode=PARTNER_CODE,
        partner2Code=None,
        customsCode=None,
        motCode=None,
        maxRecords=MAX_RECORDS,
        format_output="JSON",
        aggregateBy=None,
        breakdownMode="classic",
        countOnly=None,
        includeDesc=True,
    )
    if df is None:
        raise RuntimeError("comtradeapicall returned None (transport or auth error).")
    return df


def fetch_with_retries(*, period, reporter_code, flow_code,
                        cmd_code, conn) -> tuple[pd.DataFrame, int]:
    """Fetch one chunk with exponential backoff. Returns (df, n_attempts)."""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        if remaining_budget(conn) <= 0:
            raise DailyQuotaExceeded("Local daily budget exhausted.")
        try:
            increment_budget(conn)
            df = fetch_once(
                period=period,
                reporter_code=reporter_code,
                flow_code=flow_code,
                cmd_code=cmd_code,
            )
            return df, attempt
        except Exception as exc:
            last_exc = exc
            if _looks_like_quota_error(exc):
                raise DailyQuotaExceeded(repr(exc)) from exc
            if _looks_like_url_too_long(exc):
                raise NonRetryableClientError(repr(exc)) from exc
            if attempt >= RETRY_ATTEMPTS:
                break
            wait = RETRY_BACKOFF_SEC * attempt
            logger.warning(
                "Attempt %d/%d failed period=%s reporter=%s flow=%s cmd=%s: %r"
                " — retrying in %.1fs",
                attempt, RETRY_ATTEMPTS, period, reporter_code,
                flow_code, cmd_code, exc, wait,
            )
            time.sleep(wait)
    raise RuntimeError(
        f"Failed after {RETRY_ATTEMPTS} attempts "
        f"period={period} reporter={reporter_code} "
        f"flow={flow_code} cmd={cmd_code}: {last_exc!r}"
    )


# ---------------------------------------------------------------------------
# Chunk processing
# ---------------------------------------------------------------------------
def process_chunk(*, period: str, reporter_code: str, flow_code: str,
                   conn, batch_id: str):
    """
    Fetch one (period, reporter, flow) chunk.

    Uses AG6 (all 6-digit HS codes in one call).  If the response hits
    the 100k cap, falls back to per-HS-chapter calls.
    """
    if chunk_already_done(conn, period, reporter_code, flow_code, CMD_CODE_ALL):
        logger.info("SKIP  period=%s reporter=%s flow=%s (already loaded)",
                    period, reporter_code, flow_code)
        return

    log_id = log_fetch_start(conn, period, reporter_code,
                              flow_code, CMD_CODE_ALL, batch_id)
    try:
        df, attempts = fetch_with_retries(
            period=period,
            reporter_code=reporter_code,
            flow_code=flow_code,
            cmd_code=CMD_CODE_ALL,
            conn=conn,
        )

        # --- Truncation fallback ---
        if len(df) >= ROW_CAP_THRESHOLD:
            logger.warning(
                "Row cap hit for period=%s reporter=%s flow=%s — "
                "falling back to per-chapter calls.",
                period, reporter_code, flow_code,
            )
            log_fetch_end(conn, log_id, "failed", attempts, len(df),
                          "row cap hit — falling back to chapters")
            _process_by_chapter(
                period=period,
                reporter_code=reporter_code,
                flow_code=flow_code,
                conn=conn,
                batch_id=batch_id,
            )
            return

        insert_records(conn, df, batch_id, log_id)
        log_fetch_end(conn, log_id, "success", attempts, len(df))
        upsert_load_manifest(conn, batch_id, period, "pending", len(df))
        logger.info("OK    period=%s reporter=%s flow=%s  rows=%d",
                    period, reporter_code, flow_code, len(df))

    except DailyQuotaExceeded:
        log_fetch_end(conn, log_id, "quota_exceeded", 0, 0)
        raise
    except Exception as exc:
        log_fetch_end(conn, log_id, "failed", RETRY_ATTEMPTS, 0, str(exc))
        logger.error("FAIL  period=%s reporter=%s flow=%s: %s",
                     period, reporter_code, flow_code, exc)


def _process_by_chapter(*, period: str, reporter_code: str,
                          flow_code: str, conn, batch_id: str):
    """Fallback: one call per HS chapter (cmdCode='01'..'97')."""
    all_rows = 0
    for chapter in HS_CHAPTERS:
        if remaining_budget(conn) <= 0:
            raise DailyQuotaExceeded("Budget exhausted during chapter fallback.")

        if chunk_already_done(conn, period, reporter_code, flow_code, chapter):
            logger.info("  SKIP chapter=%s (already loaded)", chapter)
            continue

        log_id = log_fetch_start(conn, period, reporter_code,
                                  flow_code, chapter, batch_id)
        try:
            df, attempts = fetch_with_retries(
                period=period,
                reporter_code=reporter_code,
                flow_code=flow_code,
                cmd_code=chapter,
                conn=conn,
            )
            insert_records(conn, df, batch_id, log_id)
            log_fetch_end(conn, log_id, "success", attempts, len(df))
            all_rows += len(df)
            logger.info("  chapter=%s rows=%d", chapter, len(df))
        except DailyQuotaExceeded:
            log_fetch_end(conn, log_id, "quota_exceeded", 0, 0)
            raise
        except Exception as exc:
            log_fetch_end(conn, log_id, "failed", RETRY_ATTEMPTS, 0, str(exc))
            logger.error("  FAIL chapter=%s: %s", chapter, exc)

    upsert_load_manifest(conn, batch_id, period, "pending", all_rows)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run(periods: list[str], reporter_codes: list[str], flow_codes: list[str]):
    """
    Main loop. Iterates all (period, reporter, flow) combinations.

    Args:
        periods:        e.g. ['2022', '2023']
        reporter_codes: e.g. ['840', '156', '276']   (ISO numeric)
        flow_codes:     e.g. ['M', 'X']               (Import / Export)
    """
    batch_id = f"batch_{date.today().isoformat()}_{uuid.uuid4().hex[:8]}"
    logger.info("Starting batch %s", batch_id)

    conn = get_connection()
    try:
        for period in periods:
            for reporter_code in reporter_codes:
                for flow_code in flow_codes:
                    if remaining_budget(conn) <= 0:
                        logger.warning("Daily budget exhausted — stopping.")
                        return
                    try:
                        process_chunk(
                            period=period,
                            reporter_code=reporter_code,
                            flow_code=flow_code,
                            conn=conn,
                            batch_id=batch_id,
                        )
                    except DailyQuotaExceeded:
                        logger.warning("Quota exceeded — stopping for today.")
                        return
    finally:
        conn.close()

    logger.info("Batch %s complete.", batch_id)


if __name__ == "__main__":
    # Example: pull 2023 imports + exports for USA, China, Germany
    run(
        periods=["2023"],
        reporter_codes=["842", "156", "276"],
        flow_codes=["M", "X"],
    )
