"""Global Trade Risk Ledger — Streamlit dashboard.

Entry page: executive overview.
Other pages live in pages/ and are auto-discovered by Streamlit.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from lib import data, features, charts
from lib.style import (
    inject_css, render_sidebar, hero_banner, about_expander,
    kpi_card, section_rule, caption,
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
render_sidebar()

# ─── Load data ─────────────────────────────────────────────────────────────
df = data.load_trade()
yr_min, yr_max = data.year_range(df)

# ─── Hero ──────────────────────────────────────────────────────────────────
hero_banner(
    eyebrow="Global Trade Risk Ledger",
    title="Global Overview",
    tagline=(
        "A look at world trade through two lenses: structural metrics from UN "
        "Comtrade, and a news signal layer that flags where the headlines are "
        "concentrating. Start on this page for the big picture, then use the "
        "left rail to drill in."
    ),
    guide_items=[
        ("Trade Flows",
         "Where the heaviest corridors are, by commodity and year"),
        ("Country Profile",
         "Partners, basket, and dependency for one reporter"),
        ("Commodity Explorer",
         "Who dominates a given HS chapter, plus recent coverage"),
        ("Concentration & Risk",
         "Structural vs news risk — quadrant view"),
        ("AI Trade Analysis",
         "Natural-language chat against the warehouse via MCP Connector"),
    ],
    questions=[
        "Where is the world's trade concentrated, and how is the balance "
        "of trade shifting?",
        "Which trade corridors carry the most volume, and which face active "
        "disruption signals?",
        "For a given country, which partners and commodities drive its trade, "
        "and how exposed is it to a single relationship?",
        "Which countries dominate the global market for specific commodities, "
        "and where are alternative suppliers emerging?",
        "Which countries face the highest combined risk from structural "
        "concentration and current news signals?",
    ],
)

# ─── In-body filters ───────────────────────────────────────────────────────
ff1, ff2, _ = st.columns([1, 1.6, 4])
with ff1:
    year = st.selectbox(
        "Reference year",
        options=list(range(yr_max, yr_min - 1, -1)),
        index=0,
    )
with ff2:
    flow_label = st.radio(
        "Flow", options=["Exports", "Imports", "Total trade"],
        horizontal=True, index=0, label_visibility="visible",
    )
    flow_map = {"Exports": "X", "Imports": "M", "Total trade": "ALL"}
    flow_code = flow_map[flow_label]

section_rule()

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
caption(f"Which countries report the largest {flow_label.lower()} in {year}?")

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
caption(
    f"Which economies are net exporters, and which run the biggest deficits? "
    f"Net exports = exports − imports in {year}. Positive = surplus."
)

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

# ─── About this page ──────────────────────────────────────────────────────
section_rule()
about_expander(
    primary_question=(
        "Where is the world's trade concentrated, and how is the balance "
        "of trade shifting?"
    ),
    sub_questions=[
        ("Which countries report the largest exports or imports?",
         "Where the trade is · Top reporters"),
        ("Who is growing fastest over the last three years?",
         "Fastest growing economies"),
        ("Which countries run the largest surpluses and deficits?",
         "Largest trade surpluses and deficits"),
        ("How is global trade trending year over year?",
         "Global trade over time"),
    ],
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