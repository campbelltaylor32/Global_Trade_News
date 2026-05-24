"""Data access layer.

Single source of truth for getting trade data into pandas. Order of preference:

1. MySQL via SQLAlchemy + PyMySQL. Connection details come from (in order):
     a. .streamlit/secrets.toml under [db] (preferred for Streamlit)
     b. Individual env vars: DB_USER, DB_PASS, DB_HOST, DB_PORT, DB_NAME
     c. A single TRADE_DB_URL env var (full SQLAlchemy URL, overrides a–b)
2. Local Parquet/CSV cache at `data/trade_cached.parquet` if present.
3. A synthetic dataset generated on demand (so the app runs out of the box).

All queries return tidy pandas DataFrames with a consistent schema:

    columns:
        ref_year (int)
        reporter_iso (str, 3-letter)    reporter_desc (str)
        partner_iso  (str, 3-letter)    partner_desc  (str)
        flow_code    ('M' | 'X')        flow_desc     ('Import' | 'Export')
        cmd_code     (str)              cmd_desc      (str)
        primary_value_usd (float)
        net_weight        (float, nullable)
        latitude / longitude (float, partner geo)

We always filter out partner_iso == 'W00' (World aggregate) for bilateral
analyses and cmd_code == 'TOTAL' for commodity analyses — but expose helpers
that return them when "country totals" are wanted.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import streamlit as st


# ---------------------------------------------------------------------------
# Connection — MySQL (port 3306). Build the SQLAlchemy URL from whichever
# config source is present. Password is URL-encoded so special chars are safe.
# ---------------------------------------------------------------------------
from urllib.parse import quote_plus


def _build_mysql_url(user: str, password: str, host: str,
                     port: str | int, dbname: str) -> str:
    return (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{dbname}"
    )


def _get_db_url() -> Optional[str]:
    """Resolve the SQLAlchemy URL. Order: secrets.toml → individual env vars
    → TRADE_DB_URL → None."""
    # 1. Streamlit secrets — preferred. Supports either a full url= or the
    #    decomposed user/pass/host/port/name fields.
    try:
        if "db" in st.secrets:
            cfg = st.secrets["db"]
            if "url" in cfg:
                return cfg["url"]
            required = ("user", "password", "host", "port", "name")
            if all(k in cfg for k in required):
                return _build_mysql_url(
                    cfg["user"], cfg["password"],
                    cfg["host"], cfg["port"], cfg["name"],
                )
    except Exception:
        pass

    # 2. Individual env vars (these match the .env file shape)
    env = os.environ
    if all(env.get(k) for k in ("DB_USER", "DB_PASS", "DB_HOST", "DB_PORT", "DB_NAME")):
        return _build_mysql_url(
            env["DB_USER"], env["DB_PASS"],
            env["DB_HOST"], env["DB_PORT"], env["DB_NAME"],
        )

    # 3. Full URL override
    return env.get("TRADE_DB_URL")


@st.cache_resource(show_spinner=False)
def _engine():
    url = _get_db_url()
    if not url:
        return None
    from sqlalchemy import create_engine
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600)


# ---------------------------------------------------------------------------
# Main query — pulls the granular fact joined to geo + qty dimensions.
# This mirrors the schema you shared:
#   fact_trade_granular_v2 v
#   JOIN country_geo g ON g.iso_alpha_3 = v.partner_iso
#   JOIN unit_quantity_mapping q ON q.qty_code = v.qty_unit_code
# ---------------------------------------------------------------------------
BASE_SQL = """
SELECT
    v.ref_year,
    v.reporter_iso,
    v.reporter_desc,
    v.partner_iso,
    v.partner_desc,
    v.flow_code,
    v.flow_desc,
    v.cmd_code,
    v.cmd_desc,
    v.primary_value_usd,
    v.net_weight,
    g.latitude   AS partner_lat,
    g.longitude  AS partner_lon,
    q.qty_abbr,
    q.qty_description
FROM fact_trade_granular_v2 v
JOIN country_geo g
    ON g.iso_alpha_3 = v.partner_iso
LEFT JOIN unit_quantity_mapping q
    ON q.qty_code = v.qty_unit_code
WHERE v.ref_year BETWEEN :year_from AND :year_to
  AND v.primary_value_usd IS NOT NULL
  AND v.primary_value_usd > 0
"""


@st.cache_data(ttl=3600, show_spinner="Loading trade data…")
def load_trade(year_from: int = 2020, year_to: int = 2024) -> pd.DataFrame:
    """Load the master trade DataFrame from DB, cached parquet, or synthetic."""
    eng = _engine()
    if eng is not None:
        try:
            from sqlalchemy import text
            with eng.connect() as conn:
                df = pd.read_sql(
                    text(BASE_SQL), conn,
                    params={"year_from": year_from, "year_to": year_to},
                )
            return _post_process(df)
        except Exception as e:
            st.warning(f"DB query failed, falling back to cached/synthetic data. ({e})")

    # Local cache?
    cache_path = Path(__file__).resolve().parent.parent / "data" / "trade_cached.parquet"
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        return _post_process(df[(df["ref_year"] >= year_from) & (df["ref_year"] <= year_to)])

    # Last resort: synthetic
    return _post_process(_synthesize(year_from, year_to))


def _post_process(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Normalize types
    df["ref_year"] = df["ref_year"].astype(int)
    df["primary_value_usd"] = pd.to_numeric(df["primary_value_usd"], errors="coerce")
    df = df.dropna(subset=["primary_value_usd", "reporter_iso", "partner_iso"])
    # Flow direction text
    df["flow_desc"] = df["flow_desc"].fillna(df["flow_code"].map({"M": "Import", "X": "Export"}))
    return df


# ---------------------------------------------------------------------------
# Helpers that return filtered slices
# ---------------------------------------------------------------------------
def bilateral(df: pd.DataFrame) -> pd.DataFrame:
    """Exclude 'World' aggregate partners — true bilateral rows only."""
    return df[(df["partner_iso"] != "W00") & (df["partner_iso"] != "WLD")]


def country_totals(df: pd.DataFrame) -> pd.DataFrame:
    """Just the rows where partner == World (= reporter's total trade)."""
    return df[df["partner_iso"].isin(["W00", "WLD"])]


def by_commodity(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the 'TOTAL' aggregate commodity code, keep real HS chapters."""
    return df[(df["cmd_code"].astype(str) != "TOTAL") & (df["cmd_code"].notna())]


# ---------------------------------------------------------------------------
# Synthetic data — only runs when no DB and no cache. Produces a realistic
# shape: ~30 countries, 12 HS chapters, both flows, 5 years.
# ---------------------------------------------------------------------------
COUNTRIES = [
    # iso3, name, lat, lon
    ("USA", "United States",  38.0,  -97.0),
    ("CHN", "China",          35.0,  103.0),
    ("DEU", "Germany",        51.0,   10.0),
    ("JPN", "Japan",          36.0,  138.0),
    ("KOR", "Korea, Rep.",    36.0,  128.0),
    ("GBR", "United Kingdom", 54.0,   -2.0),
    ("FRA", "France",         46.0,    2.0),
    ("ITA", "Italy",          42.0,   12.0),
    ("CAN", "Canada",         56.0, -106.0),
    ("MEX", "Mexico",         23.0, -102.0),
    ("BRA", "Brazil",        -14.0,  -51.0),
    ("IND", "India",          21.0,   78.0),
    ("VNM", "Viet Nam",       16.0,  108.0),
    ("THA", "Thailand",       15.0,  101.0),
    ("IDN", "Indonesia",      -2.0,  118.0),
    ("MYS", "Malaysia",        4.0,  102.0),
    ("SGP", "Singapore",       1.4,  103.8),
    ("AUS", "Australia",     -25.0,  133.0),
    ("ZAF", "South Africa",  -30.0,   25.0),
    ("NGA", "Nigeria",         9.0,    8.0),
    ("EGY", "Egypt",          26.0,   30.0),
    ("TUR", "Turkiye",        38.0,   35.0),
    ("SAU", "Saudi Arabia",   23.0,   45.0),
    ("ARE", "UAE",            23.4,   53.8),
    ("RUS", "Russia",         61.0,  105.0),
    ("POL", "Poland",         51.9,   19.1),
    ("NLD", "Netherlands",    52.1,    5.3),
    ("ESP", "Spain",          40.5,   -3.7),
    ("CHE", "Switzerland",    46.8,    8.2),
    ("IRL", "Ireland",        53.1,   -7.7),
]

COMMODITIES = [
    ("85", "Electrical machinery & electronics"),
    ("84", "Nuclear reactors, boilers, machinery"),
    ("87", "Vehicles other than railway"),
    ("27", "Mineral fuels & oils"),
    ("30", "Pharmaceutical products"),
    ("90", "Optical & medical instruments"),
    ("71", "Pearls, precious stones, metals"),
    ("39", "Plastics & articles"),
    ("72", "Iron & steel"),
    ("10", "Cereals"),
    ("29", "Organic chemicals"),
    ("62", "Apparel, not knitted"),
]


def _synthesize(year_from: int, year_to: int) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    rows = []
    # Each country has a "scale" reflecting its trade size.
    scale = {iso: rng.lognormal(mean=24.5, sigma=1.2) for iso, *_ in COUNTRIES}
    # Each commodity has its own scale.
    cmd_scale = {c: rng.lognormal(mean=0.0, sigma=0.6) for c, _ in COMMODITIES}
    # Pairwise affinity (proximity, alliance, etc.)
    iso_list = [c[0] for c in COUNTRIES]
    affinity = {}
    for a in iso_list:
        for b in iso_list:
            affinity[(a, b)] = rng.uniform(0.05, 1.0) if a != b else 0.0

    for year in range(year_from, year_to + 1):
        # Modest secular growth + per-year shock
        global_growth = 1.0 + 0.04 * (year - year_from) + rng.normal(0, 0.02)
        for r_iso, r_name, _, _ in COUNTRIES:
            for p_iso, p_name, p_lat, p_lon in COUNTRIES:
                if r_iso == p_iso:
                    continue
                # Decide if this corridor exists this year (sparser is more realistic)
                if rng.random() < 0.35:
                    continue
                for cmd_code, cmd_desc in COMMODITIES:
                    if rng.random() < 0.45:  # not every commodity flows every corridor
                        continue
                    base = scale[r_iso] * affinity[(r_iso, p_iso)] * cmd_scale[cmd_code]
                    value = base * global_growth * rng.lognormal(mean=0, sigma=0.7)
                    # Export row
                    rows.append({
                        "ref_year": year,
                        "reporter_iso": r_iso, "reporter_desc": r_name,
                        "partner_iso":  p_iso, "partner_desc":  p_name,
                        "flow_code": "X", "flow_desc": "Export",
                        "cmd_code": cmd_code, "cmd_desc": cmd_desc,
                        "primary_value_usd": float(value),
                        "net_weight": float(value / rng.uniform(800, 3000)),
                        "partner_lat": p_lat, "partner_lon": p_lon,
                        "qty_abbr": "kg", "qty_description": "Kilograms",
                    })
                    # Symmetric import (the partner reports importing from reporter)
                    # Add Comtrade-style mirror asymmetry
                    rows.append({
                        "ref_year": year,
                        "reporter_iso": p_iso, "reporter_desc": p_name,
                        "partner_iso":  r_iso, "partner_desc":  r_name,
                        "flow_code": "M", "flow_desc": "Import",
                        "cmd_code": cmd_code, "cmd_desc": cmd_desc,
                        "primary_value_usd": float(value * rng.uniform(0.85, 1.15)),
                        "net_weight": float(value / rng.uniform(800, 3000)),
                        "partner_lat": next(c[2] for c in COUNTRIES if c[0] == r_iso),
                        "partner_lon": next(c[3] for c in COUNTRIES if c[0] == r_iso),
                        "qty_abbr": "kg", "qty_description": "Kilograms",
                    })

    df = pd.DataFrame(rows)
    return df


# ---------------------------------------------------------------------------
# Convenience accessors used by pages
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600)
def list_countries(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df[["reporter_iso", "reporter_desc"]]
        .drop_duplicates()
        .rename(columns={"reporter_iso": "iso", "reporter_desc": "name"})
        .sort_values("name")
        .reset_index(drop=True)
    )


@st.cache_data(ttl=3600)
def list_commodities(df: pd.DataFrame) -> pd.DataFrame:
    return (
        by_commodity(df)[["cmd_code", "cmd_desc"]]
        .drop_duplicates()
        .sort_values("cmd_code")
        .reset_index(drop=True)
    )


@st.cache_data(ttl=3600)
def year_range(df: pd.DataFrame) -> tuple[int, int]:
    return int(df["ref_year"].min()), int(df["ref_year"].max())
