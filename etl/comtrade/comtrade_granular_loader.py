"""
UN Comtrade granular historical loader
======================================

Pulls non-aggregated, commodity-level (HS 6-digit) trade data from the
UN Comtrade Plus API and appends results into a single master CSV.

Key design choices vs. the previous loader
------------------------------------------

1.  GRANULARITY.  cmdCode='AG6' tells Comtrade to return every 6-digit
    HS subheading row the reporter declared (rather than one aggregated
    'TOTAL' row).  The project's Commodity_Code_Mapping has ~6,897
    leaf codes at aggrLevel=6, so AG6 yields the most detailed
    commodity slice available in the HS classification.  One call with
    AG6 is dramatically cheaper than 6,900 calls iterating individual
    codes.

2.  PARTNERS.  partnerCode=None requests ALL individual partner
    countries (not just the World=0 aggregate).

3.  CHUNK = ONE API CALL.  A chunk is (period, reporter, flow).  With
    AG6 + all partners, one chunk typically returns tens of thousands
    of rows for most reporters.  When the response equals the
    100,000-row cap (= probably truncated), the loader falls back to
    a per-HS-chapter subdivision (cmdCode='01', '02', ...).

4.  ONE MASTER CSV.  Every chunk's rows are appended to a single
    output file.  No per-chunk CSVs.  Each appended row carries a
    chunk_id for traceability and post-hoc dedup.

5.  DAILY BUDGET.  The free Comtrade subscription is capped at ~500
    requests/day.  The loader records every call in budget.json and
    stops cleanly at MAX_DAILY_CALLS.  Re-running the next day picks
    up exactly where it left off via the manifest.

6.  RESUMABLE.  manifest.csv records each chunk's status.  Completed
    chunks are skipped.  The manifest key includes
    classification + frequency + cmd_code, so switching granularity
    (e.g., TOTAL -> AG6) does NOT falsely mark old chunks "done".

7.  RATE-LIMIT-SAFE.  Exponential backoff on transient errors;
    detects HTTP 429 / quota errors and stops the day gracefully.

8.  ENRICHED COLUMNS.  Keeps cifValue, fobValue, primaryValue,
    isAggregate, isReported, ISO codes, partner2, mode-of-transport,
    customs procedure, and all estimation flags.  See the
    ComtradePlus DataItems reference for definitions.


Install
-------
    python -m pip install comtradeapicall pandas python-dotenv \
                         sqlalchemy mysql-connector-python


.env example
------------
    COMTRADE_SUBSCRIPTION_KEY=your_key_here

    # Year/month window.  YYYYMM if FREQ_CODE=M, YYYY if FREQ_CODE=A
    FREQ_CODE=A                  # 'A' annual, 'M' monthly
    START_PERIOD=2019
    END_PERIOD=2024

    CL_CODE=HS                   # HS classification (current edition)
    CMD_CODE=AG6                 # AG6 = all 6-digit; granular default
    FLOW_CODES=M,X               # M=import, X=export
    PARTNER_CODE=all             # 'all' = every partner; or e.g. '0'

    # Hard limits
    MAX_RECORDS=100000           # per-request cap (Comtrade Plus)
    MAX_DAILY_CALLS=480          # leave headroom under the 500/day cap

    # Optional: test on a few reporters
    # REPORTER_CODES=156,840,276

    # Optional MySQL append
    LOAD_TO_MYSQL=false
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from sqlalchemy import text
from sqlalchemy.dialects.mysql import insert as mysql_insert

import pandas as pd
import comtradeapicall
from dotenv import load_dotenv


# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

load_dotenv()

SUBSCRIPTION_KEY = os.getenv("COMTRADE_SUBSCRIPTION_KEY")

# --- Comtrade query settings ---
TYPE_CODE = os.getenv("TYPE_CODE", "C")          # C = goods, S = services
FREQ_CODE = os.getenv("FREQ_CODE", "A").upper()  # A annual, M monthly
CL_CODE = os.getenv("CL_CODE", "HS")             # HS = current edition

# Granularity.  AG6 = all 6-digit HS subheadings (most detailed level).
# Can also be a specific code (e.g. '010121'), 'TOTAL', or 'AG2'/'AG4'.
CMD_CODE = os.getenv("CMD_CODE", "AG6")

# Period window.  When FREQ_CODE=A, use YYYY.  When FREQ_CODE=M, use YYYYMM.
START_PERIOD = os.getenv("START_PERIOD", "2019")
END_PERIOD = os.getenv("END_PERIOD", "2024")

# Flows.  M=Import, X=Export.  See Tradeflow_Mapping for extended set.
FLOW_CODES = [
    f.strip() for f in os.getenv("FLOW_CODES", "M,X").split(",") if f.strip()
]

# Partner.  'all' or 'none' or blank -> partnerCode=None (every partner).
# Numeric (e.g. '0') -> just the World aggregate.
_partner_env = os.getenv("PARTNER_CODE", "all").strip().lower()
PARTNER_CODE: Optional[str] = (
    None if _partner_env in {"all", "none", "null", ""} else _partner_env
)

# --- Rate limit and pagination ---
MAX_RECORDS = int(os.getenv("MAX_RECORDS", "100000"))
MAX_DAILY_CALLS = int(os.getenv("MAX_DAILY_CALLS", "480"))
SLEEP_SECONDS = float(os.getenv("SLEEP_SECONDS", "1.0"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "4"))
RETRY_BACKOFF_SECONDS = float(os.getenv("RETRY_BACKOFF_SECONDS", "5.0"))

# Max URL-encoded length for the cmdCode query-param VALUE.  The
# Comtrade server rejects URLs over 2000 chars total.  urllib3
# percent-encodes commas as "%2C" (1 byte -> 3), so 200 6-digit
# codes joined with commas balloon from 1399 raw chars to 1797 in
# the URL.  Budget 1600 chars of URL-encoded value here, which
# leaves ~250 chars of headroom for the base URL + other params +
# the subscription key.
MAX_CMDCODE_URL_CHARS = int(os.getenv("MAX_CMDCODE_URL_CHARS", "1600"))

# --- Optional reporter filter for test runs ---
REPORTER_CODES_ENV = os.getenv("REPORTER_CODES")

# --- Output paths ---
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "comtrade_output"))
MASTER_CSV = OUTPUT_DIR / "comtrade_master.csv"
MANIFEST_PATH = OUTPUT_DIR / "manifest.csv"
BUDGET_PATH = OUTPUT_DIR / "budget.json"
LOG_PATH = OUTPUT_DIR / "loader.log"
REF_DIR = OUTPUT_DIR / "reference"

# --- Optional MySQL load ---
LOAD_TO_MYSQL = os.getenv("LOAD_TO_MYSQL", "false").lower() == "true"
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")

FACT_TABLE = os.getenv("FACT_TABLE", "fact_trade_granular")

# --- Master CSV output ---
# Defaults to True for backward compatibility.  Set to False in .env
# to skip CSV writes entirely (MySQL becomes the only sink).
WRITE_MASTER_CSV = os.getenv("WRITE_MASTER_CSV", "true").lower() == "true"

# Sanity check: data must flow somewhere.
if not WRITE_MASTER_CSV and not LOAD_TO_MYSQL:
    raise ValueError(
        "Both WRITE_MASTER_CSV and LOAD_TO_MYSQL are disabled -- "
        "results would have nowhere to go.  Enable at least one."
    )

# --- Country mapping file (from your project) ---
COUNTRY_MAPPING_CSV = os.getenv("COUNTRY_MAPPING_CSV", "Country_Mapping_Data.csv")

# --- Commodity mapping file (required for AG6 chapter subdivision) ---
COMMODITY_MAPPING_CSV = os.getenv(
    "COMMODITY_MAPPING_CSV", "Commodity_Code_Mapping.csv"
)


# ----------------------------------------------------------------------
# SETUP
# ----------------------------------------------------------------------

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REF_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("comtrade_loader")


# ----------------------------------------------------------------------
# DAILY BUDGET
# ----------------------------------------------------------------------

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_budget() -> dict:
    if BUDGET_PATH.exists():
        try:
            data = json.loads(BUDGET_PATH.read_text())
            if data.get("date") == _today():
                return data
        except Exception:
            logger.warning("budget.json unreadable; resetting.")
    return {"date": _today(), "calls": 0}


def save_budget(budget: dict) -> None:
    BUDGET_PATH.write_text(json.dumps(budget, indent=2))


def remaining_budget(budget: dict) -> int:
    return MAX_DAILY_CALLS - int(budget.get("calls", 0))


def increment_budget(budget: dict, n: int = 1) -> None:
    budget["calls"] = int(budget.get("calls", 0)) + n
    budget["date"] = _today()
    save_budget(budget)


# ----------------------------------------------------------------------
# PERIOD HELPERS
# ----------------------------------------------------------------------

def period_range(start: str, end: str, freq: str) -> list[str]:
    """Generate the list of period strings between start and end (inclusive)."""
    if freq == "A":
        s, e = int(start), int(end)
        return [str(y) for y in range(s, e + 1)]
    if freq == "M":
        s = datetime.strptime(start, "%Y%m")
        e = datetime.strptime(end, "%Y%m")
        out: list[str] = []
        cur = s
        while cur <= e:
            out.append(cur.strftime("%Y%m"))
            y = cur.year + (cur.month // 12)
            m = (cur.month % 12) + 1
            cur = cur.replace(year=y, month=m)
        return out
    if freq == "Q":
        # Quarterly is supported in the API but not commonly needed.
        s_year, s_q = int(start[:4]), int(start[-1])
        e_year, e_q = int(end[:4]), int(end[-1])
        out = []
        y, q = s_year, s_q
        while (y, q) <= (e_year, e_q):
            out.append(f"{y}Q{q}")
            q += 1
            if q > 4:
                q = 1
                y += 1
        return out
    raise ValueError(f"Unsupported FREQ_CODE={freq!r}; use 'A', 'Q', or 'M'.")


# ----------------------------------------------------------------------
# REPORTER LIST
# ----------------------------------------------------------------------

def get_reporter_codes() -> list[str]:
    """Resolve the list of reporter M49 codes to iterate over.

    Priority:
      1. REPORTER_CODES env var (manual list for test runs).
      2. Country_Mapping_Data.csv from the project (preferred -- it
         already excludes country groups via isGroup=False).
      3. Fall back to comtradeapicall.getReference('reporter').
    """
    if REPORTER_CODES_ENV:
        codes = [c.strip() for c in REPORTER_CODES_ENV.split(",") if c.strip()]
        logger.info("Using REPORTER_CODES from env: %d reporters", len(codes))
        return codes

    project_path = Path(COUNTRY_MAPPING_CSV)
    if project_path.exists():
        df = pd.read_csv(project_path, encoding="cp1252", engine="python",
                         on_bad_lines="skip", index_col=False)
        # The project file has an unusual layout: column 'id' holds the
        # numeric M49 code (as string).  isGroup=false means real country.
        code_col = "reporterCode" if "reporterCode" in df.columns else "id"
        df[code_col] = df[code_col].astype(str).str.strip()
        if "isGroup" in df.columns:
            df = df[df["isGroup"].astype(str).str.lower() != "true"]
        df = df[df[code_col].str.fullmatch(r"\d+")]
        codes = df[code_col].dropna().unique().tolist()
        logger.info("Loaded %d reporters from %s", len(codes), project_path)
        return codes

    # Last resort: hit the API for a reference list.
    logger.info("Country mapping CSV not found; fetching reporter reference.")
    ref = comtradeapicall.getReference("reporter")
    code_col = next(
        (c for c in ("reporterCode", "id", "code", "Code") if c in ref.columns),
        None,
    )
    if code_col is None:
        raise RuntimeError(f"Could not find code column in {list(ref.columns)}")
    ref[code_col] = ref[code_col].astype(str)
    ref = ref[~ref[code_col].isin(["0", "all", "All", "WORLD", "World"])]
    ref.to_csv(REF_DIR / "reporter_reference.csv", index=False)
    return ref[code_col].dropna().astype(str).unique().tolist()


# ----------------------------------------------------------------------
# MANIFEST
# ----------------------------------------------------------------------

# The manifest key encodes everything that affects which rows a chunk
# pulls.  Changing CMD_CODE from TOTAL to AG6 produces a different
# manifest_key, so old TOTAL chunks won't be falsely treated as "done".

MANIFEST_COLS = [
    "manifest_key",
    "classification",
    "frequency",
    "cmd_code",
    "period",
    "reporter_code",
    "flow_code",
    "partner_code",
    "status",          # success | failed | truncated | empty
    "rows",
    "n_api_calls",
    "chunk_id",
    "error",
    "updated_at",
]


def build_manifest_key(
    *,
    classification: str,
    frequency: str,
    cmd_code: str,
    period: str,
    reporter_code: str,
    flow_code: str,
    partner_code: Optional[str],
) -> str:
    return "|".join(
        [
            classification,
            frequency,
            cmd_code,
            str(period),
            str(reporter_code),
            str(flow_code),
            str(partner_code) if partner_code is not None else "ALL",
        ]
    )


def load_manifest() -> pd.DataFrame:
    if MANIFEST_PATH.exists():
        return pd.read_csv(MANIFEST_PATH, dtype=str)
    return pd.DataFrame(columns=MANIFEST_COLS)


def save_manifest(manifest: pd.DataFrame) -> None:
    manifest.to_csv(MANIFEST_PATH, index=False)


def manifest_lookup(manifest: pd.DataFrame, key: str) -> Optional[pd.Series]:
    if manifest.empty:
        return None
    hit = manifest[manifest["manifest_key"] == key]
    if hit.empty:
        return None
    return hit.iloc[-1]


def upsert_manifest(manifest: pd.DataFrame, row: dict) -> pd.DataFrame:
    key = row["manifest_key"]
    row = {**{c: "" for c in MANIFEST_COLS}, **row}
    row["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not manifest.empty and (manifest["manifest_key"] == key).any():
        manifest.loc[manifest["manifest_key"] == key, MANIFEST_COLS] = [
            row[c] for c in MANIFEST_COLS
        ]
    else:
        manifest = pd.concat(
            [manifest, pd.DataFrame([{c: row[c] for c in MANIFEST_COLS}])],
            ignore_index=True,
        )
    save_manifest(manifest)
    return manifest


# Statuses that mean "do not spend another API call on this chunk".
# Important: empty chunks still used one API call and should be skipped on rerun.
TERMINAL_STATUSES = {"success", "empty", "truncated"}


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(value))
    except Exception:
        return default


def get_manifest_row_for_job(manifest: pd.DataFrame, job: "ChunkJob") -> Optional[pd.Series]:
    """Return prior manifest row for a planned job, if any."""
    return manifest_lookup(manifest, job.manifest_key())


def status_is_terminal(status: Optional[str]) -> bool:
    return str(status or "").strip().lower() in TERMINAL_STATUSES


def build_resume_stats(manifest: pd.DataFrame, jobs: list["ChunkJob"]) -> dict:
    """
    Build resume/progress stats from manifest for the currently planned jobs.

    A reporter is considered "looked at" for a period if at least one of its
    planned flow chunks has a terminal manifest status: success, empty, truncated.
    A reporter is "fully complete" for a period if all of its planned flow chunks
    for that period are terminal.
    """
    planned_by_period: dict[str, set[str]] = {}
    statuses_by_period: dict[str, dict[str, int]] = {}
    terminal_reporters_by_period: dict[str, set[str]] = {}
    full_reporters_by_period: dict[str, set[str]] = {}
    pending_jobs: list[ChunkJob] = []

    for job in jobs:
        planned_by_period.setdefault(job.period, set()).add(job.reporter_code)

    jobs_by_period_reporter: dict[tuple[str, str], list[ChunkJob]] = {}
    for job in jobs:
        jobs_by_period_reporter.setdefault((job.period, job.reporter_code), []).append(job)

        row = get_manifest_row_for_job(manifest, job)
        status = str(row.get("status")) if row is not None else "not_started"
        statuses_by_period.setdefault(job.period, {})
        statuses_by_period[job.period][status] = statuses_by_period[job.period].get(status, 0) + 1

        if status_is_terminal(status):
            terminal_reporters_by_period.setdefault(job.period, set()).add(job.reporter_code)
        else:
            pending_jobs.append(job)

    for (period, reporter), reporter_jobs in jobs_by_period_reporter.items():
        if all(
            (row := get_manifest_row_for_job(manifest, j)) is not None
            and status_is_terminal(row.get("status"))
            for j in reporter_jobs
        ):
            full_reporters_by_period.setdefault(period, set()).add(reporter)

    return {
        "planned_by_period": planned_by_period,
        "statuses_by_period": statuses_by_period,
        "terminal_reporters_by_period": terminal_reporters_by_period,
        "full_reporters_by_period": full_reporters_by_period,
        "pending_jobs": pending_jobs,
    }


def log_resume_summary(manifest: pd.DataFrame, jobs: list["ChunkJob"]) -> None:
    """Log which reporters/chunks have already been looked at from prior runs."""
    stats = build_resume_stats(manifest, jobs)

    total_chunks = len(jobs)
    terminal_chunks = 0
    for job in jobs:
        row = get_manifest_row_for_job(manifest, job)
        if row is not None and status_is_terminal(row.get("status")):
            terminal_chunks += 1

    logger.info(
        "%d/%d chunks already terminal from previous runs "
        "(statuses counted as done=%s).",
        terminal_chunks,
        total_chunks,
        sorted(TERMINAL_STATUSES),
    )

    for period in sorted(stats["planned_by_period"].keys()):
        planned_reporters = stats["planned_by_period"][period]
        looked_reporters = stats["terminal_reporters_by_period"].get(period, set())
        full_reporters = stats["full_reporters_by_period"].get(period, set())
        status_counts = stats["statuses_by_period"].get(period, {})

        logger.info(
            "Resume summary period=%s: reporters looked at=%d/%d; "
            "reporters fully complete=%d/%d; chunk_status_counts=%s",
            period,
            len(looked_reporters),
            len(planned_reporters),
            len(full_reporters),
            len(planned_reporters),
            dict(sorted(status_counts.items())),
        )

        if looked_reporters:
            sample = ", ".join(sorted(looked_reporters, key=lambda x: int(x) if str(x).isdigit() else str(x))[:25])
            extra = "..." if len(looked_reporters) > 25 else ""
            logger.info(
                "Reporters already looked at for period=%s: %s%s",
                period,
                sample,
                extra,
            )

    if stats["pending_jobs"]:
        nxt = stats["pending_jobs"][0]
        logger.info(
            "Next pending chunk appears to be period=%s reporter=%s flow=%s",
            nxt.period,
            nxt.reporter_code,
            nxt.flow_code,
        )
    else:
        logger.info("No pending chunks for the current plan.")



# ----------------------------------------------------------------------
# CLEANING / COLUMN NORMALIZATION
# ----------------------------------------------------------------------

# Column rename map -- aligned to the official ComtradePlus_DataItems
# reference.  Only fields actually returned by the goods Data API (C)
# are kept.  Any field missing from a response is silently skipped.
COLUMN_RENAME = {
    "datasetCode": "dataset_code",
    "typeCode": "type_code",
    "freqCode": "freq_code",
    "refPeriodId": "ref_period_id",
    "refYear": "ref_year",
    "refMonth": "ref_month",
    "period": "period",
    "reporterCode": "reporter_code",
    "reporterISO": "reporter_iso",
    "reporterDesc": "reporter_desc",
    "flowCode": "flow_code",
    "flowDesc": "flow_desc",
    "partnerCode": "partner_code",
    "partnerISO": "partner_iso",
    "partnerDesc": "partner_desc",
    "partner2Code": "partner2_code",
    "partner2ISO": "partner2_iso",
    "partner2Desc": "partner2_desc",
    "classificationCode": "classification_code",
    "classificationSearchCode": "classification_search_code",
    "isOriginalClassification": "is_original_classification",
    "cmdCode": "cmd_code",
    "cmdDesc": "cmd_desc",
    "aggrLevel": "aggr_level",
    "isLeaf": "is_leaf",
    "customsCode": "customs_code",
    "customsDesc": "customs_desc",
    "motCode": "mot_code",
    "motDesc": "mot_desc",
    "qtyUnitCode": "qty_unit_code",
    "qtyUnitAbbr": "qty_unit_abbr",
    "qty": "qty",
    "isQtyEstimated": "is_qty_estimated",
    "altQtyUnitCode": "alt_qty_unit_code",
    "altQtyUnitAbbr": "alt_qty_unit_abbr",
    "altQty": "alt_qty",
    "isAltQtyEstimated": "is_alt_qty_estimated",
    "netWgt": "net_weight",
    "isNetWgtEstimated": "is_net_weight_estimated",
    "isnetWgtEstimated": "is_net_weight_estimated",   # api inconsistency
    "grossWgt": "gross_weight",
    "isGrossWgtEstimated": "is_gross_weight_estimated",
    "isgrossWgtEstimated": "is_gross_weight_estimated",
    "cifValue": "cif_value_usd",
    "fobValue": "fob_value_usd",
    "primaryValue": "primary_value_usd",
    "legacyEstimationFlag": "legacy_estimation_flag",
    "isReported": "is_reported",
    "isAggregate": "is_aggregate",
}

STRING_COLUMNS = [
    "period", "reporter_code", "reporter_iso", "partner_code", "partner_iso",
    "partner2_code", "partner2_iso", "cmd_code", "flow_code",
    "classification_code", "customs_code", "mot_code",
    "qty_unit_code", "alt_qty_unit_code",
]
NUMERIC_COLUMNS = [
    "qty", "alt_qty", "net_weight", "gross_weight",
    "cif_value_usd", "fob_value_usd", "primary_value_usd",
    "aggr_level",
]
BOOL_COLUMNS = [
    "is_leaf", "is_qty_estimated", "is_alt_qty_estimated",
    "is_net_weight_estimated", "is_gross_weight_estimated",
    "is_reported", "is_aggregate", "is_original_classification",
]


def clean_df(df: pd.DataFrame, chunk_id: str) -> pd.DataFrame:
    """Normalize column names, coerce types, stamp chunk_id."""
    keep = {src: dst for src, dst in COLUMN_RENAME.items() if src in df.columns}
    out = df[list(keep.keys())].rename(columns=keep).copy()

    for c in STRING_COLUMNS:
        if c in out.columns:
            out[c] = out[c].astype(str).where(out[c].notna(), None)

    for c in NUMERIC_COLUMNS:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    for c in BOOL_COLUMNS:
        if c in out.columns:
            # API returns true/false strings or python bools depending on version
            out[c] = out[c].map(
                lambda v: True if str(v).strip().lower() in ("true", "1")
                else False if str(v).strip().lower() in ("false", "0")
                else None
            )

    out["chunk_id"] = chunk_id
    out["loaded_at_utc"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    return out


# ----------------------------------------------------------------------
# API FETCH
# ----------------------------------------------------------------------

class DailyQuotaExceeded(RuntimeError):
    """Raised when the API rejects further calls for the day."""


class NonRetryableClientError(RuntimeError):
    """Raised for client-side errors that won't fix themselves on retry
    (e.g. URL exceeds maximum length).  Skips the backoff loop."""


_QUOTA_PATTERNS = re.compile(
    r"(429|quota|rate.?limit|too many requests|subscription)",
    flags=re.IGNORECASE,
)

_URL_TOO_LONG_PATTERNS = re.compile(
    r"(url.{0,20}(exceed|too long|maximum.{0,10}length)|414)",
    flags=re.IGNORECASE,
)


def _looks_like_quota_error(exc: BaseException) -> bool:
    return bool(_QUOTA_PATTERNS.search(repr(exc)))


def _looks_like_url_too_long(exc: BaseException) -> bool:
    return bool(_URL_TOO_LONG_PATTERNS.search(repr(exc)))


def fetch_once(
    *,
    period: str,
    reporter_code: str,
    flow_code: str,
    cmd_code: str,
) -> pd.DataFrame:
    """Single Comtrade API call.  Returns a DataFrame, possibly empty.

    Raises:
        DailyQuotaExceeded: when the API signals quota/rate-limit.
        RuntimeError:       any other API failure.
    """
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
        raise RuntimeError(
            "comtradeapicall returned None (transport or auth error)."
        )
    return df


def fetch_with_retries(
    *,
    period: str,
    reporter_code: str,
    flow_code: str,
    cmd_code: str,
    budget: dict,
) -> tuple[pd.DataFrame, int]:
    """Fetch one chunk with exponential backoff.  Returns (df, n_calls)."""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        # Budget guard inside the loop too (in case retries push past).
        if remaining_budget(budget) <= 0:
            raise DailyQuotaExceeded("Local daily budget exhausted.")
        try:
            increment_budget(budget, 1)
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
            wait = RETRY_BACKOFF_SECONDS * attempt
            logger.warning(
                "Attempt %d/%d failed for period=%s reporter=%s flow=%s: %r"
                " -- retrying in %.1fs",
                attempt, RETRY_ATTEMPTS, period, reporter_code, flow_code,
                exc, wait,
            )
            time.sleep(wait)
    raise RuntimeError(
        f"Failed after {RETRY_ATTEMPTS} attempts for period={period} "
        f"reporter={reporter_code} flow={flow_code}: {last_exc!r}"
    )


# ----------------------------------------------------------------------
# OVERFLOW SUBDIVISION
# ----------------------------------------------------------------------

# When AG6 + all partners overflows the 100k cap, we refetch the data
# in batches of 6-digit leaf codes.
#
# Two failure modes the naive approach hit:
#   1. cmdCode="01" returns the aggregate row for chapter 01, not the
#      6-digit detail beneath it.  So we must pass actual leaf codes.
#   2. The Comtrade client enforces a 2000-char URL limit.  Big
#      chapters (84 = 597 codes, 29 = 489 codes, 03 = 282 codes) all
#      bust that ceiling when joined comma-separated.
#
# Solution: pack all 6,897 leaf codes (from Commodity_Code_Mapping.csv)
# into URL-length-safe batches.  At ~7 chars/code and a 1500-char
# cmdCode budget, that's ~33 batches per truncated chunk.

_LEAF_CODE_BATCHES: Optional[list[list[str]]] = None


def _pack_codes_into_batches(
    codes: list[str], max_url_value_chars: int
) -> list[list[str]]:
    """Pack codes into batches whose URL-ENCODED length <= the budget.

    urllib3 percent-encodes commas as "%2C" (3 bytes each) when
    building the query string, so each separator costs 3 chars in the
    actual URL, not 1.  Encoded length of a batch:
        sum(len(code)) + 3 * (n - 1)
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for code in codes:
        # +3 accounts for the URL-encoded comma separator (%2C),
        # skipped on the first item of a batch.
        add = len(code) + (3 if current else 0)
        if current_len + add > max_url_value_chars and current:
            batches.append(current)
            current = [code]
            current_len = len(code)
        else:
            current.append(code)
            current_len += add
    if current:
        batches.append(current)
    return batches


def get_leaf_code_batches() -> list[list[str]]:
    """Return all 6-digit HS leaf codes packed into URL-safe batches."""
    global _LEAF_CODE_BATCHES
    if _LEAF_CODE_BATCHES is not None:
        return _LEAF_CODE_BATCHES

    path = Path(COMMODITY_MAPPING_CSV)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found.  Subdivision needs the leaf-code mapping; "
            "point COMMODITY_MAPPING_CSV at the project's "
            "Commodity_Code_Mapping.csv."
        )

    df = pd.read_csv(path, encoding="cp1252", dtype=str, index_col=False)
    leaves = (
        df.loc[df["aggrLevel"].astype(str) == "6", "id"]
        .dropna()
        .astype(str)
        .tolist()
    )
    leaves = sorted({c for c in leaves if len(c) == 6 and c.isdigit()})

    if not leaves:
        raise RuntimeError(
            "No leaf codes loaded from Commodity_Code_Mapping.csv -- "
            "check the file's aggrLevel column."
        )

    batches = _pack_codes_into_batches(leaves, MAX_CMDCODE_URL_CHARS)
    _LEAF_CODE_BATCHES = batches
    longest_encoded = max(
        sum(len(c) for c in b) + 3 * (len(b) - 1) for b in batches
    )
    logger.info(
        "Leaf-code batches ready: %d batches, %d total codes "
        "(avg %.0f codes/batch, longest URL-encoded value %d chars, "
        "limit %d).",
        len(batches), len(leaves),
        len(leaves) / len(batches),
        longest_encoded, MAX_CMDCODE_URL_CHARS,
    )
    return batches


def fetch_chunk(
    *,
    period: str,
    reporter_code: str,
    flow_code: str,
    budget: dict,
) -> tuple[pd.DataFrame, int, bool]:
    """Fetch a (period, reporter, flow) chunk at CMD_CODE granularity.

    If the first call hits MAX_RECORDS exactly (likely truncated), the
    chunk is refetched in URL-length-safe leaf-code batches.

    Returns:
        (dataframe, total_api_calls, was_truncated)

        was_truncated=True means at least one sub-batch STILL hit the
        cap (data is partially missing -- a single batch returned
        >= MAX_RECORDS rows, suggesting that batch needs finer
        partitioning, e.g. per-partner).
    """
    df, n_calls = fetch_with_retries(
        period=period,
        reporter_code=reporter_code,
        flow_code=flow_code,
        cmd_code=CMD_CODE,
        budget=budget,
    )

    if len(df) < MAX_RECORDS:
        return df, n_calls, False

    if CMD_CODE != "AG6":
        logger.warning(
            "Truncation hit but CMD_CODE=%r is not AG6 -- "
            "returning truncated result.", CMD_CODE,
        )
        return df, n_calls, True

    logger.warning(
        "period=%s reporter=%s flow=%s hit MAX_RECORDS=%d -- "
        "subdividing into leaf-code batches.",
        period, reporter_code, flow_code, MAX_RECORDS,
    )

    batches = get_leaf_code_batches()
    pieces: list[pd.DataFrame] = []
    total_calls = n_calls
    truncated_batches: list[int] = []

    for idx, batch in enumerate(batches, 1):
        if remaining_budget(budget) <= 0:
            raise DailyQuotaExceeded(
                "Budget exhausted during leaf-code subdivision."
            )

        cmd_filter = ",".join(batch)
        try:
            sub_df, sub_calls = fetch_with_retries(
                period=period,
                reporter_code=reporter_code,
                flow_code=flow_code,
                cmd_code=cmd_filter,
                budget=budget,
            )
        except DailyQuotaExceeded:
            raise
        except NonRetryableClientError as exc:
            logger.error(
                "Batch %d/%d (%s..%s) hit a non-retryable client error "
                "for period=%s reporter=%s flow=%s: %r  "
                "Reduce MAX_CMDCODE_URL_CHARS and rerun.",
                idx, len(batches), batch[0], batch[-1],
                period, reporter_code, flow_code, exc,
            )
            total_calls += 1
            truncated_batches.append(idx)
            continue
        except Exception as exc:
            logger.warning(
                "Batch %d/%d (%s..%s) failed for period=%s reporter=%s "
                "flow=%s: %r",
                idx, len(batches), batch[0], batch[-1],
                period, reporter_code, flow_code, exc,
            )
            total_calls += 1
            continue

        total_calls += sub_calls

        if len(sub_df) >= MAX_RECORDS:
            logger.warning(
                "Batch %d/%d (%s..%s) for period=%s reporter=%s flow=%s "
                "ALSO hit the cap -- data within this batch is "
                "truncated.  Consider per-partner subdivision for this "
                "reporter.",
                idx, len(batches), batch[0], batch[-1],
                period, reporter_code, flow_code,
            )
            truncated_batches.append(idx)

        if not sub_df.empty:
            pieces.append(sub_df)
        time.sleep(SLEEP_SECONDS)

    combined = (
        pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    )
    return combined, total_calls, bool(truncated_batches)


# ----------------------------------------------------------------------
# MASTER CSV APPEND
# ----------------------------------------------------------------------

def append_to_master(df: pd.DataFrame) -> None:
    """Append cleaned rows to the single master CSV (header only once)."""
    if not WRITE_MASTER_CSV:
        return
    if df.empty:
        return
    header = not MASTER_CSV.exists()
    df.to_csv(MASTER_CSV, mode="a", header=header, index=False)


def remove_chunk_rows_from_master(chunk_id: str) -> int:
    """Remove rows tagged with a given chunk_id from the master CSV.

    Used when restarting a chunk whose previous run crashed mid-append.
    Returns the number of rows removed.
    """
    if not WRITE_MASTER_CSV:
        return 0
    if not MASTER_CSV.exists():
        return 0
    # Stream the file to avoid loading everything if it's huge.
    tmp = MASTER_CSV.with_suffix(".csv.tmp")
    removed = 0
    kept = 0
    chunksize = 200_000
    first = True
    for piece in pd.read_csv(MASTER_CSV, chunksize=chunksize, dtype=str):
        mask = piece.get("chunk_id", pd.Series(dtype=str)) == chunk_id
        removed += int(mask.sum())
        piece = piece[~mask]
        kept += len(piece)
        piece.to_csv(tmp, mode="a", header=first, index=False)
        first = False
    tmp.replace(MASTER_CSV)
    return removed


# ----------------------------------------------------------------------
# OPTIONAL MYSQL APPEND
# ----------------------------------------------------------------------

def get_engine():
    if not LOAD_TO_MYSQL:
        return None
    missing = [k for k, v in dict(
        DB_USER=DB_USER, DB_PASS=DB_PASS, DB_HOST=DB_HOST, DB_NAME=DB_NAME,
    ).items() if not v]
    if missing:
        raise ValueError(f"LOAD_TO_MYSQL=true but missing env vars: {missing}")
    from sqlalchemy import create_engine
    from urllib.parse import quote_plus
    user = quote_plus(DB_USER)
    pw   = quote_plus(DB_PASS)
    host = DB_HOST
    port = os.getenv("DB_PORT", "3306")
    return create_engine(
        f"mysql+mysqlconnector://{user}:{pw}@{host}:{port}/{DB_NAME}", 
        pool_pre_ping=True,
        pool_recycle=3600,
    )


# Columns that have a SQL DEFAULT and are FK-constrained.  pandas
# to_sql sends explicit NULLs, which override the DEFAULT, so we
# coerce blanks/NaN to the documented sentinel value before insert.
FK_DEFAULTS = {
    "partner2_code": "0",
    "mot_code": "0",
    "customs_code": "C00",
}


def _ensure_parent_rows(engine, df: pd.DataFrame) -> None:
    """Insert placeholder rows into mapping tables for any code that
    appears in the fact data but not in the reference table.

    Why: the Comtrade API occasionally returns codes that are not in
    the published mapping CSV (newer HS revisions, "Areas n.e.s.",
    etc.).  Strict FKs would reject those rows.  Rather than dropping
    data, we insert a placeholder with text='UNKNOWN (<code>)' so the
    fact insert succeeds.  The placeholders can be replaced later
    when the official mapping is refreshed.
    """
    from sqlalchemy import text

    def codes(col: str) -> set[str]:
        if col not in df.columns:
            return set()
        s = df[col].dropna().astype(str)
        s = s[(s != "") & (s.str.lower() != "nan") & (s.str.lower() != "none")]
        return set(s.unique())

    # (fact_column, parent_table, parent_pk, placeholder_text_column,
    #  extra_required_cols)
    fk_specs = [
        ("reporter_code", "country_mapping", "country_code",
            "country_text", {}),
        ("partner_code",  "country_mapping", "country_code",
            "country_text", {}),
        ("partner2_code", "country_mapping", "country_code",
            "country_text", {}),
        ("cmd_code",      "commodity_code_mapping", "cmd_code",
            "cmd_text", {"aggr_level": 0, "is_leaf": 0}),
        ("flow_code",     "tradeflow_mapping", "flow_code",
            "flow_desc", {}),
        ("freq_code",     "frequency_mapping", "freq_code",
            "freq_desc", {}),
        ("mot_code",      "transport_mapping", "mot_code",
            "mot_desc", {}),
        ("qty_unit_code", "unit_quantity_mapping", "qty_code",
            "qty_description", {"qty_abbr": "UNK"}),
        ("alt_qty_unit_code", "unit_quantity_mapping", "qty_code",
            "qty_description", {"qty_abbr": "UNK"}),
    ]

    with engine.begin() as conn:
        for fact_col, parent_tbl, parent_pk, text_col, extras in fk_specs:
            wanted = codes(fact_col)
            if not wanted:
                continue
            existing = {
                row[0] for row in conn.execute(
                    text(
                        f"SELECT {parent_pk} FROM {parent_tbl} "
                        f"WHERE {parent_pk} IN :codes"
                    ).bindparams(
                        __import__("sqlalchemy").bindparam(
                            "codes", expanding=True
                        )
                    ),
                    {"codes": list(wanted)},
                )
            }
            missing = wanted - existing
            if not missing:
                continue
            logger.info(
                "Inserting %d placeholder rows into %s for unknown "
                "%s codes: %s",
                len(missing), parent_tbl, fact_col,
                ", ".join(sorted(missing)[:10])
                + ("..." if len(missing) > 10 else ""),
            )
            cols = [parent_pk, text_col] + list(extras.keys())
            placeholders = ", ".join(f":{c}" for c in cols)
            insert_sql = text(
                f"INSERT IGNORE INTO {parent_tbl} ({', '.join(cols)}) "
                f"VALUES ({placeholders})"
            )
            rows = [
                {parent_pk: code,
                 text_col: f"UNKNOWN ({code})",
                 **extras}
                for code in sorted(missing)
            ]
            conn.execute(insert_sql, rows)


def remove_chunk_rows_from_mysql(engine, chunk_id: str) -> int:
    """Delete all fact rows tagged with a given chunk_id.

    Mirrors remove_chunk_rows_from_master().  Called when restarting
    a chunk whose previous attempt crashed -- prevents unique-key
    collisions when the same logical rows are re-fetched and inserted.
    """
    if engine is None or not chunk_id:
        return 0
    from sqlalchemy import text
    with engine.begin() as conn:
        result = conn.execute(
            text(f"DELETE FROM {FACT_TABLE} WHERE chunk_id = :cid"),
            {"cid": chunk_id},
        )
        return result.rowcount or 0

def delete_trade_scope(engine, df: pd.DataFrame) -> None:
    if df.empty:
        return

    period = str(df["period"].iloc[0])
    reporter_code = str(df["reporter_code"].iloc[0])
    flow_code = str(df["flow_code"].iloc[0])

    sql = f"""
    DELETE FROM {FACT_TABLE}
    WHERE period = :period
      AND reporter_code = :reporter_code
      AND flow_code = :flow_code
    """

    with engine.begin() as conn:
        conn.execute(
            text(sql),
            {
                "period": period,
                "reporter_code": reporter_code,
                "flow_code": flow_code,
            },
        )


def mysql_insert_ignore_duplicates(table, conn, keys, data_iter):
    """
    Custom pandas.to_sql insert method for MySQL.

    Uses INSERT IGNORE so rows that violate a UNIQUE KEY / PRIMARY KEY
    are skipped instead of crashing the loader.

    This is useful for resumable jobs where a chunk may have partially
    loaded in a previous run.
    """
    rows = [dict(zip(keys, row)) for row in data_iter]

    if not rows:
        return 0

    stmt = mysql_insert(table.table).values(rows).prefix_with("IGNORE")
    result = conn.execute(stmt)

    # With INSERT IGNORE, rowcount is the number of rows actually inserted.
    # Duplicate rows are ignored and not counted as inserted.
    return result.rowcount or 0


def write_to_mysql(engine, df: pd.DataFrame) -> None:
    """
    Write a cleaned Comtrade dataframe to MySQL safely.

    Behavior:
      - Fills FK default/sentinel values.
      - Ensures parent/reference rows exist before fact insert.
      - Inserts new rows into FACT_TABLE.
      - Skips duplicate rows using INSERT IGNORE.
      - Does NOT delete existing rows.
      - Does NOT crash on duplicate unique-key conflicts.
    """
    if engine is None:
        logger.warning("write_to_mysql skipped because engine is None.")
        return

    if df is None or df.empty:
        logger.info("write_to_mysql skipped because dataframe is empty.")
        return

    df = df.copy()

    # ------------------------------------------------------------
    # 1. Normalize missing values for FK/default columns
    # ------------------------------------------------------------
    for col, default in FK_DEFAULTS.items():
        if col in df.columns:
            df[col] = (
                df[col]
                .fillna(default)
                .replace("", default)
                .astype(str)
            )

    # ------------------------------------------------------------
    # 2. Normalize natural-key columns as strings
    # ------------------------------------------------------------
    natural_key_cols = [
        "period",
        "reporter_code",
        "flow_code",
        "partner_code",
        "partner2_code",
        "cmd_code",
        "customs_code",
        "mot_code",
    ]

    for col in natural_key_cols:
        if col in df.columns:
            df[col] = df[col].astype(str).where(df[col].notna(), None)

    # ------------------------------------------------------------
    # 3. Ensure parent/reference rows exist before inserting facts
    # ------------------------------------------------------------
    try:
        _ensure_parent_rows(engine, df)
    except Exception:
        logger.exception(
            "Failed while ensuring parent/reference rows before insert. "
            "table=%s rows=%d sample=%s",
            FACT_TABLE,
            len(df),
            df.head(1).to_dict(orient="records"),
        )
        raise

    # ------------------------------------------------------------
    # 4. Insert rows, ignoring duplicate unique-key conflicts
    # ------------------------------------------------------------
    try:
        with engine.begin() as conn:
            inserted_count = df.to_sql(
                FACT_TABLE,
                con=conn,
                if_exists="append",
                index=False,
                chunksize=50,
                method=mysql_insert_ignore_duplicates,
            )

        inserted_count = 0 if inserted_count is None else int(inserted_count)
        skipped_count = max(len(df) - inserted_count, 0)

        logger.info(
            "MySQL insert complete for table=%s input_rows=%d inserted=%d "
            "skipped_duplicates_or_ignored=%d",
            FACT_TABLE,
            len(df),
            inserted_count,
            skipped_count,
        )

    except Exception:
        logger.exception(
            "to_sql failed for table=%s rows=%d first-row sample: %s",
            FACT_TABLE,
            len(df),
            df.head(1).to_dict(orient="records"),
        )
        raise


# ----------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------

@dataclass
class ChunkJob:
    period: str
    reporter_code: str
    flow_code: str

    def manifest_key(self) -> str:
        return build_manifest_key(
            classification=CL_CODE,
            frequency=FREQ_CODE,
            cmd_code=CMD_CODE,
            period=self.period,
            reporter_code=self.reporter_code,
            flow_code=self.flow_code,
            partner_code=PARTNER_CODE,
        )


def plan_jobs() -> list[ChunkJob]:
    periods = period_range(START_PERIOD, END_PERIOD, FREQ_CODE)
    reporters = get_reporter_codes()
    jobs: list[ChunkJob] = []
    for p in periods:
        for r in reporters:
            for f in FLOW_CODES:
                jobs.append(ChunkJob(period=str(p),
                                     reporter_code=str(r),
                                     flow_code=str(f)))
    return jobs


def main() -> None:
    if not SUBSCRIPTION_KEY:
        raise ValueError(
            "Missing COMTRADE_SUBSCRIPTION_KEY. Put it in .env or export it."
        )

    budget = load_budget()
    manifest = load_manifest()
    engine = get_engine()

    jobs = plan_jobs()

    # Progress trackers for this run.
    total_chunks = len(jobs)
    total_reporters = len({j.reporter_code for j in jobs})
    total_reporters_by_period = {}
    for j in jobs:
        total_reporters_by_period.setdefault(j.period, set()).add(j.reporter_code)

    attempted_chunks_this_run = 0
    skipped_chunks_this_run = 0
    empty_chunks_this_run = 0
    success_chunks_this_run = 0
    failed_chunks_this_run = 0
    seen_reporters_this_run_by_period: dict[str, set[str]] = {}

    sinks = []
    if WRITE_MASTER_CSV:
        sinks.append("CSV")
    if LOAD_TO_MYSQL:
        sinks.append("MySQL")

    logger.info(
        "Planned %d chunks | reporters=%d | cl=%s freq=%s cmd=%s flows=%s "
        "partner=%s sinks=%s budget_left_today=%d/%d",
        total_chunks,
        total_reporters,
        CL_CODE,
        FREQ_CODE,
        CMD_CODE,
        FLOW_CODES,
        PARTNER_CODE if PARTNER_CODE else "ALL",
        "+".join(sinks),
        remaining_budget(budget),
        MAX_DAILY_CALLS,
    )

    # Log what the manifest says has already been looked at before this run.
    log_resume_summary(manifest, jobs)

    for job_idx, job in enumerate(jobs, 1):
        key = job.manifest_key()
        prior = manifest_lookup(manifest, key)

        # Treat empty as done. Empty chunks already cost an API call.
        if prior is not None and status_is_terminal(prior.get("status")):
            skipped_chunks_this_run += 1
            logger.info(
                "Skipping completed chunk %d/%d: period=%s reporter=%s flow=%s "
                "status=%s rows=%s prior_calls=%s updated_at=%s "
                "| skipped_this_run=%d",
                job_idx,
                total_chunks,
                job.period,
                job.reporter_code,
                job.flow_code,
                prior.get("status"),
                prior.get("rows", ""),
                prior.get("n_api_calls", ""),
                prior.get("updated_at", ""),
                skipped_chunks_this_run,
            )
            continue

        # Log reporter progress when we first reach a reporter in this run.
        period_seen = seen_reporters_this_run_by_period.setdefault(job.period, set())
        if job.reporter_code not in period_seen:
            period_seen.add(job.reporter_code)

            stats = build_resume_stats(manifest, jobs)
            already_looked = stats["terminal_reporters_by_period"].get(job.period, set())
            fully_complete = stats["full_reporters_by_period"].get(job.period, set())
            period_total = len(total_reporters_by_period.get(job.period, set()))

            logger.info(
                "Reporter progress period=%s: reached reporter=%s in this run "
                "(this_run_reporters_reached=%d; previously_looked_reporters=%d/%d; "
                "previously_fully_complete_reporters=%d/%d)",
                job.period,
                job.reporter_code,
                len(period_seen),
                len(already_looked),
                period_total,
                len(fully_complete),
                period_total,
            )

        # If the prior attempt crashed mid-append, clean its rows first.
        if prior is not None and prior.get("status") in {"in_progress", "truncated_partial"}:
            stale_chunk_id = prior.get("chunk_id")
            logger.info(
                "Found stale prior chunk: period=%s reporter=%s flow=%s "
                "status=%s chunk_id=%s. Cleaning stale rows before retry.",
                job.period,
                job.reporter_code,
                job.flow_code,
                prior.get("status"),
                stale_chunk_id,
            )

            if stale_chunk_id:
                removed = remove_chunk_rows_from_master(stale_chunk_id)
                if removed:
                    logger.info(
                        "Removed %d stale rows from master CSV for chunk_id=%s",
                        removed,
                        stale_chunk_id,
                    )

                removed_db = remove_chunk_rows_from_mysql(engine, stale_chunk_id)
                if removed_db:
                    logger.info(
                        "Removed %d stale rows from MySQL for chunk_id=%s",
                        removed_db,
                        stale_chunk_id,
                    )

        elif prior is not None and prior.get("status") == "failed":
            logger.info(
                "Retrying previously failed chunk: period=%s reporter=%s flow=%s "
                "prior_error=%s updated_at=%s",
                job.period,
                job.reporter_code,
                job.flow_code,
                prior.get("error", ""),
                prior.get("updated_at", ""),
            )

        if remaining_budget(budget) <= 0:
            logger.warning(
                "Daily budget reached before fetching period=%s reporter=%s flow=%s. "
                "Stopping. Resume later; manifest will pick up here.",
                job.period,
                job.reporter_code,
                job.flow_code,
            )
            break

        chunk_id = uuid.uuid4().hex

        # Mark in_progress so a crash mid-append can be cleaned up next run.
        manifest = upsert_manifest(manifest, {
            "manifest_key": key,
            "classification": CL_CODE,
            "frequency": FREQ_CODE,
            "cmd_code": CMD_CODE,
            "period": job.period,
            "reporter_code": job.reporter_code,
            "flow_code": job.flow_code,
            "partner_code": str(PARTNER_CODE) if PARTNER_CODE else "ALL",
            "status": "in_progress",
            "rows": "0",
            "n_api_calls": "0",
            "chunk_id": chunk_id,
            "error": "",
        })

        attempted_chunks_this_run += 1

        logger.info(
            "Fetching chunk %d/%d | attempted_this_run=%d skipped_this_run=%d "
            "| period=%s reporter=%s flow=%s cmd=%s | budget_left_before=%d/%d",
            job_idx,
            total_chunks,
            attempted_chunks_this_run,
            skipped_chunks_this_run,
            job.period,
            job.reporter_code,
            job.flow_code,
            CMD_CODE,
            remaining_budget(budget),
            MAX_DAILY_CALLS,
        )

        try:
            df_raw, n_calls, subdivided = fetch_chunk(
                period=job.period,
                reporter_code=job.reporter_code,
                flow_code=job.flow_code,
                budget=budget,
            )

        except DailyQuotaExceeded as exc:
            logger.warning("Quota exceeded: %r -- stopping for today.", exc)
            manifest = upsert_manifest(manifest, {
                "manifest_key": key,
                "classification": CL_CODE,
                "frequency": FREQ_CODE,
                "cmd_code": CMD_CODE,
                "period": job.period,
                "reporter_code": job.reporter_code,
                "flow_code": job.flow_code,
                "partner_code": str(PARTNER_CODE) if PARTNER_CODE else "ALL",
                "status": "failed",
                "rows": "0",
                "n_api_calls": "0",
                "chunk_id": chunk_id,
                "error": f"DailyQuotaExceeded: {exc!r}"[:480],
            })
            failed_chunks_this_run += 1
            break

        except KeyboardInterrupt:
            logger.warning(
                "KeyboardInterrupt received while fetching period=%s reporter=%s flow=%s. "
                "Leaving chunk marked in_progress with chunk_id=%s so it can be cleaned/retried next run.",
                job.period,
                job.reporter_code,
                job.flow_code,
                chunk_id,
            )
            raise

        except Exception as exc:
            logger.exception(
                "Chunk failed: period=%s reporter=%s flow=%s",
                job.period,
                job.reporter_code,
                job.flow_code,
            )
            manifest = upsert_manifest(manifest, {
                "manifest_key": key,
                "classification": CL_CODE,
                "frequency": FREQ_CODE,
                "cmd_code": CMD_CODE,
                "period": job.period,
                "reporter_code": job.reporter_code,
                "flow_code": job.flow_code,
                "partner_code": str(PARTNER_CODE) if PARTNER_CODE else "ALL",
                "status": "failed",
                "rows": "0",
                "n_api_calls": "0",
                "chunk_id": chunk_id,
                "error": repr(exc)[:480],
            })
            failed_chunks_this_run += 1
            time.sleep(SLEEP_SECONDS)
            continue

        if df_raw is None or df_raw.empty:
            manifest = upsert_manifest(manifest, {
                "manifest_key": key,
                "classification": CL_CODE,
                "frequency": FREQ_CODE,
                "cmd_code": CMD_CODE,
                "period": job.period,
                "reporter_code": job.reporter_code,
                "flow_code": job.flow_code,
                "partner_code": str(PARTNER_CODE) if PARTNER_CODE else "ALL",
                "status": "empty",
                "rows": "0",
                "n_api_calls": str(n_calls),
                "chunk_id": chunk_id,
                "error": "",
            })

            empty_chunks_this_run += 1

            logger.info(
                "  -> empty result | period=%s reporter=%s flow=%s "
                "calls=%d budget_left=%d/%d | empty_this_run=%d",
                job.period,
                job.reporter_code,
                job.flow_code,
                n_calls,
                remaining_budget(budget),
                MAX_DAILY_CALLS,
                empty_chunks_this_run,
            )

            time.sleep(SLEEP_SECONDS)
            continue

        cleaned = clean_df(df_raw, chunk_id=chunk_id)
        append_to_master(cleaned)
        write_to_mysql(engine, cleaned)

        status = "truncated" if subdivided else "success"

        manifest = upsert_manifest(manifest, {
            "manifest_key": key,
            "classification": CL_CODE,
            "frequency": FREQ_CODE,
            "cmd_code": CMD_CODE,
            "period": job.period,
            "reporter_code": job.reporter_code,
            "flow_code": job.flow_code,
            "partner_code": str(PARTNER_CODE) if PARTNER_CODE else "ALL",
            "status": status,
            "rows": str(len(cleaned)),
            "n_api_calls": str(n_calls),
            "chunk_id": chunk_id,
            "error": "",
        })

        success_chunks_this_run += 1

        logger.info(
            "  -> %s rows=%d calls=%d budget_left=%d/%d | "
            "success_or_truncated_this_run=%d",
            status,
            len(cleaned),
            n_calls,
            remaining_budget(budget),
            MAX_DAILY_CALLS,
            success_chunks_this_run,
        )

        time.sleep(SLEEP_SECONDS)

    logger.info(
        "Run complete. Calls used today: %d/%d. This run: attempted=%d, "
        "skipped_terminal=%d, empty=%d, success_or_truncated=%d, failed=%d.",
        budget.get("calls", 0),
        MAX_DAILY_CALLS,
        attempted_chunks_this_run,
        skipped_chunks_this_run,
        empty_chunks_this_run,
        success_chunks_this_run,
        failed_chunks_this_run,
    )

    # Final view of manifest progress after this run.
    log_resume_summary(load_manifest(), jobs)


if __name__ == "__main__":
    main()