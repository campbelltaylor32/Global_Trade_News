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

# --- Country mapping file (from your project) ---
COUNTRY_MAPPING_CSV = os.getenv("COUNTRY_MAPPING_CSV", "Country_Mapping_Data.csv")


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
                         on_bad_lines="skip")
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


_QUOTA_PATTERNS = re.compile(
    r"(429|quota|rate.?limit|too many requests|subscription)",
    flags=re.IGNORECASE,
)


def _looks_like_quota_error(exc: BaseException) -> bool:
    return bool(_QUOTA_PATTERNS.search(repr(exc)))


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

HS_CHAPTERS = [f"{i:02d}" for i in range(1, 100)]   # '01'..'99'


def fetch_chunk(
    *,
    period: str,
    reporter_code: str,
    flow_code: str,
    budget: dict,
) -> tuple[pd.DataFrame, int, bool]:
    """Fetch a (period, reporter, flow) chunk at CMD_CODE granularity.

    If the first call hits MAX_RECORDS exactly, the chunk is presumed
    truncated; the loader subdivides by HS chapter and concatenates.

    Returns:
        (dataframe, total_api_calls, was_subdivided)
    """
    df, n_calls = fetch_with_retries(
        period=period,
        reporter_code=reporter_code,
        flow_code=flow_code,
        cmd_code=CMD_CODE,
        budget=budget,
    )

    # If the response is below the cap, we're done.
    if len(df) < MAX_RECORDS:
        return df, n_calls, False

    logger.warning(
        "period=%s reporter=%s flow=%s hit MAX_RECORDS=%d -- "
        "subdividing by HS chapter.",
        period, reporter_code, flow_code, MAX_RECORDS,
    )

    pieces: list[pd.DataFrame] = []
    total_calls = n_calls
    # Only chapter-level subdivision when CMD_CODE was AG6.  For other
    # cmd_codes (e.g. a specific 6-digit), truncation is unexpected --
    # we just return what we got and flag truncated.
    if CMD_CODE != "AG6":
        return df, n_calls, True

    for chapter in HS_CHAPTERS:
        if remaining_budget(budget) <= 0:
            raise DailyQuotaExceeded(
                "Budget exhausted during chapter subdivision."
            )
        try:
            sub_df, sub_calls = fetch_with_retries(
                period=period,
                reporter_code=reporter_code,
                flow_code=flow_code,
                cmd_code=chapter,
                budget=budget,
            )
        except DailyQuotaExceeded:
            raise
        except Exception as exc:
            logger.warning(
                "Sub-chapter %s failed for period=%s reporter=%s flow=%s: %r",
                chapter, period, reporter_code, flow_code, exc,
            )
            total_calls += 1
            continue
        total_calls += sub_calls
        if not sub_df.empty:
            pieces.append(sub_df)
        time.sleep(SLEEP_SECONDS)

    combined = (
        pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    )
    return combined, total_calls, True


# ----------------------------------------------------------------------
# MASTER CSV APPEND
# ----------------------------------------------------------------------

def append_to_master(df: pd.DataFrame) -> None:
    """Append cleaned rows to the single master CSV (header only once)."""
    if df.empty:
        return
    header = not MASTER_CSV.exists()
    df.to_csv(MASTER_CSV, mode="a", header=header, index=False)


def remove_chunk_rows_from_master(chunk_id: str) -> int:
    """Remove rows tagged with a given chunk_id from the master CSV.

    Used when restarting a chunk whose previous run crashed mid-append.
    Returns the number of rows removed.
    """
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
    return create_engine(
        f"mysql+mysqlconnector://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}"
    )


def write_to_mysql(engine, df: pd.DataFrame) -> None:
    if engine is None or df.empty:
        return
    df.to_sql(
        "fact_trade_granular",
        con=engine,
        if_exists="append",
        index=False,
        chunksize=5000,
        method="multi",
    )


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
            "Missing COMTRADE_SUBSCRIPTION_KEY.  Put it in .env or export it."
        )

    budget = load_budget()
    manifest = load_manifest()
    engine = get_engine()

    jobs = plan_jobs()
    logger.info(
        "Planned %d chunks  |  cl=%s  freq=%s  cmd=%s  flows=%s  "
        "partner=%s  budget_left_today=%d/%d",
        len(jobs), CL_CODE, FREQ_CODE, CMD_CODE, FLOW_CODES,
        PARTNER_CODE if PARTNER_CODE else "ALL",
        remaining_budget(budget), MAX_DAILY_CALLS,
    )

    completed = sum(
        1 for j in jobs
        if (row := manifest_lookup(manifest, j.manifest_key())) is not None
        and row.get("status") == "success"
    )
    logger.info("%d/%d chunks already completed; resuming.",
                completed, len(jobs))

    for job in jobs:
        key = job.manifest_key()
        prior = manifest_lookup(manifest, key)
        if prior is not None and prior.get("status") == "success":
            continue

        # If the prior attempt crashed mid-append, clean its rows first.
        if prior is not None and prior.get("status") in {"in_progress",
                                                         "truncated_partial"}:
            stale_chunk_id = prior.get("chunk_id")
            if stale_chunk_id:
                removed = remove_chunk_rows_from_master(stale_chunk_id)
                if removed:
                    logger.info("Removed %d stale rows from master for "
                                "chunk_id=%s", removed, stale_chunk_id)

        if remaining_budget(budget) <= 0:
            logger.warning("Daily budget reached -- stopping.  "
                           "Resume tomorrow; manifest will pick up here.")
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

        logger.info("Fetching period=%s reporter=%s flow=%s cmd=%s",
                    job.period, job.reporter_code, job.flow_code, CMD_CODE)

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
                "manifest_key": key, "classification": CL_CODE,
                "frequency": FREQ_CODE, "cmd_code": CMD_CODE,
                "period": job.period, "reporter_code": job.reporter_code,
                "flow_code": job.flow_code,
                "partner_code": str(PARTNER_CODE) if PARTNER_CODE else "ALL",
                "status": "failed", "rows": "0",
                "n_api_calls": "0", "chunk_id": chunk_id,
                "error": f"DailyQuotaExceeded: {exc!r}"[:480],
            })
            break
        except Exception as exc:
            logger.exception("Chunk failed: period=%s reporter=%s flow=%s",
                             job.period, job.reporter_code, job.flow_code)
            manifest = upsert_manifest(manifest, {
                "manifest_key": key, "classification": CL_CODE,
                "frequency": FREQ_CODE, "cmd_code": CMD_CODE,
                "period": job.period, "reporter_code": job.reporter_code,
                "flow_code": job.flow_code,
                "partner_code": str(PARTNER_CODE) if PARTNER_CODE else "ALL",
                "status": "failed", "rows": "0",
                "n_api_calls": "0", "chunk_id": chunk_id,
                "error": repr(exc)[:480],
            })
            time.sleep(SLEEP_SECONDS)
            continue

        if df_raw is None or df_raw.empty:
            manifest = upsert_manifest(manifest, {
                "manifest_key": key, "classification": CL_CODE,
                "frequency": FREQ_CODE, "cmd_code": CMD_CODE,
                "period": job.period, "reporter_code": job.reporter_code,
                "flow_code": job.flow_code,
                "partner_code": str(PARTNER_CODE) if PARTNER_CODE else "ALL",
                "status": "empty", "rows": "0",
                "n_api_calls": str(n_calls),
                "chunk_id": chunk_id, "error": "",
            })
            time.sleep(SLEEP_SECONDS)
            continue

        cleaned = clean_df(df_raw, chunk_id=chunk_id)
        append_to_master(cleaned)
        write_to_mysql(engine, cleaned)

        manifest = upsert_manifest(manifest, {
            "manifest_key": key, "classification": CL_CODE,
            "frequency": FREQ_CODE, "cmd_code": CMD_CODE,
            "period": job.period, "reporter_code": job.reporter_code,
            "flow_code": job.flow_code,
            "partner_code": str(PARTNER_CODE) if PARTNER_CODE else "ALL",
            "status": "truncated" if subdivided and CMD_CODE != "AG6"
                      else "success",
            "rows": str(len(cleaned)),
            "n_api_calls": str(n_calls),
            "chunk_id": chunk_id,
            "error": "",
        })

        logger.info(
            "  -> %d rows  (calls=%d  budget_left=%d)",
            len(cleaned), n_calls, remaining_budget(budget),
        )

        time.sleep(SLEEP_SECONDS)

    logger.info("Run complete.  Calls used today: %d/%d.",
                budget.get("calls", 0), MAX_DAILY_CALLS)


if __name__ == "__main__":
    main()
