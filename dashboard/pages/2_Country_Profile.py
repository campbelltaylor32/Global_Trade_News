"""Country Profile — deep dive on a selected reporter.

News integrations:
  • Concentration callout enrichment — alongside the partner-HHI card, show
    the top news signal currently tagged on this country's #1 export
    commodity.
  • "In the news for this country" — table of the country's top export
    commodities with article volume, signal share, and the latest headline.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from lib import data, features, charts
from lib.style import (
    inject_css, render_sidebar, about_expander,
    kpi_card, caption, section_rule, PALETTE,
    fmt_money, fmt_pct, fmt_int,
)

st.set_page_config(page_title="Country Profile", page_icon="🌐", layout="wide", initial_sidebar_state="expanded")
inject_css()
render_sidebar()

df = data.load_trade()
news = data.load_news()
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
    # Dependency callouts — Partner HHI card now enriched with #9: the top
    # news signal currently tagged on the country's #1 export commodity.
    pc = features.partner_concentration(df, flow=flow_code).query(
        "reporter_iso == @iso and ref_year == @year"
    )
    if not pc.empty:
        r = pc.iloc[0]

        # Identify this country's #1 export commodity for the news enrichment
        top_cmd_row = (
            features.reporter_commodity_year(df)
            .query("reporter_iso == @iso and ref_year == @year and flow_code == 'X'")
            .sort_values("value", ascending=False)
            .head(1)
        )
        signal_extra = None
        if not top_cmd_row.empty and not news.empty:
            top_cmd_code = str(top_cmd_row.iloc[0]["cmd_code"])
            sig, count = features.top_signal_for_commodity(
                news, top_cmd_code, lookback_days=180
            )
            if sig:
                signal_extra = f"`{sig}` leads news on top export"

        hhi_label = ("Highly concentrated" if r["hhi"] > 2500
                     else "Moderate" if r["hhi"] > 1500
                     else "Diversified")
        st.markdown(kpi_card("Partner HHI", f"{r['hhi']:,.0f}",
                             delta=hhi_label,
                             sign=-1 if r["hhi"] > 2500 else 0),
                    unsafe_allow_html=True)
        if signal_extra:
            st.markdown(
                f'<div style="color:{PALETTE["text_muted"]};font-size:0.78rem;'
                f'margin-top:-8px;margin-bottom:10px;padding-left:2px;">'
                f"{signal_extra}</div>",
                unsafe_allow_html=True,
            )
        else:
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
caption("Which HS chapters make up this country's trade basket?")

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
caption("How have shares of the top 8 partners shifted year over year?")

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

# ─── In the news for this country (integration #7) ───────────────────────
if not news.empty:
    section_rule()
    st.subheader(f"In the news for {sel}")
    caption(
        "Coverage on this country's top export commodities. Useful for spotting "
        "headline-driven risks behind the trade picture above."
    )

    # Top 5 export commodities for the latest year
    top_cmds = (
        features.reporter_commodity_year(df)
        .query("reporter_iso == @iso and ref_year == @year and flow_code == 'X'")
        .sort_values("value", ascending=False)
        .head(5)
    )

    if top_cmds.empty:
        st.info("No export-commodity data for this country.")
    else:
        # Per-commodity rollup
        rows = []
        cutoff_90 = news["article_date"].max() - pd.Timedelta(days=90)
        for r in top_cmds.itertuples(index=False):
            cmd_news = news[news["cmd_code"] == str(r.cmd_code)]
            recent_n = int((cmd_news["article_date"] >= cutoff_90).sum())
            n_total = len(cmd_news)
            sig_share = (cmd_news["has_signal"].mean() if n_total else 0.0)
            latest_row = (cmd_news.sort_values("article_date", ascending=False).head(1))
            latest_title = (latest_row["title"].iloc[0]
                            if len(latest_row) else None)
            latest_url = (latest_row["url"].iloc[0]
                          if len(latest_row) else None)
            top_sig, _ = features.top_signal_for_commodity(
                cmd_news, str(r.cmd_code), lookback_days=180
            )
            rows.append({
                "HS": str(r.cmd_code),
                "Commodity": r.cmd_desc,
                "Export share": f"{(r.value / top_cmds['value'].sum())*100:.1f}%",
                "Articles (90d)": recent_n,
                "Articles (all)": n_total,
                "% with signal": f"{sig_share*100:.0f}%" if n_total else "—",
                "Top signal": top_sig or "—",
                "Latest headline": latest_title or "—",
                "URL": latest_url or "",
            })
        news_table = pd.DataFrame(rows)
        st.dataframe(
            news_table,
            use_container_width=True,
            hide_index=True,
            column_config={
                "URL": st.column_config.LinkColumn(display_text="open"),
                "Latest headline": st.column_config.TextColumn(width="large"),
            },
        )

        # Also surface the 5 most syndicated stories across this country's
        # top commodities — a "what's actually loud" view
        st.markdown(
            f'<div style="color:{PALETTE["text_muted"]};font-size:0.85rem;'
            f"margin-top:0.6rem;margin-bottom:0.4rem;\">"
            f"Most syndicated stories across these commodities</div>",
            unsafe_allow_html=True,
        )
        top_cmd_codes = top_cmds["cmd_code"].astype(str).tolist()
        country_news = news[news["cmd_code"].isin(top_cmd_codes)]
        top_stories = features.news_top_stories(country_news, n=5)
        if top_stories.empty:
            st.info("No news for this country's top export commodities.")
        else:
            show = top_stories[["title", "cmd_code", "syndications",
                                "sources", "trade_signals",
                                "first_seen", "url"]].rename(columns={
                "title": "Headline",
                "cmd_code": "HS",
                "syndications": "Articles",
                "sources": "Sources",
                "trade_signals": "Signals",
                "first_seen": "First seen",
                "url": "URL",
            })
            show["First seen"] = pd.to_datetime(show["First seen"]).dt.strftime("%Y-%m-%d")
            st.dataframe(
                show,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "URL": st.column_config.LinkColumn(display_text="open"),
                    "Headline": st.column_config.TextColumn(width="large"),
                },
            )

# ─── About this page ──────────────────────────────────────────────────────
section_rule()
about_expander(
    primary_question=(
        "For a given country, which partners and commodities drive its "
        "trade, and how exposed is it to a single relationship?"
    ),
    sub_questions=[
        ("How is this country's trade balance evolving?",
         "Exports vs imports · Net balance"),
        ("Who are its largest trading partners?",
         "Top trading partners"),
        ("How concentrated is its trade across partners?",
         "Partner HHI · Top-3 share · Effective partners"),
        ("What commodities make up its trade basket?",
         "Commodity composition treemap"),
        ("How is its partner mix shifting over time?",
         "Partner mix evolution"),
        ("What's in the news for its top export commodities?",
         "In the news for {country}"),
    ],
)