"""
comtrade_loader.py
==================
UN Comtrade Monthly Loader  –  HS (goods) + EBOPS (services)
Chunks API requests by commodity code and upserts into the
fact_trade_granular table (and all mapping dimension tables).

Dependencies
------------
    pip install sqlalchemy pymysql requests python-dotenv tqdm

Environment variables (or .env file)
-------------------------------------
    COMTRADE_API_KEY   – your UN Comtrade subscription key
    DB_URL             – SQLAlchemy URL, e.g.
                         mysql+pymysql://user:pass@host:3306/dbname
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from tqdm import tqdm

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / defaults  (override via environment or edit here)
# ---------------------------------------------------------------------------
BASE_URL = "https://comtradeapi.un.org/data/v1/get"

API_KEY = os.getenv("COMTRADE_API_KEY", "")
DB_URL  = os.getenv("DB_URL", "mysql+pymysql://root:password@localhost:3306/comtrade")

# Template parameters
GOODS_TYPE    = "C"          # commodities
SERVICES_TYPE = "S"          # services
FREQ_CODE     = "M"          # monthly
GOODS_CL      = "HS"
SERVICES_CL   = "EBOPS"

# Retry / rate-limit
MAX_RETRIES    = 5
RETRY_BACKOFF  = 2.0         # seconds, doubles each retry
REQUEST_DELAY  = 0.5         # seconds between successful calls

# Comtrade API hard limit per call
API_PAGE_SIZE  = 250_000     # rows; use None for API default

# ---------------------------------------------------------------------------
# Default fetch parameters  –  override per call via kwargs
# ---------------------------------------------------------------------------
DEFAULT_PARAMS: dict[str, Any] = {
    "reporterCode":  None,   # None = all reporters
    "partnerCode":   None,   # None = all partners
    "partner2Code":  None,
    "flowCode":      None,   # None = all flows
    "customsCode":   None,
    "motCode":       None,
    "aggregateBy":   None,
    "breakdownMode": None,
    "includeDesc":   True,
}

# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------
UPSERT_DIMENSION_SQL: dict[str, str] = {
    "frequency_mapping": """
        INSERT INTO frequency_mapping (freq_code, freq_desc)
        VALUES (:freq_code, :freq_desc)
        ON DUPLICATE KEY UPDATE freq_desc = VALUES(freq_desc)
    """,
    "tradeflow_mapping": """
        INSERT INTO tradeflow_mapping (flow_code, flow_desc)
        VALUES (:flow_code, :flow_desc)
        ON DUPLICATE KEY UPDATE flow_desc = VALUES(flow_desc)
    """,
    "transport_mapping": """
        INSERT INTO transport_mapping (mot_code, mot_desc)
        VALUES (:mot_code, :mot_desc)
        ON DUPLICATE KEY UPDATE mot_desc = VALUES(mot_desc)
    """,
    "country_mapping": """
        INSERT INTO country_mapping
            (country_code, country_text, iso_alpha_3, iso_alpha_2,
             reporter_note, is_group)
        VALUES
            (:country_code, :country_text, :iso_alpha_3, :iso_alpha_2,
             :reporter_note, :is_group)
        ON DUPLICATE KEY UPDATE
            country_text  = VALUES(country_text),
            iso_alpha_3   = VALUES(iso_alpha_3),
            iso_alpha_2   = VALUES(iso_alpha_2)
    """,
    "commodity_code_mapping": """
        INSERT INTO commodity_code_mapping
            (cmd_code, cmd_text, parent_code, is_leaf, aggr_level,
             standard_unit_abbr)
        VALUES
            (:cmd_code, :cmd_text, :parent_code, :is_leaf, :aggr_level,
             :standard_unit_abbr)
        ON DUPLICATE KEY UPDATE
            cmd_text   = VALUES(cmd_text),
            is_leaf    = VALUES(is_leaf),
            aggr_level = VALUES(aggr_level)
    """,
    "unit_quantity_mapping": """
        INSERT INTO unit_quantity_mapping
            (qty_code, qty_abbr, qty_description)
        VALUES
            (:qty_code, :qty_abbr, :qty_description)
        ON DUPLICATE KEY UPDATE
            qty_abbr        = VALUES(qty_abbr),
            qty_description = VALUES(qty_description)
    """,
}

UPSERT_FACT_SQL = """
    INSERT INTO fact_trade_granular (
        chunk_id, loaded_at_utc,
        dataset_code, type_code, freq_code,
        ref_period_id, ref_year, ref_month, period,
        reporter_code, reporter_iso, reporter_desc,
        flow_code, flow_desc,
        partner_code, partner_iso, partner_desc,
        partner2_code, partner2_iso, partner2_desc,
        classification_code, classification_search_code,
        is_original_classification,
        cmd_code, cmd_desc, aggr_level, is_leaf,
        customs_code, customs_desc,
        mot_code, mot_desc,
        qty_unit_code, qty_unit_abbr, qty, is_qty_estimated,
        alt_qty_unit_code, alt_qty_unit_abbr, alt_qty, is_alt_qty_estimated,
        net_weight, is_net_weight_estimated,
        gross_weight, is_gross_weight_estimated,
        cif_value_usd, fob_value_usd, primary_value_usd,
        legacy_estimation_flag, is_reported, is_aggregate
    ) VALUES (
        :chunk_id, :loaded_at_utc,
        :dataset_code, :type_code, :freq_code,
        :ref_period_id, :ref_year, :ref_month, :period,
        :reporter_code, :reporter_iso, :reporter_desc,
        :flow_code, :flow_desc,
        :partner_code, :partner_iso, :partner_desc,
        :partner2_code, :partner2_iso, :partner2_desc,
        :classification_code, :classification_search_code,
        :is_original_classification,
        :cmd_code, :cmd_desc, :aggr_level, :is_leaf,
        :customs_code, :customs_desc,
        :mot_code, :mot_desc,
        :qty_unit_code, :qty_unit_abbr, :qty, :is_qty_estimated,
        :alt_qty_unit_code, :alt_qty_unit_abbr, :alt_qty, :is_alt_qty_estimated,
        :net_weight, :is_net_weight_estimated,
        :gross_weight, :is_gross_weight_estimated,
        :cif_value_usd, :fob_value_usd, :primary_value_usd,
        :legacy_estimation_flag, :is_reported, :is_aggregate
    )
    ON DUPLICATE KEY UPDATE
        chunk_id              = VALUES(chunk_id),
        loaded_at_utc         = VALUES(loaded_at_utc),
        primary_value_usd     = VALUES(primary_value_usd),
        cif_value_usd         = VALUES(cif_value_usd),
        fob_value_usd         = VALUES(fob_value_usd),
        qty                   = VALUES(qty),
        net_weight            = VALUES(net_weight),
        gross_weight          = VALUES(gross_weight),
        is_reported           = VALUES(is_reported)
"""

UPSERT_MANIFEST_SQL = """
    INSERT INTO load_manifest
        (manifest_key, classification, frequency, cmd_code,
         period, reporter_code, flow_code, partner_code,
         status, rows_loaded, n_api_calls, chunk_id, error, updated_at)
    VALUES
        (:manifest_key, :classification, :frequency, :cmd_code,
         :period, :reporter_code, :flow_code, :partner_code,
         :status, :rows_loaded, :n_api_calls, :chunk_id, :error, :updated_at)
    ON DUPLICATE KEY UPDATE
        status      = VALUES(status),
        rows_loaded = VALUES(rows_loaded),
        n_api_calls = VALUES(n_api_calls),
        chunk_id    = VALUES(chunk_id),
        error       = VALUES(error),
        updated_at  = VALUES(updated_at)
"""

# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

def _build_url(type_code: str, freq_code: str, cl_code: str) -> str:
    return f"{BASE_URL}/{type_code}/{freq_code}/{cl_code}"


def _call_api(
    url: str,
    params: dict[str, Any],
    api_key: str = API_KEY,
) -> dict:
    """GET with retry / exponential back-off."""
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    clean_params = {k: v for k, v in params.items() if v is not None}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=clean_params, timeout=60)
            if resp.status_code == 429:
                wait = RETRY_BACKOFF * (2 ** attempt)
                log.warning("Rate limited – sleeping %.0fs (attempt %d)", wait, attempt)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp.json()
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF * attempt
            log.warning("Request error %s – retry %d/%d in %.0fs", exc, attempt, MAX_RETRIES, wait)
            time.sleep(wait)

    raise RuntimeError(f"All {MAX_RETRIES} retries failed for {url}")


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------

def _str(v: Any) -> str | None:
    return str(v).strip() if v is not None else None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(bool(v))
    if isinstance(v, str):
        return 1 if v.lower() in ("true", "1", "yes") else 0
    return None


def normalise_row(row: dict, chunk_id: str, loaded_at: datetime) -> dict:
    period = _str(row.get("period") or row.get("refPeriodId") or row.get("refYear"))
    ref_year  = _int(row.get("refYear"))
    ref_month = _int(row.get("refMonth"))

    # Build period string YYYYMM if missing but year+month present
    if not period and ref_year and ref_month:
        period = f"{ref_year}{ref_month:02d}"

    return {
        "chunk_id":       chunk_id,
        "loaded_at_utc":  loaded_at,
        # dataset
        "dataset_code":   _str(row.get("datasetCode") or row.get("typeCode")),
        "type_code":      _str(row.get("typeCode")),
        "freq_code":      _str(row.get("freqCode")),
        # period
        "ref_period_id":  _int(row.get("refPeriodId")),
        "ref_year":       ref_year,
        "ref_month":      ref_month,
        "period":         period or "0",
        # reporter
        "reporter_code":  _str(row.get("reporterCode")) or "0",
        "reporter_iso":   _str(row.get("reporterISO")),
        "reporter_desc":  _str(row.get("reporterDesc")),
        # flow
        "flow_code":      _str(row.get("flowCode")) or "0",
        "flow_desc":      _str(row.get("flowDesc")),
        # partner
        "partner_code":   _str(row.get("partnerCode")) or "0",
        "partner_iso":    _str(row.get("partnerISO")),
        "partner_desc":   _str(row.get("partnerDesc")),
        "partner2_code":  _str(row.get("partner2Code")) or "0",
        "partner2_iso":   _str(row.get("partner2ISO")),
        "partner2_desc":  _str(row.get("partner2Desc")),
        # classification
        "classification_code":        _str(row.get("classificationCode")),
        "classification_search_code": _str(row.get("classificationSearchCode")),
        "is_original_classification": _bool(row.get("isOriginalClassification")),
        # commodity
        "cmd_code":    _str(row.get("cmdCode")) or "TOTAL",
        "cmd_desc":    _str(row.get("cmdDesc")),
        "aggr_level":  _int(row.get("aggrLevel")),
        "is_leaf":     _bool(row.get("isLeaf")),
        # customs / mot
        "customs_code": _str(row.get("customsCode")) or "C00",
        "customs_desc": _str(row.get("customsDesc")),
        "mot_code":     _str(row.get("motCode")) or "0",
        "mot_desc":     _str(row.get("motDesc")),
        # quantities
        "qty_unit_code":       _str(row.get("qtyUnitCode")),
        "qty_unit_abbr":       _str(row.get("qtyUnitAbbr")),
        "qty":                 _float(row.get("qty")),
        "is_qty_estimated":    _bool(row.get("isQtyEstimated")),
        "alt_qty_unit_code":   _str(row.get("altQtyUnitCode")),
        "alt_qty_unit_abbr":   _str(row.get("altQtyUnitAbbr")),
        "alt_qty":             _float(row.get("altQty")),
        "is_alt_qty_estimated":_bool(row.get("isAltQtyEstimated")),
        "net_weight":               _float(row.get("netWgt")),
        "is_net_weight_estimated":  _bool(row.get("isNetWgtEstimated")),
        "gross_weight":             _float(row.get("grossWgt")),
        "is_gross_weight_estimated":_bool(row.get("isGrossWgtEstimated")),
        # values
        "cif_value_usd":     _float(row.get("cifvalue")),
        "fob_value_usd":     _float(row.get("fobvalue")),
        "primary_value_usd": _float(row.get("primaryValue")),
        # flags
        "legacy_estimation_flag": _int(row.get("legacyEstimationFlag")),
        "is_reported":  _bool(row.get("isReported")),
        "is_aggregate": _bool(row.get("isAggregate")),
    }


# ---------------------------------------------------------------------------
# Dimension seeding  (called once per API response)
# ---------------------------------------------------------------------------

def _seed_dimensions(conn, rows: list[dict]) -> None:
    """Extract unique dimension values from raw rows and upsert them."""
    freq_seen, flow_seen, mot_seen = set(), set(), set()
    country_seen, cmd_seen, unit_seen = set(), set(), set()

    for r in rows:
        # frequency
        fc = _str(r.get("freqCode"))
        fd = _str(r.get("freqDesc") or fc)
        if fc and fc not in freq_seen:
            conn.execute(text(UPSERT_DIMENSION_SQL["frequency_mapping"]),
                         {"freq_code": fc, "freq_desc": fd or fc})
            freq_seen.add(fc)

        # flow
        fl = _str(r.get("flowCode"))
        fld = _str(r.get("flowDesc") or fl)
        if fl and fl not in flow_seen:
            conn.execute(text(UPSERT_DIMENSION_SQL["tradeflow_mapping"]),
                         {"flow_code": fl, "flow_desc": fld or fl})
            flow_seen.add(fl)

        # mot
        mot = _str(r.get("motCode")) or "0"
        motd = _str(r.get("motDesc") or mot)
        if mot not in mot_seen:
            conn.execute(text(UPSERT_DIMENSION_SQL["transport_mapping"]),
                         {"mot_code": mot, "mot_desc": motd or mot})
            mot_seen.add(mot)

        # countries: reporter, partner, partner2
        for code_key, iso_key, desc_key in [
            ("reporterCode", "reporterISO", "reporterDesc"),
            ("partnerCode",  "partnerISO",  "partnerDesc"),
            ("partner2Code", "partner2ISO", "partner2Desc"),
        ]:
            cc = _str(r.get(code_key)) or "0"
            if cc not in country_seen:
                conn.execute(text(UPSERT_DIMENSION_SQL["country_mapping"]), {
                    "country_code":  cc,
                    "country_text":  _str(r.get(desc_key)) or cc,
                    "iso_alpha_3":   _str(r.get(iso_key)),
                    "iso_alpha_2":   None,
                    "reporter_note": None,
                    "is_group":      0,
                })
                country_seen.add(cc)

        # commodity
        cmd = _str(r.get("cmdCode")) or "TOTAL"
        if cmd not in cmd_seen:
            conn.execute(text(UPSERT_DIMENSION_SQL["commodity_code_mapping"]), {
                "cmd_code":           cmd,
                "cmd_text":           _str(r.get("cmdDesc")) or cmd,
                "parent_code":        None,
                "is_leaf":            _bool(r.get("isLeaf")) or 0,
                "aggr_level":         _int(r.get("aggrLevel")) or 0,
                "standard_unit_abbr": _str(r.get("qtyUnitAbbr")),
            })
            cmd_seen.add(cmd)

        # unit quantities
        for code_key, abbr_key in [
            ("qtyUnitCode", "qtyUnitAbbr"),
            ("altQtyUnitCode", "altQtyUnitAbbr"),
        ]:
            qc = _str(r.get(code_key))
            qa = _str(r.get(abbr_key))
            if qc and qc not in unit_seen:
                conn.execute(text(UPSERT_DIMENSION_SQL["unit_quantity_mapping"]), {
                    "qty_code":        qc,
                    "qty_abbr":        qa or qc,
                    "qty_description": qa or qc,
                })
                unit_seen.add(qc)


# ---------------------------------------------------------------------------
# Core fetch + store function
# ---------------------------------------------------------------------------

def fetch_and_store(
    engine,
    type_code: str,
    cl_code: str,
    cmd_codes: list[str],
    periods: list[str],
    extra_params: dict | None = None,
) -> dict[str, int]:
    """
    Fetch Comtrade data chunked by commodity code, store to DB.

    Returns summary dict: {manifest_key: rows_loaded}
    """
    url = _build_url(type_code, FREQ_CODE, cl_code)
    params = {**DEFAULT_PARAMS, **(extra_params or {})}
    summary: dict[str, int] = {}
    loaded_at = datetime.now(timezone.utc).replace(tzinfo=None)

    for period_str in tqdm(periods, desc=f"{type_code}/{cl_code} periods"):
        params["period"] = period_str

        for cmd in tqdm(cmd_codes, desc=f"  cmds @ {period_str}", leave=False):
            params["cmdCode"] = cmd

            # Manifest key uniquely identifies this chunk
            mkey = f"{type_code}_{cl_code}_{FREQ_CODE}_{period_str}_{cmd}"
            chunk_id = hashlib.md5(mkey.encode()).hexdigest()
            n_api_calls = 0
            rows_loaded = 0
            error_msg: str | None = None

            try:
                data = _call_api(url, params)
                n_api_calls += 1
                raw_rows: list[dict] = data.get("data", []) or []

                if not raw_rows:
                    log.debug("No data for %s", mkey)
                else:
                    with engine.begin() as conn:
                        _seed_dimensions(conn, raw_rows)

                        for row in raw_rows:
                            norm = normalise_row(row, chunk_id, loaded_at)
                            try:
                                conn.execute(text(UPSERT_FACT_SQL), norm)
                                rows_loaded += 1
                            except IntegrityError as ie:
                                log.warning("FK/integrity skip row: %s", ie.orig)

                status = "success"

            except Exception as exc:
                error_msg = str(exc)[:490]
                status = "error"
                log.error("Failed %s: %s", mkey, error_msg)

            # Write manifest entry
            with engine.begin() as conn:
                conn.execute(text(UPSERT_MANIFEST_SQL), {
                    "manifest_key":  mkey,
                    "classification": cl_code,
                    "frequency":      FREQ_CODE,
                    "cmd_code":       cmd,
                    "period":         period_str,
                    "reporter_code":  _str(params.get("reporterCode")),
                    "flow_code":      _str(params.get("flowCode")),
                    "partner_code":   _str(params.get("partnerCode")),
                    "status":         status,
                    "rows_loaded":    rows_loaded,
                    "n_api_calls":    n_api_calls,
                    "chunk_id":       chunk_id,
                    "error":          error_msg,
                    "updated_at":     loaded_at,
                })

            summary[mkey] = rows_loaded

    return summary


# ---------------------------------------------------------------------------
# Reference-data helpers  (seed mapping tables from Comtrade reference API)
# ---------------------------------------------------------------------------

REFERENCE_BASE = "https://comtradeapi.un.org/files/v1/app/reference"

def seed_reference_data(engine, api_key: str = API_KEY) -> None:
    """
    Pull Comtrade reference lists (reporters, partners, HS codes, units,
    flows, transport modes) and populate dimension tables.
    """
    endpoints = {
        "reporterAreas":  "https://comtradeapi.un.org/files/v1/app/reference/Reporters.json",
        "partnerAreas":   "https://comtradeapi.un.org/files/v1/app/reference/partnerAreas.json",
        "HSCodes":        "https://comtradeapi.un.org/files/v1/app/reference/HS.json",
        "EBOPSCodes":     "https://comtradeapi.un.org/files/v1/app/reference/EBOPS2010.json",
        "tradeFlows":     "https://comtradeapi.un.org/files/v1/app/reference/tradeFlows.json",
        "modeOfTransport":"https://comtradeapi.un.org/files/v1/app/reference/modeOfTransport.json",
        "units":          "https://comtradeapi.un.org/files/v1/app/reference/unitOfQty.json",
    }

    headers = {"Ocp-Apim-Subscription-Key": api_key}

    for name, url in endpoints.items():
        log.info("Seeding reference: %s", name)
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            items = resp.json().get("results", resp.json())
        except Exception as exc:
            log.warning("Could not fetch %s: %s", name, exc)
            continue

        with engine.begin() as conn:
            if name in ("reporterAreas", "partnerAreas"):
                for item in items:
                    country_code = _str(item.get("id") or item.get("reporterCode"))
                    if not country_code:
                        log.debug("Skipping %s entry with null code: %s", name, item)
                        continue
                    conn.execute(text(UPSERT_DIMENSION_SQL["country_mapping"]), {
                        "country_code":  country_code,
                        "country_text":  _str(item.get("text") or item.get("reporterDesc")) or country_code,
                        "iso_alpha_3":   _str(item.get("iso3")),
                        "iso_alpha_2":   _str(item.get("iso2")),
                        "reporter_note": _str(item.get("note")),
                        "is_group":      _bool(item.get("isGroup")) or 0,
                    })

            elif name in ("HSCodes", "EBOPSCodes"):
                # Disable FK checks: parent codes may arrive out of order
                # or contain sentinels like '#'
                conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
                for item in items:
                    cmd_code = _str(item.get("id") or item.get("code"))
                    if not cmd_code:
                        log.debug("Skipping %s entry with null code: %s", name, item)
                        continue
                    raw_parent = _str(item.get("parent"))
                    # Treat '#', '-', 'n/a' etc. as no parent
                    parent_code = (
                        raw_parent
                        if raw_parent and raw_parent.replace(" ", "").isalnum()
                        else None
                    )
                    conn.execute(text(UPSERT_DIMENSION_SQL["commodity_code_mapping"]), {
                        "cmd_code":           cmd_code,
                        "cmd_text":           _str(item.get("text") or item.get("description")) or cmd_code,
                        "parent_code":        parent_code,
                        "is_leaf":            _bool(item.get("isLeaf")) or 0,
                        "aggr_level":         _int(item.get("aggrLevel") or item.get("level")) or 0,
                        "standard_unit_abbr": _str(item.get("standardUnitAbbr")),
                    })
                conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

            elif name == "tradeFlows":
                for item in items:
                    flow_code = _str(item.get("id") or item.get("flowCode"))
                    if not flow_code:
                        continue
                    conn.execute(text(UPSERT_DIMENSION_SQL["tradeflow_mapping"]), {
                        "flow_code": flow_code,
                        "flow_desc": _str(item.get("text") or item.get("flowDesc")) or flow_code,
                    })

            elif name == "modeOfTransport":
                for item in items:
                    mot_code = _str(item.get("id") or item.get("motCode"))
                    if not mot_code:
                        continue
                    conn.execute(text(UPSERT_DIMENSION_SQL["transport_mapping"]), {
                        "mot_code": mot_code,
                        "mot_desc": _str(item.get("text") or item.get("motDesc")) or mot_code,
                    })

            elif name == "units":
                for item in items:
                    qty_code = _str(item.get("id") or item.get("qtyCode"))
                    if not qty_code:
                        continue
                    conn.execute(text(UPSERT_DIMENSION_SQL["unit_quantity_mapping"]), {
                        "qty_code":        qty_code,
                        "qty_abbr":        _str(item.get("abbreviation") or item.get("qtyAbbr")) or qty_code,
                        "qty_description": _str(item.get("text") or item.get("description")) or qty_code,
                    })

    log.info("Reference seeding complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="UN Comtrade monthly loader")
    parser.add_argument("--periods",      nargs="+", required=True,
                        help="One or more YYYYMM strings, e.g. 202401 202402")
    parser.add_argument("--cmd-codes",    nargs="+", default=["TOTAL"],
                        help="HS commodity codes to fetch, e.g. 01 02 27")
    parser.add_argument("--reporter",     default=None,
                        help="Reporter M49 code(s), comma-separated")
    parser.add_argument("--partner",      default=None,
                        help="Partner M49 code(s), comma-separated")
    parser.add_argument("--flow",         default=None,
                        help="Flow code(s), e.g. X,M")
    parser.add_argument("--seed-refs",    action="store_true",
                        help="Seed all reference/dimension tables first")
    parser.add_argument("--goods-only",   action="store_true")
    parser.add_argument("--services-only",action="store_true")
    args = parser.parse_args()

    if not API_KEY:
        raise SystemExit("ERROR: set COMTRADE_API_KEY environment variable.")

    engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=3600)

    if args.seed_refs:
        seed_reference_data(engine)

    extra = {
        "reporterCode": args.reporter,
        "partnerCode":  args.partner,
        "flowCode":     args.flow,
    }

    total_rows = 0

    if not args.services_only:
        log.info("=== Fetching GOODS (HS) ===")
        result = fetch_and_store(
            engine,
            type_code=GOODS_TYPE,
            cl_code=GOODS_CL,
            cmd_codes=args.cmd_codes,
            periods=args.periods,
            extra_params=extra,
        )
        total_rows += sum(result.values())
        log.info("Goods rows loaded: %d", sum(result.values()))

    if not args.goods_only:
        log.info("=== Fetching SERVICES (EBOPS) ===")
        result = fetch_and_store(
            engine,
            type_code=SERVICES_TYPE,
            cl_code=SERVICES_CL,
            cmd_codes=args.cmd_codes,
            periods=args.periods,
            extra_params=extra,
        )
        total_rows += sum(result.values())
        log.info("Services rows loaded: %d", sum(result.values()))

    log.info("=== Done. Total rows loaded: %d ===", total_rows)