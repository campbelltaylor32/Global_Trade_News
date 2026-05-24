"""Derived metrics for the dashboard.

Every function takes a tidy trade DataFrame (see lib.data) and returns either
an aggregate frame or a scalar. Heavy aggregations are cached via @st.cache_data.

All money values are in USD. All shares are in 0..1 (multiply by 100 for %).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from .data import bilateral, by_commodity


# ---------------------------------------------------------------------------
# Base aggregates
# ---------------------------------------------------------------------------
@st.cache_data(ttl=1800)
def country_year(df: pd.DataFrame) -> pd.DataFrame:
    """Per reporter-year-flow totals (across all partners and commodities).

    Uses bilateral rows (no 'World' aggregate). Returns columns:
        reporter_iso, reporter_desc, ref_year, flow_code, value
    """
    b = bilateral(df)
    out = (
        b.groupby(["reporter_iso", "reporter_desc", "ref_year", "flow_code"], as_index=False)
         ["primary_value_usd"].sum()
         .rename(columns={"primary_value_usd": "value"})
    )
    return out


@st.cache_data(ttl=1800)
def country_year_wide(df: pd.DataFrame) -> pd.DataFrame:
    """Country-year with exports, imports, balance, total_trade as columns."""
    cy = country_year(df)
    wide = (
        cy.pivot_table(
            index=["reporter_iso", "reporter_desc", "ref_year"],
            columns="flow_code", values="value", aggfunc="sum",
        )
        .fillna(0.0)
        .reset_index()
    )
    wide.columns.name = None
    wide = wide.rename(columns={"M": "imports", "X": "exports"})
    if "exports" not in wide: wide["exports"] = 0.0
    if "imports" not in wide: wide["imports"] = 0.0
    wide["total_trade"] = wide["exports"] + wide["imports"]
    wide["balance"]     = wide["exports"] - wide["imports"]
    return wide


@st.cache_data(ttl=1800)
def corridor_year(df: pd.DataFrame) -> pd.DataFrame:
    """Reporter–Partner–Year–Flow totals (the bilateral corridor view)."""
    b = bilateral(df)
    cols = ["reporter_iso", "reporter_desc",
            "partner_iso", "partner_desc",
            "ref_year", "flow_code",
            "partner_lat", "partner_lon"]
    out = (
        b.groupby(cols, as_index=False, dropna=False)
         ["primary_value_usd"].sum()
         .rename(columns={"primary_value_usd": "value"})
    )
    return out


@st.cache_data(ttl=1800)
def reporter_commodity_year(df: pd.DataFrame) -> pd.DataFrame:
    """Per reporter–commodity–year totals (excluding 'TOTAL' aggregate)."""
    bc = by_commodity(bilateral(df))
    out = (
        bc.groupby(
            ["reporter_iso", "reporter_desc",
             "cmd_code", "cmd_desc",
             "ref_year", "flow_code"], as_index=False)
          ["primary_value_usd"].sum()
          .rename(columns={"primary_value_usd": "value"})
    )
    return out


# ---------------------------------------------------------------------------
# Growth metrics
# ---------------------------------------------------------------------------
def yoy_growth(df: pd.DataFrame, group_cols: list[str], value_col: str = "value",
               year_col: str = "ref_year") -> pd.DataFrame:
    """Add year-over-year growth (`yoy`) and prior value (`value_prev`) columns."""
    df = df.sort_values(group_cols + [year_col]).copy()
    df["value_prev"] = df.groupby(group_cols)[value_col].shift(1)
    df["yoy"] = (df[value_col] - df["value_prev"]) / df["value_prev"].replace(0, np.nan)
    return df


def cagr(start_value: pd.Series, end_value: pd.Series, periods: int) -> pd.Series:
    """Compound annual growth rate over `periods` years."""
    safe_start = start_value.replace(0, np.nan)
    return (end_value / safe_start) ** (1.0 / periods) - 1.0


@st.cache_data(ttl=1800)
def country_growth_table(df: pd.DataFrame, window: int = 3) -> pd.DataFrame:
    """For each country: latest-year value, CAGR over the last `window` years, abs delta.

    Returns one row per reporter with columns:
        reporter_iso, reporter_desc, total_now, total_then, cagr, abs_change
    """
    cy = country_year_wide(df)
    max_year = cy["ref_year"].max()
    min_year = max_year - window
    if min_year < cy["ref_year"].min():
        min_year = cy["ref_year"].min()
    periods = max(int(max_year - min_year), 1)

    now  = cy[cy["ref_year"] == max_year][["reporter_iso", "reporter_desc", "total_trade"]]
    then = cy[cy["ref_year"] == min_year][["reporter_iso", "total_trade"]]
    m = now.merge(then, on="reporter_iso", suffixes=("_now", "_then"))
    m["cagr"]       = cagr(m["total_trade_then"], m["total_trade_now"], periods)
    m["abs_change"] = m["total_trade_now"] - m["total_trade_then"]
    return m.rename(columns={"total_trade_now": "total_now", "total_trade_then": "total_then"})


# ---------------------------------------------------------------------------
# Concentration: HHI, top-N dependency, effective partner count
# ---------------------------------------------------------------------------
def _hhi(values: np.ndarray) -> float:
    total = values.sum()
    if total <= 0:
        return np.nan
    shares = values / total
    return float((shares ** 2).sum() * 10_000)


@st.cache_data(ttl=1800)
def partner_concentration(df: pd.DataFrame, flow: str = "X") -> pd.DataFrame:
    """Per reporter–year, HHI across partners + top-3 dependency + effective N.

    flow: 'X' (exports), 'M' (imports), or 'ALL' (sum of both).
    """
    cor = corridor_year(df)
    if flow != "ALL":
        cor = cor[cor["flow_code"] == flow]
    g = cor.groupby(["reporter_iso", "reporter_desc", "ref_year"])

    rows = []
    for (riso, rdesc, year), grp in g:
        vals = grp["value"].to_numpy()
        total = vals.sum()
        if total <= 0:
            continue
        shares = vals / total
        sorted_shares = np.sort(shares)[::-1]
        rows.append({
            "reporter_iso": riso,
            "reporter_desc": rdesc,
            "ref_year": year,
            "hhi": float((shares ** 2).sum() * 10_000),
            "top1_share": float(sorted_shares[0]) if len(sorted_shares) >= 1 else np.nan,
            "top3_share": float(sorted_shares[:3].sum()) if len(sorted_shares) >= 3 else float(sorted_shares.sum()),
            "top5_share": float(sorted_shares[:5].sum()) if len(sorted_shares) >= 5 else float(sorted_shares.sum()),
            "effective_partners": float(1.0 / (shares ** 2).sum()),
            "n_partners": int(len(shares)),
            "total_value": float(total),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=1800)
def commodity_concentration(df: pd.DataFrame, flow: str = "X") -> pd.DataFrame:
    """HHI across commodities per reporter-year (export or import basket diversity)."""
    rc = reporter_commodity_year(df)
    if flow != "ALL":
        rc = rc[rc["flow_code"] == flow]
    g = rc.groupby(["reporter_iso", "reporter_desc", "ref_year"])
    rows = []
    for (riso, rdesc, year), grp in g:
        vals = grp["value"].to_numpy()
        total = vals.sum()
        if total <= 0:
            continue
        shares = vals / total
        rows.append({
            "reporter_iso": riso,
            "reporter_desc": rdesc,
            "ref_year": year,
            "commodity_hhi": float((shares ** 2).sum() * 10_000),
            "effective_commodities": float(1.0 / (shares ** 2).sum()),
            "n_commodities": int(len(shares)),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Market share / dominance per commodity
# ---------------------------------------------------------------------------
@st.cache_data(ttl=1800)
def commodity_market_share(df: pd.DataFrame, flow: str = "X") -> pd.DataFrame:
    """For each commodity-year, each reporter's share of global flow."""
    rc = reporter_commodity_year(df)
    if flow != "ALL":
        rc = rc[rc["flow_code"] == flow]
    totals = rc.groupby(["cmd_code", "ref_year"])["value"].sum().rename("global_value")
    out = rc.merge(totals, on=["cmd_code", "ref_year"])
    out["market_share"] = out["value"] / out["global_value"]
    return out


# ---------------------------------------------------------------------------
# Bilateral trade intensity (relationship strength)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=1800)
def trade_intensity(df: pd.DataFrame, year: int, flow: str = "X") -> pd.DataFrame:
    """For a given year+flow, compute relationship strength:

        intensity = (bilateral / reporter_total) * (bilateral / partner_total)

    Higher means the pair matters disproportionately to both sides.
    """
    cor = corridor_year(df)
    cor = cor[(cor["ref_year"] == year) & (cor["flow_code"] == flow)]
    rep_tot = cor.groupby("reporter_iso")["value"].sum().rename("rep_total")
    par_tot = cor.groupby("partner_iso")["value"].sum().rename("par_total")
    out = cor.merge(rep_tot, on="reporter_iso").merge(par_tot, on="partner_iso")
    out["share_of_reporter"] = out["value"] / out["rep_total"]
    out["share_of_partner"]  = out["value"] / out["par_total"]
    out["intensity"]         = out["share_of_reporter"] * out["share_of_partner"]
    return out


# ---------------------------------------------------------------------------
# Movers — used on the Overview "biggest changes" panel
# ---------------------------------------------------------------------------
@st.cache_data(ttl=1800)
def top_corridor_movers(df: pd.DataFrame, n: int = 10, window: int = 3) -> pd.DataFrame:
    """Largest absolute and % changes in bilateral corridor value over `window` yrs."""
    cor = corridor_year(df)
    cor = cor.groupby(
        ["reporter_iso", "reporter_desc",
         "partner_iso",  "partner_desc",
         "ref_year"], as_index=False)["value"].sum()
    max_year = cor["ref_year"].max()
    min_year = max_year - window
    now  = cor[cor["ref_year"] == max_year][
        ["reporter_iso", "reporter_desc", "partner_iso", "partner_desc", "value"]]
    then = cor[cor["ref_year"] == min_year][
        ["reporter_iso", "partner_iso", "value"]]
    m = now.merge(then, on=["reporter_iso", "partner_iso"], suffixes=("_now", "_then"))
    m["abs_change"] = m["value_now"] - m["value_then"]
    m["pct_change"] = (m["value_now"] - m["value_then"]) / m["value_then"].replace(0, np.nan)
    return m


# ---------------------------------------------------------------------------
# A risk-style composite (purely from trade data — no news yet)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=1800)
def concentration_risk_score(df: pd.DataFrame) -> pd.DataFrame:
    """Combine partner-HHI, commodity-HHI, and 3yr volatility into one 0–100 score.

    NOT predictive — purely descriptive. Higher = more concentrated and volatile.
    """
    pc = partner_concentration(df, flow="X")
    cc = commodity_concentration(df, flow="X")
    cy = country_year_wide(df)

    # Volatility = std of YoY growth across the panel
    growth = yoy_growth(cy.rename(columns={"total_trade": "value"}),
                        group_cols=["reporter_iso"], value_col="value")
    vol = growth.groupby("reporter_iso")["yoy"].std().rename("yoy_vol")

    latest = pc["ref_year"].max()
    pc_l = pc[pc["ref_year"] == latest][["reporter_iso", "reporter_desc", "hhi", "top3_share"]]
    cc_l = cc[cc["ref_year"] == latest][["reporter_iso", "commodity_hhi"]]
    out = pc_l.merge(cc_l, on="reporter_iso").merge(vol, on="reporter_iso", how="left")

    # Normalize each component 0..1 (rank-based to handle outliers)
    for col in ("hhi", "commodity_hhi", "yoy_vol"):
        ranks = out[col].rank(pct=True)
        out[f"{col}_n"] = ranks.fillna(0.5)
    out["risk_score"] = (
        0.45 * out["hhi_n"] +
        0.35 * out["commodity_hhi_n"] +
        0.20 * out["yoy_vol_n"]
    ) * 100
    return out.sort_values("risk_score", ascending=False)
