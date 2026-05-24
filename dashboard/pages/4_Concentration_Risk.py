"""Concentration & Risk — HHI, dependency, and a composite risk score.

This is the page that most directly answers the proposal's resilience question
*without* the news layer: which countries' trade profiles look fragile based
on concentration and volatility alone?
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from lib import data, features, charts
from lib.style import (
    inject_css, kpi_card, caption, section_rule,
    fmt_money, fmt_pct, fmt_int, PALETTE,
)

st.set_page_config(page_title="Concentration & Risk", page_icon="🌐", layout="wide")
inject_css()

df = data.load_trade()
yr_min, yr_max = data.year_range(df)

st.title("Concentration & Risk")
caption(
    "Resilience signals based on trade structure alone — concentration across "
    "partners and commodities, and volatility of trade. "
    "Higher score → more exposure to a single relationship or shock."
)

# ─── Filters ──────────────────────────────────────────────────────────────
f1, f2 = st.columns([1, 3])
with f1:
    year = st.selectbox("Year", list(range(yr_max, yr_min - 1, -1)), index=0)
with f2:
    flow_label = st.radio("Flow side", ["Exports", "Imports"],
                          horizontal=True, index=0)
flow_code = "X" if flow_label == "Exports" else "M"

# ─── Risk score table ─────────────────────────────────────────────────────
score = features.concentration_risk_score(df)
pc = features.partner_concentration(df, flow=flow_code).query("ref_year == @year")
cc = features.commodity_concentration(df, flow=flow_code).query("ref_year == @year")

# Latest snapshot — composite computed from the full panel, so it's a single view
st.subheader("Composite risk score (top 15)")
caption(
    "Composite = 0.45·partner HHI (rank) + 0.35·commodity HHI (rank) + 0.20·YoY "
    "volatility (rank). Descriptive, not predictive."
)

leaders = score.head(15).copy()
leaders["Risk score"]      = leaders["risk_score"].map(lambda v: f"{v:.1f}")
leaders["Partner HHI"]     = leaders["hhi"].map(lambda v: f"{v:,.0f}")
leaders["Commodity HHI"]   = leaders["commodity_hhi"].map(lambda v: f"{v:,.0f}")
leaders["Top-3 share"]     = leaders["top3_share"].map(lambda v: f"{v*100:.1f}%")
leaders["YoY volatility"]  = leaders["yoy_vol"].map(
    lambda v: f"{v*100:.1f}%" if pd.notna(v) else "—"
)
show_cols = ["reporter_desc", "Risk score", "Partner HHI",
             "Commodity HHI", "Top-3 share", "YoY volatility"]
leaders = leaders[show_cols].rename(columns={"reporter_desc": "Country"})
st.dataframe(leaders, use_container_width=True, hide_index=True)

section_rule()

# ─── Scatter: partner HHI vs commodity HHI ────────────────────────────────
st.subheader("Concentration map: partners vs commodities")
caption(
    "Bottom-left = diversified on both axes (safer). Top-right = concentrated on "
    "both — most exposed. Bubble size = total trade value."
)

merged = pc.merge(
    cc[["reporter_iso", "commodity_hhi"]], on="reporter_iso", how="inner"
)
merged = merged.rename(columns={
    "hhi": "Partner HHI", "commodity_hhi": "Commodity HHI",
    "total_value": "Total trade",
    "reporter_desc": "Country",
})
st.plotly_chart(
    charts.scatter_concentration(
        merged, x="Partner HHI", y="Commodity HHI",
        size="Total trade", hover_name="Country",
    ),
    use_container_width=True,
)

section_rule()

# ─── Dependency callouts ──────────────────────────────────────────────────
left, right = st.columns(2)

with left:
    st.subheader("Most partner-dependent")
    caption("Highest share of trade going to a single top-1 partner.")
    deps = pc.nlargest(10, "top1_share")[
        ["reporter_desc", "top1_share", "n_partners", "effective_partners"]
    ].copy()
    deps["Top-1 share"] = deps["top1_share"].map(lambda v: f"{v*100:.1f}%")
    deps["Effective partners"] = deps["effective_partners"].map(lambda v: f"{v:.1f}")
    deps["Active partners"]    = deps["n_partners"].astype(int)
    deps = deps[["reporter_desc", "Top-1 share", "Effective partners", "Active partners"]]
    deps = deps.rename(columns={"reporter_desc": "Country"})
    st.dataframe(deps, use_container_width=True, hide_index=True)

with right:
    st.subheader("Most diversified")
    caption("Lowest partner HHI — broadest trading base.")
    div = pc.nsmallest(10, "hhi")[
        ["reporter_desc", "hhi", "effective_partners", "n_partners"]
    ].copy()
    div["Partner HHI"] = div["hhi"].map(lambda v: f"{v:,.0f}")
    div["Effective partners"] = div["effective_partners"].map(lambda v: f"{v:.1f}")
    div["Active partners"]    = div["n_partners"].astype(int)
    div = div[["reporter_desc", "Partner HHI", "Effective partners", "Active partners"]]
    div = div.rename(columns={"reporter_desc": "Country"})
    st.dataframe(div, use_container_width=True, hide_index=True)

section_rule()

# ─── Glossary ─────────────────────────────────────────────────────────────
with st.expander("How to read these metrics"):
    st.markdown(
        """
- **HHI** (Herfindahl-Hirschman Index) — sum of squared shares × 10,000.
  Range: 0–10,000. Above **2,500** is conventionally called "highly concentrated."
- **Top-N share** — fraction of trade flowing through the top *N* partners.
- **Effective partners** — `1 / Σ(share²)`. The number of equal-sized
  partners that would yield the same HHI. Smaller = more concentrated.
- **YoY volatility** — standard deviation of year-over-year growth in total trade.
- **Composite risk score** — a weighted, rank-based combination of the above.
  Purely structural; no event or news data is used. *Future Risk Overlay page
  will combine this with GDELT/News tone signals.*
        """
    )
