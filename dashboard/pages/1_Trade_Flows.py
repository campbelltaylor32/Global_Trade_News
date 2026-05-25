"""Trade Flows — Plotly natural-earth great-circle arc map.

Filters by year, flow direction, commodity, focus country, and top-N density.
News integrations:
  • Recent disruptions filter — limit map to commodities with disruption
    signals (tariff/sanction/strike/etc.) in news within the last 90 days.
  • Headline strip — when a single commodity is selected, surface the most
    syndicated stories for that HS code.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from lib import data, features, charts
from lib.style import (
    inject_css, render_sidebar, about_expander,
    caption, section_rule, fmt_money, PALETTE,
)

st.set_page_config(page_title="Trade Flows", page_icon="🌐", layout="wide", initial_sidebar_state="expanded")
inject_css()
render_sidebar()

df = data.load_trade()
news = data.load_news()
yr_min, yr_max = data.year_range(df)
countries  = data.list_countries(df)
commodities = data.list_commodities(df)

st.title("Trade Flows")
caption(
    "Bilateral trade corridors on a natural-earth projection. "
    "Line thickness and color intensity scale with corridor value (log-binned)."
)

# ─── Filters ───────────────────────────────────────────────────────────────
fc1, fc2, fc3, fc4, fc5 = st.columns([1, 1.2, 1.6, 1, 1])
with fc1:
    year = st.selectbox("Year", options=list(range(yr_max, yr_min - 1, -1)), index=0)
with fc2:
    flow_label = st.selectbox("Flow", ["Exports", "Imports"], index=0)
    flow_code = "X" if flow_label == "Exports" else "M"
with fc3:
    commodity_options = ["All commodities"] + [
        f"{r.cmd_code} — {r.cmd_desc}" for r in commodities.itertuples()
    ]
    commodity_choice = st.selectbox("Commodity", commodity_options, index=0)
with fc4:
    top_n = st.slider("Top N corridors", min_value=20, max_value=500, value=100, step=20)
with fc5:
    focus_iso = st.selectbox(
        "Focus country", ["—"] + countries["name"].tolist(), index=0,
    )

# Disruption filter chip — only meaningful with news data available and when
# the commodity filter is broad (otherwise it just duplicates the picker)
disruption_filter_row = st.columns([4, 1])
with disruption_filter_row[1]:
    disruption_only = st.toggle(
        "Recent disruptions only",
        value=False,
        help=(
            "Restrict the map to corridors whose commodity has had a "
            "disruption-tagged article (tariff, sanction, ban, embargo, "
            "strike, shortage, disruption, dispute, blockade) in the news "
            "within the last 90 days."
        ),
        disabled=news.empty,
    )

# ─── Build flow dataset ────────────────────────────────────────────────────
selected_cmd_code: str | None = None
selected_cmd_desc: str | None = None
if commodity_choice != "All commodities":
    selected_cmd_code = commodity_choice.split(" — ")[0]
    selected_cmd_desc = commodity_choice.split(" — ", 1)[1]

flows = features.corridor_year(df).query("ref_year == @year and flow_code == @flow_code").copy()

if selected_cmd_code is not None:
    rc = (
        df[(df["ref_year"] == year)
           & (df["flow_code"] == flow_code)
           & (df["cmd_code"].astype(str) == selected_cmd_code)
           & (~df["partner_iso"].isin(["W00", "WLD"]))]
        .groupby(["reporter_iso", "reporter_desc",
                  "partner_iso", "partner_desc"], as_index=False)
        .agg(value=("primary_value_usd", "sum"),
             partner_lat=("partner_lat", "first"),
             partner_lon=("partner_lon", "first"))
    )
    flows = rc

# Apply disruption filter (works for both "All commodities" and a specific one)
if disruption_only and not news.empty:
    disrupted_codes = features.commodities_with_disruption(news, lookback_days=90)
    if selected_cmd_code is not None:
        # If the user has already picked a commodity, the toggle only keeps
        # the map populated when that commodity is itself disrupted.
        if selected_cmd_code not in disrupted_codes:
            flows = flows.iloc[0:0]
    else:
        # Otherwise we need the per-row cmd_code, which corridor_year drops —
        # so rebuild flows from the granular fact restricted to disrupted codes.
        flows = (
            df[(df["ref_year"] == year)
               & (df["flow_code"] == flow_code)
               & (df["cmd_code"].astype(str).isin(disrupted_codes))
               & (~df["partner_iso"].isin(["W00", "WLD"]))]
            .groupby(["reporter_iso", "reporter_desc",
                      "partner_iso", "partner_desc"], as_index=False)
            .agg(value=("primary_value_usd", "sum"),
                 partner_lat=("partner_lat", "first"),
                 partner_lon=("partner_lon", "first"))
        )

if focus_iso != "—":
    iso = countries.set_index("name").loc[focus_iso, "iso"]
    flows = flows[(flows["reporter_iso"] == iso) | (flows["partner_iso"] == iso)]

# Source coords from partner-side geo
geo_lookup = (
    df.dropna(subset=["partner_lat", "partner_lon"])
      .groupby("partner_iso")[["partner_lat", "partner_lon"]]
      .agg("first")
      .to_dict("index")
)

def coords(iso: str):
    g = geo_lookup.get(iso)
    return (g["partner_lat"], g["partner_lon"]) if g else (None, None)

flows[["src_lat", "src_lon"]] = flows["reporter_iso"].apply(
    lambda i: pd.Series(coords(i))
)
flows = flows.rename(columns={"partner_lat": "dst_lat", "partner_lon": "dst_lon",
                              "reporter_desc": "src_name", "partner_desc": "dst_name"})
flows = flows.dropna(subset=["src_lat", "src_lon", "dst_lat", "dst_lon"])
flows = flows.nlargest(top_n, "value").reset_index(drop=True)

# ─── Top-line metrics ──────────────────────────────────────────────────────
m1, m2, m3 = st.columns(3)
m1.metric("Corridors shown", f"{len(flows):,}")
m2.metric("Total value", fmt_money(flows["value"].sum()))
m3.metric(
    "Average corridor",
    fmt_money(flows["value"].mean()) if len(flows) else "—",
)

if disruption_only and len(flows) == 0:
    st.warning(
        "No corridors match the current filters with the 'Recent disruptions' "
        "toggle on. Either widen the commodity filter or turn the toggle off."
    )

# ─── Headline strip (only when a single commodity is selected) ────────────
if selected_cmd_code is not None and not news.empty:
    section_rule()
    head_l, head_r = st.columns([3, 1])
    with head_l:
        st.markdown(
            f'<div style="color:{PALETTE["text_muted"]};font-size:0.78rem;'
            f'text-transform:uppercase;letter-spacing:0.08em;">In the news · '
            f'HS {selected_cmd_code}</div>'
            f'<div style="font-size:1.0rem;color:{PALETTE["text"]};margin-top:2px;'
            f'margin-bottom:8px;">Top stories on {selected_cmd_desc.lower()}</div>',
            unsafe_allow_html=True,
        )
    headlines = features.recent_headlines(news, cmd_codes=[selected_cmd_code], n=5)
    if headlines.empty:
        with head_l:
            st.markdown(
                f'<div style="color:{PALETTE["text_muted"]};font-size:0.85rem;">'
                f"No recent coverage indexed for this commodity.</div>",
                unsafe_allow_html=True,
            )
    else:
        # 5-column horizontal strip
        cols = st.columns(len(headlines))
        for col, row in zip(cols, headlines.itertuples(index=False)):
            sigs = row.trade_signals or ""
            sig_html = ""
            if sigs:
                for s in [s for s in sigs.split(",") if s][:2]:
                    sig_html += (
                        f'<span style="display:inline-block;background:{PALETTE["panel_alt"]};'
                        f'color:{PALETTE["accent"]};border:1px solid {PALETTE["border"]};'
                        f'border-radius:999px;padding:1px 8px;font-size:0.70rem;'
                        f'margin-right:4px;margin-top:6px;">{s}</span>'
                    )
            date_str = (pd.to_datetime(row.latest).strftime("%b %d, %Y")
                        if pd.notna(row.latest) else "")
            col.markdown(
                f'<div style="background:{PALETTE["panel"]};border:1px solid '
                f'{PALETTE["border"]};border-radius:10px;padding:12px 14px;'
                f'height:100%;">'
                f'<div style="color:{PALETTE["text_muted"]};font-size:0.72rem;">'
                f"{date_str} · {row.sources} source{'s' if row.sources != 1 else ''}"
                f"</div>"
                f'<a href="{row.url}" target="_blank" '
                f'style="color:{PALETTE["text"]};font-size:0.88rem;line-height:1.35;'
                f'text-decoration:none;display:block;margin-top:6px;">{row.title}</a>'
                f"{sig_html}"
                f"</div>",
                unsafe_allow_html=True,
            )

section_rule()

# ─── Map ───────────────────────────────────────────────────────────────────
fig = charts.trade_flow_arc_map(flows)
st.plotly_chart(fig, use_container_width=True)

section_rule()

# ─── Table of the visible corridors ────────────────────────────────────────
st.subheader("Corridors in view")
table = flows[["src_name", "dst_name", "value"]].rename(
    columns={"src_name": "From", "dst_name": "To", "value": "Trade value (USD)"}
)
table["Trade value (USD)"] = table["Trade value (USD)"].map(lambda v: f"${v:,.0f}")
st.dataframe(table, use_container_width=True, hide_index=True)

# ─── About this page ──────────────────────────────────────────────────────
section_rule()
about_expander(
    primary_question=(
        "Which trade corridors carry the most volume, and which face "
        "active disruption signals?"
    ),
    sub_questions=[
        ("Which bilateral corridors carry the most value?",
         "Arc map · Corridors in view table"),
        ("How do corridors shift when isolated to a single commodity?",
         "Commodity filter · Arc map"),
        ("Which corridors involve commodities under active news disruption?",
         "Recent disruptions toggle"),
        ("What are the latest headlines on the selected commodity?",
         "In the news strip"),
    ],
)

st.markdown(
    f'<div style="color:{PALETTE["text_muted"]};font-size:0.78rem;margin-top:1rem;">'
    f"Tip: pick a focus country to isolate its trade network, or toggle "
    f"<i>Recent disruptions</i> to see only corridors with active news signals."
    f"</div>",
    unsafe_allow_html=True,
)