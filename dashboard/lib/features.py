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


# ===========================================================================
#  NEWS AGGREGATIONS
#  All news functions operate on the news DataFrame returned by data.load_news().
#  cmd_code is normalized to canonical string form on both sides, so joins to
#  the trade fact work directly.
# ===========================================================================
NEGATIVE_SIGNALS = {
    "tariff", "sanction", "sanctions", "ban", "export ban", "import ban",
    "embargo", "strike", "shortage", "disruption", "dispute", "war",
    "blockade", "regulation",
}


def _explode_signals(news: pd.DataFrame) -> pd.DataFrame:
    """Long-form one-row-per-(article, signal) frame for trade_signals.

    Articles with no signal are dropped. Whitespace around comma-separated
    values is stripped.
    """
    if "trade_signals" not in news.columns:
        return pd.DataFrame(columns=["article_id", "cmd_code", "signal"])
    s = news.dropna(subset=["trade_signals"]).copy()
    s = s[s["trade_signals"].astype(str).str.strip() != ""]
    if s.empty:
        return pd.DataFrame(columns=["article_id", "cmd_code", "signal"])
    s["signal"] = s["trade_signals"].astype(str).str.split(",")
    s = s.explode("signal")
    s["signal"] = s["signal"].astype(str).str.strip().str.lower()
    s = s[s["signal"] != ""]
    return s[["article_id", "cmd_code", "signal"]]


@st.cache_data(ttl=1800)
def news_kpis(news: pd.DataFrame) -> dict:
    """High-level KPIs for the News overview."""
    if news.empty:
        return {"articles": 0, "unique_stories": 0, "sources": 0,
                "commodities": 0, "signal_share": 0.0,
                "languages": 0}
    return {
        "articles":       int(len(news)),
        "unique_stories": int(news["title"].nunique()),
        "sources":        int(news["source_domain"].nunique()),
        "commodities":    int(news["cmd_code"].nunique()),
        "signal_share":   float(news["has_signal"].mean()),
        "languages":      int(news["language"].nunique()),
    }


@st.cache_data(ttl=1800)
def news_volume_by_commodity(news: pd.DataFrame,
                             cmd_desc_lookup: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per cmd_code with article count, unique stories, signal share.

    cmd_desc_lookup optional: DataFrame with cmd_code + cmd_desc to join in
    pretty descriptions from the trade data.
    """
    if news.empty:
        return pd.DataFrame(columns=["cmd_code", "cmd_desc",
                                     "articles", "unique_stories", "signal_share"])
    g = news.groupby("cmd_code", as_index=False).agg(
        articles=("article_id", "count"),
        unique_stories=("title", "nunique"),
        signal_share=("has_signal", "mean"),
    )
    if "cmd_desc" in news.columns:
        nd = news.dropna(subset=["cmd_desc"])[["cmd_code", "cmd_desc"]].drop_duplicates()
        g = g.merge(nd, on="cmd_code", how="left")
    if cmd_desc_lookup is not None:
        cdl = cmd_desc_lookup.copy()
        cdl["cmd_code"] = cdl["cmd_code"].astype(str)
        g["cmd_code"] = g["cmd_code"].astype(str)
        if "cmd_desc" not in g.columns or g["cmd_desc"].isna().all():
            g = g.drop(columns=[c for c in ("cmd_desc",) if c in g.columns], errors="ignore")
            g = g.merge(cdl, on="cmd_code", how="left")
        else:
            g = g.merge(cdl.rename(columns={"cmd_desc": "_cdl_desc"}),
                        on="cmd_code", how="left")
            g["cmd_desc"] = g["cmd_desc"].fillna(g["_cdl_desc"])
            g = g.drop(columns=["_cdl_desc"])
    if "cmd_desc" not in g.columns:
        g["cmd_desc"] = g["cmd_code"]
    return g.sort_values("articles", ascending=False)


@st.cache_data(ttl=1800)
def news_signal_mix(news: pd.DataFrame) -> pd.DataFrame:
    """Count of articles per trade signal (long form, sorted)."""
    s = _explode_signals(news)
    if s.empty:
        return pd.DataFrame(columns=["signal", "articles"])
    return (
        s.groupby("signal", as_index=False)
         .size()
         .rename(columns={"size": "articles"})
         .sort_values("articles", ascending=False)
    )


@st.cache_data(ttl=1800)
def news_sentiment_mix(news: pd.DataFrame) -> pd.DataFrame:
    """Sentiment counts, with NULL → 'unlabeled' bucket."""
    if news.empty:
        return pd.DataFrame(columns=["sentiment", "articles"])
    s = news["sentiment"].fillna("unlabeled").str.lower()
    return (
        s.value_counts()
         .rename_axis("sentiment")
         .reset_index(name="articles")
    )


@st.cache_data(ttl=1800)
def news_top_sources(news: pd.DataFrame, n: int = 15) -> pd.DataFrame:
    if news.empty:
        return pd.DataFrame(columns=["source_domain", "articles", "unique_stories"])
    return (
        news.groupby("source_domain", as_index=False)
            .agg(articles=("article_id", "count"),
                 unique_stories=("title", "nunique"))
            .sort_values("articles", ascending=False)
            .head(n)
    )


@st.cache_data(ttl=1800)
def news_top_stories(news: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Stories ranked by syndication count (number of source domains that ran them)."""
    if news.empty:
        return pd.DataFrame(columns=[
            "title", "cmd_code", "first_seen", "syndications",
            "sources", "trade_signals", "url"])
    g = (
        news.groupby("title", as_index=False)
            .agg(cmd_code=("cmd_code", "first"),
                 first_seen=("article_date", "min"),
                 syndications=("article_id", "count"),
                 sources=("source_domain", "nunique"),
                 trade_signals=("trade_signals",
                                lambda s: ",".join(sorted({
                                    t.strip().lower()
                                    for v in s.dropna().astype(str)
                                    for t in v.split(",") if t.strip()
                                }))),
                 url=("url", "first"))
            .sort_values(["syndications", "first_seen"], ascending=[False, False])
            .head(n)
    )
    return g


@st.cache_data(ttl=1800)
def news_timeline(news: pd.DataFrame, freq: str = "M") -> pd.DataFrame:
    """Articles per period (default monthly). Returns columns: period, articles."""
    if news.empty or news["article_date"].isna().all():
        return pd.DataFrame(columns=["period", "articles"])
    ts = (
        news.dropna(subset=["article_date"])
            .set_index("article_date")
            .resample(freq).size()
            .rename("articles")
            .reset_index()
            .rename(columns={"article_date": "period"})
    )
    return ts


@st.cache_data(ttl=1800)
def commodity_news_risk(news: pd.DataFrame) -> pd.DataFrame:
    """Per-commodity composite *news* risk score (0-100).

    Combines (a) the log of article volume, (b) the share of articles
    carrying a disruption-style signal (tariff, sanction, ban, ...), and
    (c) the share of articles with negative sentiment. Rank-normalized
    across commodities so the output is comparable.

    This is a *parallel* signal to the structural risk on the Concentration
    page — they intentionally are not merged.
    """
    if news.empty:
        return pd.DataFrame(columns=["cmd_code", "articles",
                                     "neg_signal_share", "neg_sentiment_share",
                                     "news_risk_score"])

    # Negative-signal flag at article level
    neg_signal_flag = news["trade_signals"].fillna("").astype(str).str.lower().apply(
        lambda s: any(sig in s for sig in NEGATIVE_SIGNALS) if s else False
    )

    g = news.assign(_neg_signal=neg_signal_flag).groupby("cmd_code", as_index=False).agg(
        articles=("article_id", "count"),
        neg_signal_share=("_neg_signal", "mean"),
        neg_sentiment_share=(
            "sentiment",
            lambda s: float((s.fillna("").str.lower() == "negative").mean()),
        ),
    )
    # Pretty desc
    nd = news.dropna(subset=["cmd_desc"])[["cmd_code", "cmd_desc"]].drop_duplicates() \
        if "cmd_desc" in news.columns else pd.DataFrame(columns=["cmd_code", "cmd_desc"])
    g = g.merge(nd, on="cmd_code", how="left")

    # Rank-normalize and combine
    g["vol_n"]  = np.log1p(g["articles"]).rank(pct=True).fillna(0.5)
    g["sig_n"]  = g["neg_signal_share"].rank(pct=True).fillna(0.5)
    g["sent_n"] = g["neg_sentiment_share"].rank(pct=True).fillna(0.5)
    g["news_risk_score"] = (
        0.40 * g["vol_n"] + 0.40 * g["sig_n"] + 0.20 * g["sent_n"]
    ) * 100
    return g.drop(columns=["vol_n", "sig_n", "sent_n"]).sort_values(
        "news_risk_score", ascending=False
    )


@st.cache_data(ttl=1800)
def filter_news(news: pd.DataFrame,
                cmd_codes: list[str] | None = None,
                signals: list[str] | None = None,
                languages: list[str] | None = None,
                date_from: pd.Timestamp | None = None,
                date_to: pd.Timestamp | None = None,
                sentiments: list[str] | None = None) -> pd.DataFrame:
    """Apply the common set of news filters used across pages."""
    out = news
    if cmd_codes:
        out = out[out["cmd_code"].isin([str(c) for c in cmd_codes])]
    if languages:
        out = out[out["language"].isin(languages)]
    if sentiments:
        # None matches "unlabeled"
        target_real = [s for s in sentiments if s != "unlabeled"]
        keep_real = out["sentiment"].str.lower().isin([s.lower() for s in target_real]) \
            if target_real else pd.Series(False, index=out.index)
        keep_unl = out["sentiment"].isna() if "unlabeled" in sentiments else pd.Series(False, index=out.index)
        out = out[keep_real | keep_unl]
    if signals:
        sigs_lower = [s.lower() for s in signals]
        out = out[out["trade_signals"].fillna("").astype(str).str.lower().apply(
            lambda v: any(sig in v for sig in sigs_lower)
        )]
    if date_from is not None:
        out = out[out["article_date"] >= pd.Timestamp(date_from)]
    if date_to is not None:
        out = out[out["article_date"] <= pd.Timestamp(date_to)]
    return out


# ===========================================================================
#  NEWS ↔ TRADE INTEGRATION HELPERS
#  Used by inline news widgets on the trade pages.
# ===========================================================================
DISRUPTION_SIGNALS = {
    "tariff", "sanction", "sanctions", "ban", "export ban", "import ban",
    "embargo", "strike", "shortage", "disruption", "dispute", "blockade",
}


@st.cache_data(ttl=1800)
def recent_headlines(news: pd.DataFrame,
                     cmd_codes: list[str] | None = None,
                     n: int = 5,
                     dedup: bool = True) -> pd.DataFrame:
    """Return the most recent headlines for given commodities.

    With `dedup=True` (default), syndicated copies of the same title collapse
    into one row with a `sources` count — much more useful than 8 copies of
    the same story. Returns at most `n` rows.
    """
    if news.empty:
        return pd.DataFrame()

    sub = news
    if cmd_codes:
        sub = sub[sub["cmd_code"].isin([str(c) for c in cmd_codes])]
    if sub.empty:
        return pd.DataFrame()

    if not dedup:
        return sub.sort_values("article_date", ascending=False).head(n)

    grouped = (
        sub.groupby("title", as_index=False)
           .agg(cmd_code=("cmd_code", "first"),
                cmd_desc=("cmd_desc", "first") if "cmd_desc" in sub.columns else ("cmd_code", "first"),
                latest=("article_date", "max"),
                sources=("source_domain", "nunique"),
                syndications=("article_id", "count"),
                trade_signals=(
                    "trade_signals",
                    lambda s: ",".join(sorted({
                        t.strip().lower()
                        for v in s.dropna().astype(str)
                        for t in v.split(",") if t.strip()
                    }))
                ),
                url=("url", "first"))
           .sort_values("latest", ascending=False)
           .head(n)
    )
    return grouped


@st.cache_data(ttl=1800)
def commodities_with_disruption(news: pd.DataFrame,
                                lookback_days: int = 90,
                                signals: set[str] | None = None) -> set:
    """Return the set of cmd_codes that have at least one article carrying a
    disruption signal within the past `lookback_days`.

    Used by the 'Recent disruptions' filter chip on the Trade Flows map.
    """
    if news.empty:
        return set()
    sigs = signals if signals is not None else DISRUPTION_SIGNALS
    cutoff = news["article_date"].max() - pd.Timedelta(days=lookback_days)
    recent = news[news["article_date"] >= cutoff]
    if recent.empty:
        return set()
    has_disrupt = recent["trade_signals"].fillna("").astype(str).str.lower().apply(
        lambda v: any(sig in v for sig in sigs) if v else False
    )
    return set(recent.loc[has_disrupt, "cmd_code"].dropna().astype(str).unique())


@st.cache_data(ttl=1800)
def top_signal_for_commodity(news: pd.DataFrame, cmd_code: str,
                             lookback_days: int | None = None) -> tuple[str | None, int]:
    """Most-mentioned trade signal for a commodity. Returns (signal, count)."""
    if news.empty or cmd_code is None:
        return None, 0
    sub = news[news["cmd_code"] == str(cmd_code)]
    if lookback_days is not None and len(sub):
        cutoff = sub["article_date"].max() - pd.Timedelta(days=lookback_days)
        sub = sub[sub["article_date"] >= cutoff]
    sub = sub.dropna(subset=["trade_signals"])
    sub = sub[sub["trade_signals"].astype(str).str.strip() != ""]
    if sub.empty:
        return None, 0
    expanded = (
        sub["trade_signals"].astype(str).str.lower()
           .str.split(",").explode().str.strip()
    )
    expanded = expanded[expanded != ""]
    if expanded.empty:
        return None, 0
    counts = expanded.value_counts()
    return counts.index[0], int(counts.iloc[0])


@st.cache_data(ttl=1800)
def country_news_risk(trade: pd.DataFrame, news: pd.DataFrame,
                      flow: str = "X", year: int | None = None) -> pd.DataFrame:
    """Country-level news risk = export-share-weighted average of per-commodity
    news risk for each country.

    This is the per-country counterpart to commodity_news_risk(). The weighting
    is by export value share in `year` (default: latest available), so a
    country's news risk reflects what's happening in news for the goods that
    actually matter to that country's trade.

    Returns columns:
        reporter_iso, reporter_desc, news_risk_score, articles, top_cmd_code,
        top_cmd_desc, top_cmd_share
    """
    if news.empty:
        return pd.DataFrame(columns=[
            "reporter_iso", "reporter_desc", "news_risk_score",
            "articles", "top_cmd_code", "top_cmd_desc", "top_cmd_share"
        ])

    # Per-commodity news risk (already on 0-100 scale)
    cnr = commodity_news_risk(news)[["cmd_code", "news_risk_score", "articles"]]

    # Per-reporter-commodity export value, in the chosen year
    rc = reporter_commodity_year(trade)
    rc = rc[rc["flow_code"] == flow]
    if year is None:
        year = int(rc["ref_year"].max())
    rc = rc[rc["ref_year"] == year]
    if rc.empty:
        return pd.DataFrame()

    # Country totals for weighting
    totals = (rc.groupby("reporter_iso")["value"].sum()
                .rename("rep_total").reset_index())
    rc = rc.merge(totals, on="reporter_iso")
    rc["weight"] = rc["value"] / rc["rep_total"].replace(0, np.nan)

    # Join news risk by cmd_code
    rc["cmd_code"] = rc["cmd_code"].astype(str)
    cnr["cmd_code"] = cnr["cmd_code"].astype(str)
    j = rc.merge(cnr, on="cmd_code", how="left")
    # Commodities with no news → assume neutral baseline (50) so a country
    # whose basket simply isn't covered doesn't get scored as 'safe'.
    j["news_risk_score"] = j["news_risk_score"].fillna(50.0)
    j["articles"] = j["articles"].fillna(0).astype(int)

    # Weighted average per reporter
    grouped = j.groupby(["reporter_iso", "reporter_desc"], as_index=False).apply(
        lambda g: pd.Series({
            "news_risk_score": float((g["news_risk_score"] * g["weight"]).sum()),
            # Export-share-weighted article count — interpretable as
            # "expected article exposure" per unit of export value.
            "articles": float((g["articles"] * g["weight"]).sum()),
        }),
        include_groups=False,
    )
    grouped["articles"] = grouped["articles"].round().astype(int)

    # Also identify each country's top export commodity for tooltip context
    top_per_country = (
        rc.sort_values("value", ascending=False)
          .groupby("reporter_iso")
          .head(1)[["reporter_iso", "cmd_code", "cmd_desc", "weight"]]
          .rename(columns={"cmd_code": "top_cmd_code",
                           "cmd_desc": "top_cmd_desc",
                           "weight": "top_cmd_share"})
    )
    grouped = grouped.merge(top_per_country, on="reporter_iso", how="left")
    return grouped.sort_values("news_risk_score", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=1800)
def structural_vs_news(trade: pd.DataFrame, news: pd.DataFrame,
                       flow: str = "X", year: int | None = None) -> pd.DataFrame:
    """Join the structural risk score and the country news risk into one frame
    suitable for the quadrant scatter on the Concentration & Risk page."""
    structural = concentration_risk_score(trade)[
        ["reporter_iso", "reporter_desc", "risk_score", "hhi",
         "commodity_hhi", "top3_share"]
    ].rename(columns={"risk_score": "structural_risk"})

    news_risk = country_news_risk(trade, news, flow=flow, year=year)
    if news_risk.empty:
        structural["news_risk_score"] = 50.0
        structural["articles"] = 0
        structural["top_cmd_code"] = None
        structural["top_cmd_desc"] = None
        return structural

    merged = structural.merge(
        news_risk[["reporter_iso", "news_risk_score", "articles",
                   "top_cmd_code", "top_cmd_desc"]],
        on="reporter_iso", how="left"
    )
    merged["news_risk_score"] = merged["news_risk_score"].fillna(50.0)
    merged["articles"] = merged["articles"].fillna(0).astype(int)
    return merged