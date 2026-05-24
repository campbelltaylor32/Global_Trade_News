"""Country Profile — deep dive on a selected reporter."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from lib import data, features, charts
from lib.style import (
    inject_css, kpi_card, caption, section_rule,
    fmt_money, fmt_pct, fmt_int,
)

st.set_page_config(page_title="Country Profile", page_icon="🌐", layout="wide")
inject_css()

df = data.load_trade()
countries = data.list_countries(df)
yr_min, yr_max = data.year_range(df)

# ─── Header + selector ────────────────────────────────────────────────────
st.title("Country Profile")
caption("Deep dive on one reporter: partners, commodity basket, balance, dependency.")

sel = st.selectbox(
    "Country",
    options=countries["name"].tolist(),
    index=countries["name"].tolist().index("United States")
        if "United States" in countries["name"].tolist() else 0,
)
iso = countries.set_index("name").loc[sel, "iso"]

cy = features.country_year_wide(df).query("reporter_iso == @iso")
if cy.empty:
    st.warning("No data for this country.")
    st.stop()
cy_now = cy[cy["ref_year"] == cy["ref_year"].max()].iloc[0]
cy_prev = cy[cy["ref_year"] == cy["ref_year"].max() - 1]
yoy = (
    (cy_now["total_trade"] / cy_prev.iloc[0]["total_trade"] - 1)
    if len(cy_prev) and cy_prev.iloc[0]["total_trade"] > 0 else None
)

# ─── KPIs ─────────────────────────────────────────────────────────────────
k1, k2, k3, k4 = st.columns(4)
k1.markdown(
    kpi_card("Total trade", fmt_money(cy_now["total_trade"]),
             delta=fmt_pct(yoy) if yoy is not None else None,
             sign=1 if (yoy or 0) > 0 else -1),
    unsafe_allow_html=True,
)
k2.markdown(kpi_card("Exports", fmt_money(cy_now["exports"])), unsafe_allow_html=True)
k3.markdown(kpi_card("Imports", fmt_money(cy_now["imports"])), unsafe_allow_html=True)
k4.markdown(
    kpi_card(
        "Trade balance",
        fmt_money(cy_now["balance"]),
        delta=("Surplus" if cy_now["balance"] > 0 else "Deficit"),
        sign=1 if cy_now["balance"] > 0 else -1,
    ),
    unsafe_allow_html=True,
)

section_rule()

# ─── Trade balance over time ──────────────────────────────────────────────
left, right = st.columns([1.4, 1])

with left:
    st.subheader("Exports vs imports over time")
    ts = cy.melt(id_vars="ref_year", value_vars=["exports", "imports"],
                 var_name="flow", value_name="value")
    st.plotly_chart(
        charts.trade_timeseries(ts, x="ref_year", y="value", color="flow"),
        use_container_width=True,
    )

with right:
    st.subheader("Net balance")
    bal_df = cy[["ref_year", "balance"]].rename(columns={"ref_year": "Year",
                                                          "balance": "value"})
    bal_df["Year"] = bal_df["Year"].astype(str)
    st.plotly_chart(
        charts.diverging_bar(bal_df.rename(columns={"value": "balance"}),
                             x="balance", y="Year"),
        use_container_width=True,
    )

section_rule()

# ─── Top partners ─────────────────────────────────────────────────────────
st.subheader("Top trading partners")
flow_for_partners = st.radio(
    "Direction", ["Exports", "Imports", "Both"], horizontal=True, index=0,
    key="partner_flow",
)
flow_code = {"Exports": "X", "Imports": "M", "Both": "ALL"}[flow_for_partners]
year = cy["ref_year"].max()

cor = features.corridor_year(df).query(
    "reporter_iso == @iso and ref_year == @year"
)
if flow_code != "ALL":
    cor = cor[cor["flow_code"] == flow_code]
partners = cor.groupby(["partner_iso", "partner_desc"], as_index=False)["value"].sum()
partners = partners.nlargest(15, "value").rename(columns={"partner_desc": "Partner"})

cA, cB = st.columns([1.4, 1])
with cA:
    st.plotly_chart(charts.bar_h(partners, x="value", y="Partner"),
                    use_container_width=True)
with cB:
    # Dependency callouts
    pc = features.partner_concentration(df, flow=flow_code).query(
        "reporter_iso == @iso and ref_year == @year"
    )
    if not pc.empty:
        r = pc.iloc[0]
        st.markdown(kpi_card("Partner HHI", f"{r['hhi']:,.0f}",
                             delta=("Highly concentrated" if r["hhi"] > 2500
                                    else "Moderate" if r["hhi"] > 1500
                                    else "Diversified"),
                             sign=-1 if r["hhi"] > 2500 else 0),
                    unsafe_allow_html=True)
        st.markdown("")
        st.markdown(kpi_card("Top-3 partner share", fmt_pct(r["top3_share"], 0)),
                    unsafe_allow_html=True)
        st.markdown("")
        st.markdown(kpi_card("Effective partners",
                             f"{r['effective_partners']:.1f}",
                             delta=f"out of {int(r['n_partners'])} total"),
                    unsafe_allow_html=True)

section_rule()

# ─── Commodity composition ────────────────────────────────────────────────
st.subheader("Commodity composition")
caption("HS chapters that make up this country's trade basket.")

rc = features.reporter_commodity_year(df).query(
    "reporter_iso == @iso and ref_year == @year"
)
if flow_code != "ALL":
    rc = rc[rc["flow_code"] == flow_code]
basket = rc.groupby(["cmd_code", "cmd_desc"], as_index=False)["value"].sum()
basket = basket.nlargest(12, "value")

st.plotly_chart(
    charts.commodity_treemap(basket, path=["cmd_desc"], value="value"),
    use_container_width=True,
)

section_rule()

# ─── Yearly partner shifts (sparkline-style) ──────────────────────────────
st.subheader("Partner mix evolution")
caption("How shares of the top 8 partners shifted across years.")

evo = features.corridor_year(df).query("reporter_iso == @iso")
if flow_code != "ALL":
    evo = evo[evo["flow_code"] == flow_code]
top_partners = (
    evo.groupby("partner_desc")["value"].sum()
       .nlargest(8).index.tolist()
)
evo = evo[evo["partner_desc"].isin(top_partners)]
evo = evo.groupby(["ref_year", "partner_desc"], as_index=False)["value"].sum()

st.plotly_chart(
    charts.trade_timeseries(evo, x="ref_year", y="value", color="partner_desc"),
    use_container_width=True,
)
