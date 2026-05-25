"""Data access layer.

Source preference (per dataset):
1. MySQL via SQLAlchemy + PyMySQL. Connection details:
     a. .streamlit/secrets.toml [db]                    (preferred)
     b. Env vars: DB_USER, DB_PASS, DB_HOST, DB_PORT, DB_NAME
     c. Full TRADE_DB_URL env var                       (override)
2. Local Parquet/CSV cache (if present)
3. A synthetic dataset built on demand (so the app runs offline).

Two facts are exposed:
- `load_trade()`  — fact_trade_granular_v2 joined to country_geo & qty dim
- `load_news()`   — news_articles (joinable to trade on cmd_code)

Both default to a 2023-2025 window — the years currently available in the
project's warehouse.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import streamlit as st


# Default year window — change in one place and every page picks it up
DEFAULT_YEAR_FROM = 2023
DEFAULT_YEAR_TO   = 2025
AVAILABLE_YEARS   = list(range(DEFAULT_YEAR_FROM, DEFAULT_YEAR_TO + 1))


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def _build_mysql_url(user: str, password: str, host: str,
                     port: str | int, dbname: str) -> str:
    return (
        f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{dbname}"
    )


def _get_db_url() -> Optional[str]:
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

    env = os.environ
    if all(env.get(k) for k in ("DB_USER", "DB_PASS", "DB_HOST", "DB_PORT", "DB_NAME")):
        return _build_mysql_url(
            env["DB_USER"], env["DB_PASS"],
            env["DB_HOST"], env["DB_PORT"], env["DB_NAME"],
        )
    return env.get("TRADE_DB_URL")


@st.cache_resource(show_spinner=False)
def _engine():
    url = _get_db_url()
    if not url:
        return None
    try:
        from sqlalchemy import create_engine
        return create_engine(url, pool_pre_ping=True, pool_recycle=3600)
    except ModuleNotFoundError as e:
        st.warning(
            f"Database driver `{e.name}` is not installed. "
            f"Run `pip install -r requirements.txt`. Falling back to synthetic data."
        )
        return None
    except Exception as e:
        st.warning(f"Could not create DB engine ({e}). Falling back to synthetic data.")
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _norm_cmd_code(x) -> str | None:
    """Canonical HS-code string. Strips leading zeros so '01' and 1 both → '1'."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip()
    if s == "" or s.lower() == "nan":
        return None
    s = s.lstrip("0") or "0"
    return s


# ---------------------------------------------------------------------------
# Trade fact
# ---------------------------------------------------------------------------
TRADE_SQL = """
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
def load_trade(year_from: int = DEFAULT_YEAR_FROM,
               year_to: int = DEFAULT_YEAR_TO) -> pd.DataFrame:
    """Load the master trade DataFrame from DB, cached parquet, or synthetic."""
    eng = _engine()
    if eng is not None:
        try:
            from sqlalchemy import text
            with eng.connect() as conn:
                df = pd.read_sql(
                    text(TRADE_SQL), conn,
                    params={"year_from": year_from, "year_to": year_to},
                )
            return _post_process_trade(df)
        except Exception as e:
            st.warning(f"Trade DB query failed: {e}. Falling back.")

    cache_path = Path(__file__).resolve().parent.parent / "data" / "trade_cached.parquet"
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        return _post_process_trade(
            df[(df["ref_year"] >= year_from) & (df["ref_year"] <= year_to)]
        )

    return _post_process_trade(_synthesize_trade(year_from, year_to))


def _post_process_trade(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ref_year"] = df["ref_year"].astype(int)
    df["primary_value_usd"] = pd.to_numeric(df["primary_value_usd"], errors="coerce")
    df = df.dropna(subset=["primary_value_usd", "reporter_iso", "partner_iso"])
    df["flow_desc"] = df["flow_desc"].fillna(
        df["flow_code"].map({"M": "Import", "X": "Export"})
    )
    df["cmd_code"] = df["cmd_code"].map(_norm_cmd_code)
    return df


# ---------------------------------------------------------------------------
# News fact
# ---------------------------------------------------------------------------
NEWS_SQL = """
SELECT
    article_id,
    url_hash,
    cmd_code,
    matched_term,
    match_score,
    title,
    url,
    source_domain,
    article_date,
    year_month_date,
    period,
    language,
    sentiment,
    trade_signals
FROM news_articles
"""


@st.cache_data(ttl=3600, show_spinner="Loading news data…")
def load_news() -> pd.DataFrame:
    """Load the news article DataFrame from DB, cached parquet, or synthetic."""
    eng = _engine()
    if eng is not None:
        try:
            from sqlalchemy import text
            with eng.connect() as conn:
                df = pd.read_sql(text(NEWS_SQL), conn)
            return _post_process_news(df)
        except Exception as e:
            st.info(f"News DB query failed: {e}. Falling back to synthetic news.")

    cache_path = Path(__file__).resolve().parent.parent / "data" / "news_cached.parquet"
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
        return _post_process_news(df)

    return _post_process_news(_synthesize_news())


def _post_process_news(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["cmd_code"] = df["cmd_code"].map(_norm_cmd_code)
    # Normalize date — handle either ISO ('2026-01-01') or US ('1/1/26') strings
    df["article_date"] = pd.to_datetime(df["article_date"], errors="coerce",
                                        format="mixed", dayfirst=False)
    # Clean optional text fields
    for col in ("sentiment", "trade_signals", "language", "source_domain", "title"):
        if col in df.columns:
            df[col] = df[col].astype("object")
            df.loc[df[col].isna(), col] = None
            df[col] = df[col].where(df[col].notna(), None)
    # Year-month convenience
    df["year_month"] = df["article_date"].dt.to_period("M").astype(str)
    df["year"] = df["article_date"].dt.year
    # Has signal flag — non-empty trade_signals
    df["has_signal"] = (
        df.get("trade_signals", pd.Series([None] * len(df)))
        .map(lambda s: bool(s) and str(s).strip() != "")
    )
    return df


# ---------------------------------------------------------------------------
# Reference data — country / commodity / year accessors
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


def bilateral(df: pd.DataFrame) -> pd.DataFrame:
    """Exclude 'World' aggregate partners — true bilateral rows only."""
    return df[(df["partner_iso"] != "W00") & (df["partner_iso"] != "WLD")]


def country_totals(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["partner_iso"].isin(["W00", "WLD"])]


def by_commodity(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df["cmd_code"].astype(str) != "TOTAL") & (df["cmd_code"].notna())]


# ---------------------------------------------------------------------------
# Synthetic trade data
# ---------------------------------------------------------------------------
COUNTRIES = [
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


def _synthesize_trade(year_from: int, year_to: int) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    rows = []
    scale = {iso: rng.lognormal(mean=24.5, sigma=1.2) for iso, *_ in COUNTRIES}
    cmd_scale = {c: rng.lognormal(mean=0.0, sigma=0.6) for c, _ in COMMODITIES}
    iso_list = [c[0] for c in COUNTRIES]
    affinity = {}
    for a in iso_list:
        for b in iso_list:
            affinity[(a, b)] = rng.uniform(0.05, 1.0) if a != b else 0.0

    for year in range(year_from, year_to + 1):
        global_growth = 1.0 + 0.04 * (year - year_from) + rng.normal(0, 0.02)
        for r_iso, r_name, _, _ in COUNTRIES:
            for p_iso, p_name, p_lat, p_lon in COUNTRIES:
                if r_iso == p_iso:
                    continue
                if rng.random() < 0.35:
                    continue
                for cmd_code, cmd_desc in COMMODITIES:
                    if rng.random() < 0.45:
                        continue
                    base = scale[r_iso] * affinity[(r_iso, p_iso)] * cmd_scale[cmd_code]
                    value = base * global_growth * rng.lognormal(mean=0, sigma=0.7)
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

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Synthetic news data — tied to the same commodities as the trade synth, with
# realistic syndication (same title across many sources), partial sentiment
# coverage (~30%), and a believable trade-signal incidence (~25%).
# ---------------------------------------------------------------------------
NEWS_TEMPLATES = {
    "85": [  # Electronics
        ("US imposes new export controls on advanced semiconductors", ["sanction", "export ban"]),
        ("Chip shortage easing as fabs ramp up production", []),
        ("EU launches anti-dumping probe into Chinese electronics", ["tariff"]),
        ("Major chipmaker reports record export quarter", []),
        ("South Korea announces $19B semiconductor support package", ["subsidy"]),
        ("Taiwan strait tensions raise chip supply concerns", ["disruption"]),
    ],
    "84": [
        ("Machine-tool exports climb on capex recovery", []),
        ("Industrial equipment imports surge in Southeast Asia", []),
        ("China imposes inspection delays on imported machinery", ["disruption"]),
        ("Germany extends Russia sanctions to dual-use equipment", ["sanction"]),
    ],
    "87": [
        ("EU tariffs on Chinese EVs spark Beijing backlash", ["tariff"]),
        ("Mexican auto exports to US hit new high", []),
        ("Truckers' strike disrupts cross-border auto shipments", ["disruption", "strike"]),
        ("Carmakers warn of supply-chain shock from chip curbs", ["disruption"]),
        ("UK signs vehicle export deal with India", []),
    ],
    "27": [
        ("OPEC+ extends production cuts through year-end", ["disruption"]),
        ("LNG prices spike after pipeline incident", ["disruption"]),
        ("New US sanctions target Russian oil shipments", ["sanction"]),
        ("Crude exports rebound on stronger Asian demand", []),
        ("Red Sea attacks reroute tanker traffic", ["disruption"]),
    ],
    "30": [
        ("Generic-drug shortage extends into second quarter", ["shortage"]),
        ("India-US pharma trade dispute escalates", ["dispute"]),
        ("New EU rules tighten import standards on APIs", ["regulation"]),
        ("Vaccine exports rise on bilateral deals", []),
    ],
    "90": [
        ("Medical-device exports buoyed by aging populations", []),
        ("US restricts export of advanced lithography tools", ["export ban"]),
        ("Optical-instrument trade resumes after port strike", ["strike"]),
    ],
    "71": [
        ("Diamond imports plunge on weak luxury demand", []),
        ("Russia's gold sanctioned by G7 partners", ["sanction"]),
        ("Gold exports from Africa hit decade high", []),
    ],
    "39": [
        ("Plastic-resin tariffs raise prices across Asia", ["tariff"]),
        ("EU plastic packaging rules tighten import standards", ["regulation"]),
    ],
    "72": [
        ("US extends Section 232 tariffs on steel imports", ["tariff"]),
        ("Steel exports from Turkey hit by EU safeguard", ["tariff"]),
        ("Iron-ore shipping disrupted by cyclone in WA", ["disruption"]),
    ],
    "10": [
        ("Black Sea grain corridor agreement extended", []),
        ("Wheat prices climb on supply concerns", ["shortage"]),
        ("India extends rice export ban", ["export ban"]),
        ("Drought cuts Brazilian corn harvest forecast", ["shortage"]),
        ("China cuts beef import quota in protectionist push", ["tariff"]),
    ],
    "29": [
        ("Chemical exports recover from European weakness", []),
        ("China imposes anti-dumping duties on US chemicals", ["tariff"]),
    ],
    "62": [
        ("Bangladesh apparel exports rebound on EU orders", []),
        ("US extends tariff exclusions on imported garments", ["tariff"]),
        ("Vietnam apparel makers diversify away from cotton imports", []),
    ],
}


def _synthesize_news() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    article_id = 0
    sentiments = ["positive", "negative", "neutral", None, None, None, None]
    languages = ["English", "English", "English", "English",
                 "Spanish", "French", "German", "Chinese", "Portuguese"]
    # Make a pool of realistic-looking source domains
    source_pool = [
        "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "scmp.com",
        "nikkei.com", "agriculture.com", "investinglive.com", "lefaso.net",
        "naroomanewsonline.com.au", "westernadvocate.com.au", "areanews.com.au",
        "nynganobserver.com.au", "queanbeyanage.com.au", "irrigator.com.au",
        "therural.com.au", "gleninnesexaminer.com.au", "mundiario.com",
        "camdencourier.com.au", "canberratimes.com.au", "northweststar.com.au",
        "tradearabia.com", "argusmedia.com", "spglobal.com",
        "agrinews-pubs.com", "supplychaindive.com", "theshelbyreport.com",
    ]

    # Date range: Jan 2023 → Dec 2025 (matches trade data window)
    date_range = pd.date_range("2023-01-01", "2025-12-31", freq="D")

    for cmd_code, templates in NEWS_TEMPLATES.items():
        cmd_desc = next((d for c, d in COMMODITIES if c == cmd_code), cmd_code)
        # 50 - 220 articles per commodity, weighted toward big/contentious chapters
        n_base = int(rng.integers(120, 220) if cmd_code in ("85", "87", "27", "10")
                     else rng.integers(50, 130))
        for _ in range(n_base):
            template_title, template_signals = templates[rng.integers(0, len(templates))]
            article_date = pd.Timestamp(rng.choice(date_range))
            n_syndications = int(rng.choice([1, 1, 1, 2, 3, 5, 8, 12], p=[0.4, 0.15, 0.1, 0.1, 0.08, 0.08, 0.05, 0.04]))
            language = rng.choice(languages)
            sentiment = rng.choice(sentiments)
            # Add the article + (n_syndications - 1) syndicated copies of same title
            for _ in range(n_syndications):
                source = rng.choice(source_pool)
                signals_for_row = list(template_signals)
                # Sometimes the underlying article has additional signals
                if rng.random() < 0.08 and signals_for_row:
                    extras = rng.choice(["dispute", "regulation", "subsidy"], size=1)
                    signals_for_row.extend(list(extras))
                trade_signals = ",".join(sorted(set(signals_for_row))) if signals_for_row else ""
                rows.append({
                    "article_id": article_id,
                    "url_hash":   f"{article_id:032x}",
                    "cmd_code":   cmd_code,
                    "cmd_desc":   cmd_desc,
                    "matched_term": template_title.split()[0].lower(),
                    "match_score": float(round(rng.uniform(6, 15), 1)),
                    "title":      template_title,
                    "url":        f"https://{source}/{article_id}",
                    "source_domain": source,
                    "article_date": article_date,
                    "year_month_date": article_date.strftime("%Y-%m"),
                    "period":     int(article_date.strftime("%Y%m")),
                    "language":   language,
                    "sentiment":  sentiment,
                    "trade_signals": trade_signals,
                })
                article_id += 1

    return pd.DataFrame(rows)