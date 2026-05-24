"""Trade Flows — pydeck arc map with filtering by country / commodity / year."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from lib import data, features, charts
from lib.style import (
    inject_css, caption, section_rule, fmt_money,
)

st.set_page_config(page_title="Trade Flows", page_icon="🌐", layout="wide")
inject_css()

df = data.load_trade()
yr_min, yr_max = data.year_range(df)
countries  = data.list_countries(df)
commodities = data.list_commodities(df)

st.title("Trade Flows")
caption(
    "Interactive arc map of bilateral trade. Width scales with value (log). "
    "Teal end is the source, amber end is the destination."
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

# ─── Build flow dataset ────────────────────────────────────────────────────
flows = features.corridor_year(df).query("ref_year == @year and flow_code == @flow_code").copy()

if commodity_choice != "All commodities":
    code = commodity_choice.split(" — ")[0]
    # Recompute corridor totals for that one commodity
    rc = (
        df[(df["ref_year"] == year)
           & (df["flow_code"] == flow_code)
           & (df["cmd_code"].astype(str) == code)
           & (~df["partner_iso"].isin(["W00", "WLD"]))]
        .groupby(["reporter_iso", "reporter_desc",
                  "partner_iso", "partner_desc"], as_index=False)
        .agg(value=("primary_value_usd", "sum"),
             partner_lat=("partner_lat", "first"),
             partner_lon=("partner_lon", "first"))
    )
    flows = rc

if focus_iso != "—":
    iso = countries.set_index("name").loc[focus_iso, "iso"]
    flows = flows[(flows["reporter_iso"] == iso) | (flows["partner_iso"] == iso)]

# Need source coords too. Reporter coords come from country_geo via partner side
# for the opposite direction — easiest: build a small iso → (lat, lon) map.
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

section_rule()

# ─── Map ───────────────────────────────────────────────────────────────────
deck = charts.trade_flow_arc_map(flows)
st.pydeck_chart(deck, use_container_width=True)

section_rule()

# ─── Table of the visible corridors ────────────────────────────────────────
st.subheader("Corridors in view")
table = flows[["src_name", "dst_name", "value"]].rename(
    columns={"src_name": "From", "dst_name": "To", "value": "Trade value (USD)"}
)
table["Trade value (USD)"] = table["Trade value (USD)"].map(lambda v: f"${v:,.0f}")
st.dataframe(table, use_container_width=True, hide_index=True)

st.markdown(
    f'<div style="color:#64748B;font-size:0.78rem;margin-top:1rem;">'
    f"Tip: pick a focus country to isolate its trade network. Switch to a single HS chapter "
    f"to see commodity-specific corridors emerge."
    f"</div>",
    unsafe_allow_html=True,
)
