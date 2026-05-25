"""Commodity Explorer — pick an HS chapter, see global market structure."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from lib import data, features, charts
from lib.style import (
    inject_css, render_sidebar, about_expander,
    kpi_card, caption, section_rule,
    fmt_money, fmt_pct, fmt_int,
)

st.set_page_config(page_title="Commodity Explorer", page_icon="🌐", layout="wide", initial_sidebar_state="expanded")
inject_css()
render_sidebar()

df = data.load_trade()
commodities = data.list_commodities(df)
yr_min, yr_max = data.year_range(df)

st.title("Commodity Explorer")
caption(
    "Who dominates global trade in a given commodity, how concentrated is the supply, "
    "and which alternative producers are emerging."
)

c1, c2 = st.columns([3, 1])
with c1:
    options = [f"{r.cmd_code} — {r.cmd_desc}" for r in commodities.itertuples()]
    choice = st.selectbox("HS commodity chapter", options, index=0)
    code = choice.split(" — ")[0]
    desc = choice.split(" — ", 1)[1]
with c2:
    year = st.selectbox("Year", list(range(yr_max, yr_min - 1, -1)), index=0)

# ─── Slice ────────────────────────────────────────────────────────────────
ms = features.commodity_market_share(df, flow="X")
ms_now = ms[(ms["cmd_code"].astype(str) == code) & (ms["ref_year"] == year)]

if ms_now.empty:
    st.warning("No data for this commodity-year combination.")
    st.stop()

global_value = ms_now["global_value"].iloc[0]
n_exporters = ms_now["reporter_iso"].nunique()
top1_share = ms_now["market_share"].max()
top3_share = ms_now.nlargest(3, "market_share")["market_share"].sum()

# Compute commodity-level HHI across exporters
shares = ms_now["market_share"].to_numpy()
hhi = float((shares ** 2).sum() * 10_000)
hhi_label = ("Highly concentrated" if hhi > 2500
             else "Moderate" if hhi > 1500
             else "Competitive")

k1, k2, k3, k4 = st.columns(4)
k1.markdown(kpi_card("Global export value", fmt_money(global_value)),
            unsafe_allow_html=True)
k2.markdown(kpi_card("Exporting countries", fmt_int(n_exporters)),
            unsafe_allow_html=True)
k3.markdown(kpi_card("Top-3 share", fmt_pct(top3_share, 0),
                     delta=hhi_label,
                     sign=-1 if hhi > 2500 else 0),
            unsafe_allow_html=True)
k4.markdown(kpi_card("Exporter HHI", f"{hhi:,.0f}"),
            unsafe_allow_html=True)

section_rule()

# ─── Map of producers + leaderboard ───────────────────────────────────────
left, right = st.columns([1.4, 1])

with left:
    st.subheader(f"Where {desc.lower()} is exported from, {year}")
    chmap = ms_now[["reporter_iso", "reporter_desc", "value"]].copy()
    st.plotly_chart(
        charts.world_choropleth(chmap, iso_col="reporter_iso", value_col="value",
                                hover_name="reporter_desc"),
        use_container_width=True,
    )

with right:
    st.subheader("Top exporters")
    top = ms_now.nlargest(12, "value")[["reporter_desc", "value", "market_share"]]
    top = top.rename(columns={"reporter_desc": "Country",
                              "value": "Export value (USD)",
                              "market_share": "Share"})
    top["Share"] = top["Share"].map(lambda x: f"{x*100:.1f}%")
    top["Export value (USD)"] = top["Export value (USD)"].map(lambda v: f"${v/1e9:,.2f}B")
    st.dataframe(top, use_container_width=True, hide_index=True, height=460)

section_rule()

# ─── Market share dynamics ────────────────────────────────────────────────
st.subheader("Market share dynamics over time")
caption("How are the top exporters' shares of global trade in this commodity shifting?")

ms_cmd = ms[ms["cmd_code"].astype(str) == code]
top_exporters = (
    ms_cmd[ms_cmd["ref_year"] == year]
    .nlargest(8, "value")["reporter_iso"].tolist()
)
evo = ms_cmd[ms_cmd["reporter_iso"].isin(top_exporters)][
    ["ref_year", "reporter_desc", "market_share"]]

st.plotly_chart(
    charts.trade_timeseries(evo, x="ref_year", y="market_share", color="reporter_desc"),
    use_container_width=True,
)

section_rule()

# ─── Emerging alternatives ────────────────────────────────────────────────
st.subheader("Emerging alternative suppliers")
caption(
    "Which countries are gaining the most share in this commodity over the "
    "last 3 years — candidate diversification destinations?"
)

if year - 3 >= yr_min:
    then = ms_cmd[ms_cmd["ref_year"] == year - 3][["reporter_iso", "reporter_desc",
                                                    "market_share"]]
    now  = ms_cmd[ms_cmd["ref_year"] == year][["reporter_iso", "market_share"]]
    chg = now.merge(then, on="reporter_iso", suffixes=("_now", "_then"))
    chg["share_change"] = chg["market_share_now"] - chg["market_share_then"]
    risers = chg.nlargest(10, "share_change")
    risers["Δ share (pp)"] = risers["share_change"] * 100
    risers["Now"]  = risers["market_share_now"]  * 100
    risers["Then"] = risers["market_share_then"] * 100
    show = risers[["reporter_desc", "Then", "Now", "Δ share (pp)"]].rename(
        columns={"reporter_desc": "Country"}
    )
    for col in ("Then", "Now", "Δ share (pp)"):
        show[col] = show[col].map(lambda v: f"{v:+.2f}%" if col == "Δ share (pp)" else f"{v:.2f}%")
    st.dataframe(show, use_container_width=True, hide_index=True)
else:
    st.info("Not enough history to compute 3-year share changes.")

# ─── News coverage for this commodity ─────────────────────────────────────
section_rule()
st.subheader("News coverage")
caption(
    f"Articles tagged to HS chapter {code}. Sentiment and signal columns are "
    f"populated where the upstream NLP pipeline labeled them."
)

news = data.load_news()
cmd_news = features.filter_news(news, cmd_codes=[code])

if cmd_news.empty:
    st.info("No news articles indexed for this commodity yet.")
else:
    # KPIs row
    nk = features.news_kpis(cmd_news)
    nc1, nc2, nc3, nc4 = st.columns(4)
    nc1.markdown(kpi_card("Articles", fmt_int(nk["articles"])), unsafe_allow_html=True)
    nc2.markdown(kpi_card("Unique stories", fmt_int(nk["unique_stories"]),
                          delta=f"of {nk['articles']:,} total"), unsafe_allow_html=True)
    nc3.markdown(kpi_card("With trade signal", fmt_pct(nk["signal_share"], 0)),
                 unsafe_allow_html=True)
    nc4.markdown(kpi_card("Distinct sources", fmt_int(nk["sources"])),
                 unsafe_allow_html=True)

    # Signal mix and timeline side by side
    nL, nR = st.columns([1, 1.4])
    with nL:
        st.markdown("**Signal mix**")
        sig_df = features.news_signal_mix(cmd_news)
        st.plotly_chart(charts.signal_bar(sig_df, title=""), use_container_width=True)
    with nR:
        st.markdown("**Coverage volume**")
        ts = features.news_timeline(cmd_news, freq="ME")
        st.plotly_chart(charts.news_timeline_chart(ts, title=""), use_container_width=True)

    # Top stories (dedup by title)
    st.markdown("**Most-syndicated stories**")
    top = features.news_top_stories(cmd_news, n=10)
    show = top[["title", "syndications", "sources", "trade_signals", "first_seen"]].rename(
        columns={"title": "Headline", "syndications": "Articles",
                 "sources": "Sources", "trade_signals": "Signals",
                 "first_seen": "First seen"}
    )
    show["First seen"] = pd.to_datetime(show["First seen"]).dt.strftime("%Y-%m-%d")
    st.dataframe(show, use_container_width=True, hide_index=True)

# ─── About this page ──────────────────────────────────────────────────────
section_rule()
about_expander(
    primary_question=(
        "Which countries dominate the global market for specific commodities, "
        "and where are alternative suppliers emerging?"
    ),
    sub_questions=[
        ("Who are the top exporters of this commodity?",
         "Choropleth · Top exporters table"),
        ("How concentrated is global supply (exporter HHI)?",
         "Exporter HHI KPI · Top-3 share"),
        ("How is market share shifting among the top players?",
         "Market share dynamics over time"),
        ("Which countries are gaining share fastest?",
         "Emerging alternative suppliers"),
        ("What is the recent news coverage on this commodity?",
         "News coverage section"),
    ],
)