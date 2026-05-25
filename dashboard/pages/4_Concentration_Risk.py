"""Concentration & Risk — HHI, dependency, and composite scores.

News integration:
  • Quadrant scatter (#13) — countries plotted on (structural risk, news risk).
    Quadrants flag "Quietly fragile" (high structural, low news — risk the
    market isn't pricing) vs "In the storm" (both high — most urgent).
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from lib import data, features, charts
from lib.style import (
    inject_css, render_sidebar, about_expander,
    kpi_card, caption, section_rule,
    fmt_money, fmt_pct, fmt_int, PALETTE,
)

st.set_page_config(page_title="Concentration & Risk", page_icon="🌐", layout="wide", initial_sidebar_state="expanded")
inject_css()
render_sidebar()

df = data.load_trade()
news = data.load_news()
yr_min, yr_max = data.year_range(df)

st.title("Concentration & Risk")
caption(
    "Resilience signals for trade structure. Structural metrics come from "
    "concentration across partners and commodities and from volatility of "
    "trade. The news layer comes from per-commodity coverage volume, "
    "disruption signals, and sentiment, weighted to each country by its "
    "export basket."
)

# ─── Filters ──────────────────────────────────────────────────────────────
f1, f2 = st.columns([1, 3])
with f1:
    year = st.selectbox("Year", list(range(yr_max, yr_min - 1, -1)), index=0)
with f2:
    flow_label = st.radio("Flow side", ["Exports", "Imports"],
                          horizontal=True, index=0)
flow_code = "X" if flow_label == "Exports" else "M"

# ─── Quadrant scatter (NEW — structural vs news) ──────────────────────────
if not news.empty:
    st.subheader("Structural risk vs news risk")
    caption(
        "Each dot is a country. Horizontal: structural risk (concentration + "
        "volatility, 0–100). Vertical: news risk (export-share-weighted "
        "average of commodity-level news risk, 0–100). Dotted lines mark the "
        "medians; quadrants are labeled in each corner."
    )

    quad = features.structural_vs_news(df, news, flow=flow_code, year=year)
    if quad.empty:
        st.info("Not enough overlap between trade and news data to plot.")
    else:
        st.plotly_chart(
            charts.risk_quadrant_scatter(
                quad,
                x="structural_risk", y="news_risk_score",
                name="reporter_desc",
                size="articles",
                x_label="Structural risk (0–100)",
                y_label="News risk (0–100)",
            ),
            use_container_width=True,
        )

        # Tabular summary of each quadrant
        with st.expander("Countries by quadrant", expanded=False):
            x_med = quad["structural_risk"].median()
            y_med = quad["news_risk_score"].median()

            def q_of(r):
                if r["structural_risk"] >= x_med and r["news_risk_score"] >= y_med:
                    return "In the storm"
                if r["structural_risk"] >= x_med and r["news_risk_score"] < y_med:
                    return "Quietly fragile"
                if r["structural_risk"] < x_med and r["news_risk_score"] >= y_med:
                    return "Noisy but resilient"
                return "Stable"

            q = quad.copy()
            q["Quadrant"] = q.apply(q_of, axis=1)
            cq1, cq2 = st.columns(2)
            for col, label in zip(
                [cq1, cq1, cq2, cq2],
                ["In the storm", "Quietly fragile",
                 "Noisy but resilient", "Stable"],
            ):
                rows = q[q["Quadrant"] == label].sort_values(
                    "news_risk_score" if "news" in label.lower() else "structural_risk",
                    ascending=False,
                )
                col.markdown(f"**{label}** ({len(rows)})")
                show = rows[["reporter_desc", "structural_risk",
                             "news_risk_score", "articles",
                             "top_cmd_desc"]].copy()
                show = show.rename(columns={
                    "reporter_desc": "Country",
                    "structural_risk": "Struct.",
                    "news_risk_score": "News",
                    "articles": "Articles",
                    "top_cmd_desc": "Top export",
                })
                show["Struct."] = show["Struct."].map(lambda v: f"{v:.0f}")
                show["News"] = show["News"].map(lambda v: f"{v:.0f}")
                col.dataframe(show, use_container_width=True, hide_index=True)

    section_rule()

# ─── Risk score table ─────────────────────────────────────────────────────
score = features.concentration_risk_score(df)
pc = features.partner_concentration(df, flow=flow_code).query("ref_year == @year")
cc = features.commodity_concentration(df, flow=flow_code).query("ref_year == @year")

st.subheader("Composite structural risk score (top 15)")
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
    caption("Which countries rely most heavily on a single top partner?")
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
    caption("Which countries have the broadest trading base (lowest partner HHI)?")
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

# ─── About this page ──────────────────────────────────────────────────────
about_expander(
    primary_question=(
        "Which countries face the highest combined risk from structural "
        "concentration and current news signals?"
    ),
    sub_questions=[
        ("Which countries are structurally most fragile?",
         "Composite structural risk score · top 15"),
        ("How do structural risk and news pressure intersect?",
         "Structural risk vs news risk · quadrant chart"),
        ("How does concentration split between partners and commodities?",
         "Concentration map: partners vs commodities"),
        ("Which countries are most dependent on a single partner?",
         "Most partner-dependent"),
        ("Which countries have the most diversified trade base?",
         "Most diversified"),
    ],
)

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
- **Composite structural risk** — weighted, rank-based combination of the
  three structural metrics above. Trade-data only.
- **News risk (quadrant chart)** — export-share-weighted average of
  per-commodity news risk for that country. News risk per commodity =
  40% rank(log article volume) + 40% rank(disruption-signal share) +
  20% rank(negative-sentiment share). Commodities with no news coverage
  receive a neutral 50 (otherwise uncovered countries score as "safe").
        """
    )