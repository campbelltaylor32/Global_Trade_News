"""Global Trade Risk Ledger — Streamlit dashboard.

Entry page: executive overview.
Other pages live in pages/ and are auto-discovered by Streamlit.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from lib import data, features, charts
from lib.style import (
    inject_css, kpi_card, section_rule, caption,
    fmt_money, fmt_pct, fmt_int,
)

# ─── Page config ───────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Global Trade Risk Ledger",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_css()

# ─── Sidebar: global filters ───────────────────────────────────────────────
with st.sidebar:
    st.markdown("### Global Trade Risk Ledger")
    st.markdown(
        '<div style="color:#94A3B8;font-size:0.8rem;margin-top:-8px;">'
        "ADSP 31011 — UN Comtrade analytics"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown("---")

# ─── Load data ─────────────────────────────────────────────────────────────
df = data.load_trade(year_from=2020, year_to=2024)
yr_min, yr_max = data.year_range(df)

with st.sidebar:
    year = st.slider(
        "Reference year", min_value=int(yr_min), max_value=int(yr_max),
        value=int(yr_max), step=1,
    )
    flow_label = st.radio(
        "Flow", options=["Exports", "Imports", "Total trade"],
        horizontal=True, index=0,
    )
    flow_map = {"Exports": "X", "Imports": "M", "Total trade": "ALL"}
    flow_code = flow_map[flow_label]

# ─── Header ────────────────────────────────────────────────────────────────
st.title("Global Overview")
caption(
    f"State of world trade in {year}. Use the sidebar to switch year and flow direction. "
    "Switch pages on the left to drill into corridors, countries, and commodities."
)

# ─── Top KPIs ──────────────────────────────────────────────────────────────
cy = features.country_year_wide(df)
cy_now  = cy[cy["ref_year"] == year]
cy_prev = cy[cy["ref_year"] == year - 1]

total_trade_now  = float(cy_now["total_trade"].sum()) / 2  # /2: every flow double-counted
total_trade_prev = float(cy_prev["total_trade"].sum()) / 2 if len(cy_prev) else None
yoy = (total_trade_now / total_trade_prev - 1) if total_trade_prev else None

n_countries = cy_now["reporter_iso"].nunique()
n_corridors = (
    features.corridor_year(df)
    .query("ref_year == @year")
    .groupby(["reporter_iso", "partner_iso"]).size().shape[0]
)
n_commodities = (
    features.reporter_commodity_year(df)
    .query("ref_year == @year")["cmd_code"].nunique()
)

c1, c2, c3, c4 = st.columns(4)
c1.markdown(kpi_card("Global trade", fmt_money(total_trade_now),
                     delta=fmt_pct(yoy) if yoy is not None else None,
                     sign=1 if (yoy or 0) > 0 else -1 if (yoy or 0) < 0 else 0),
            unsafe_allow_html=True)
c2.markdown(kpi_card("Reporting countries", fmt_int(n_countries)),
            unsafe_allow_html=True)
c3.markdown(kpi_card("Active corridors", fmt_int(n_corridors)),
            unsafe_allow_html=True)
c4.markdown(kpi_card("HS commodity chapters", fmt_int(n_commodities)),
            unsafe_allow_html=True)

section_rule()

# ─── Choropleth: trade size by country ─────────────────────────────────────
st.subheader("Where the trade is")
caption(f"Total {flow_label.lower()} by reporter, {year}.")

cy_year = features.country_year(df).query("ref_year == @year")
if flow_code == "ALL":
    map_df = cy_year.groupby(["reporter_iso", "reporter_desc"], as_index=False)["value"].sum()
else:
    map_df = cy_year.query("flow_code == @flow_code")[
        ["reporter_iso", "reporter_desc", "value"]]

st.plotly_chart(
    charts.world_choropleth(
        map_df, iso_col="reporter_iso", value_col="value",
        hover_name="reporter_desc",
    ),
    use_container_width=True,
)

section_rule()

# ─── Top countries + movers ────────────────────────────────────────────────
left, right = st.columns([1, 1])

with left:
    st.subheader(f"Top reporters by {flow_label.lower()}")
    top_df = map_df.nlargest(15, "value").rename(columns={"reporter_desc": "Country"})
    st.plotly_chart(
        charts.bar_h(top_df, x="value", y="Country"),
        use_container_width=True,
    )

with right:
    st.subheader("Fastest growing economies (3-yr CAGR)")
    caption("Latest available year vs. 3 years prior. Only countries with both endpoints.")
    growers = features.country_growth_table(df, window=3)
    growers = growers.dropna(subset=["cagr"])
    growers = growers[growers["total_then"] > 0]
    top_grow = growers.nlargest(10, "cagr")[["reporter_desc", "cagr"]]
    top_grow = top_grow.rename(columns={"reporter_desc": "Country"})
    st.plotly_chart(
        charts.diverging_bar(top_grow, x="cagr", y="Country"),
        use_container_width=True,
    )

section_rule()

# ─── Trade balance leaderboard ─────────────────────────────────────────────
st.subheader("Largest trade surpluses and deficits")
caption(f"Net exports = exports − imports, {year}. Positive = surplus.")

bal = cy_now[["reporter_iso", "reporter_desc", "balance"]].copy()
bal = bal.sort_values("balance")
bottom5 = bal.head(8)
top5    = bal.tail(8)
extremes = pd.concat([bottom5, top5]).sort_values("balance")
extremes = extremes.rename(columns={"reporter_desc": "Country"})
st.plotly_chart(
    charts.diverging_bar(extremes, x="balance", y="Country"),
    use_container_width=True,
)

# ─── Time series of global trade ───────────────────────────────────────────
st.subheader("Global trade over time")
glob_ts = (
    features.country_year(df)
    .groupby(["ref_year", "flow_code"], as_index=False)["value"].sum()
)
glob_ts["flow"] = glob_ts["flow_code"].map({"X": "Exports", "M": "Imports"})
# /2 to undo double-counting in totals
glob_ts["value"] = glob_ts["value"] / 2
st.plotly_chart(
    charts.trade_timeseries(glob_ts, x="ref_year", y="value", color="flow"),
    use_container_width=True,
)

# ─── Footnote ──────────────────────────────────────────────────────────────
section_rule()
st.markdown(
    f'<div style="color:#64748B;font-size:0.78rem;">'
    f"Source: UN Comtrade (fact_trade_granular_v2). "
    f"Values in USD. Bilateral aggregates exclude 'World' (W00) totals to avoid double counting. "
    f"News and event signals will overlay on a future Risk Overlay page."
    f"</div>",
    unsafe_allow_html=True,
)
