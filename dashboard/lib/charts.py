"""Reusable chart builders. Keep chart styling in one place so every page
inherits the same look."""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from .style import PALETTE, SEQUENTIAL, DIVERGING, CATEGORICAL


# Common layout tweaks ───────────────────────────────────────────────────────
def _apply_layout(fig: go.Figure, height: int = 360, show_legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, system-ui, sans-serif", size=12, color=PALETTE["text"]),
        title=dict(font=dict(size=14, color=PALETTE["text"]), x=0, xanchor="left"),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
            bgcolor="rgba(0,0,0,0)",
        ),
        xaxis=dict(gridcolor=PALETTE["border"], zerolinecolor=PALETTE["border"]),
        yaxis=dict(gridcolor=PALETTE["border"], zerolinecolor=PALETTE["border"]),
        showlegend=show_legend,
        hoverlabel=dict(bgcolor=PALETTE["panel_alt"], bordercolor=PALETTE["border"]),
    )
    return fig


# Choropleth ────────────────────────────────────────────────────────────────
def world_choropleth(df: pd.DataFrame, iso_col: str, value_col: str,
                     title: str = "", hover_name: str | None = None,
                     unit: str = "$") -> go.Figure:
    """World choropleth on the dark basemap."""
    fig = px.choropleth(
        df, locations=iso_col, color=value_col,
        hover_name=hover_name or iso_col,
        color_continuous_scale=SEQUENTIAL,
        labels={value_col: ""},
    )
    fig.update_geos(
        showcoastlines=False, showland=True, landcolor=PALETTE["panel"],
        showocean=True, oceancolor=PALETTE["bg"],
        showframe=False, projection_type="natural earth",
        bgcolor="rgba(0,0,0,0)",
    )
    fig.update_traces(
        marker_line_color=PALETTE["border"], marker_line_width=0.5,
        hovertemplate=f"<b>%{{hovertext}}</b><br>{unit} %{{z:,.0f}}<extra></extra>",
    )
    fig.update_layout(
        title=title,
        height=480,
        margin=dict(l=0, r=0, t=30, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        coloraxis_colorbar=dict(
            thickness=10, len=0.6, x=0.95, tickfont=dict(color=PALETTE["text_muted"]),
        ),
        font=dict(color=PALETTE["text"]),
    )
    return fig


# Time series ───────────────────────────────────────────────────────────────
def trade_timeseries(df: pd.DataFrame, x: str = "ref_year", y: str = "value",
                     color: str | None = None, title: str = "",
                     stacked: bool = False) -> go.Figure:
    if color:
        fig = px.area(df, x=x, y=y, color=color, color_discrete_sequence=CATEGORICAL,
                      groupnorm="" if not stacked else None)
    else:
        fig = px.line(df, x=x, y=y, color_discrete_sequence=[PALETTE["accent"]])
    fig.update_traces(line=dict(width=2.5))
    fig.update_layout(title=title)
    return _apply_layout(fig, height=340)


def bar_h(df: pd.DataFrame, x: str, y: str, title: str = "",
          color: str | None = None) -> go.Figure:
    """Horizontal bar — top-N style."""
    df = df.sort_values(x)
    fig = px.bar(
        df, x=x, y=y, orientation="h",
        color=color, color_continuous_scale=SEQUENTIAL if color else None,
        color_discrete_sequence=[PALETTE["accent_soft"]],
    )
    fig.update_layout(title=title, coloraxis_showscale=False)
    fig.update_traces(hovertemplate="<b>%{y}</b><br>$%{x:,.0f}<extra></extra>")
    return _apply_layout(fig, height=max(280, 24 * len(df)), show_legend=False)


def diverging_bar(df: pd.DataFrame, x: str, y: str, title: str = "") -> go.Figure:
    """Bar with positive (green) and negative (red) splits — for trade balance, growth."""
    colors = np.where(df[x] >= 0, PALETTE["pos"], PALETTE["neg"])
    fig = go.Figure(go.Bar(x=df[x], y=df[y], orientation="h", marker_color=colors,
                           hovertemplate="<b>%{y}</b><br>%{x:,.2f}<extra></extra>"))
    fig.update_layout(title=title)
    return _apply_layout(fig, height=max(280, 24 * len(df)), show_legend=False)


def scatter_concentration(df: pd.DataFrame, x: str, y: str, size: str,
                          hover_name: str, title: str = "") -> go.Figure:
    fig = px.scatter(
        df, x=x, y=y, size=size, hover_name=hover_name,
        size_max=40, color_discrete_sequence=[PALETTE["accent"]],
        opacity=0.75,
    )
    fig.update_traces(marker=dict(line=dict(width=0.5, color=PALETTE["border"])))
    fig.update_layout(title=title)
    return _apply_layout(fig, height=440, show_legend=False)


# Treemap for commodity composition ─────────────────────────────────────────
def commodity_treemap(df: pd.DataFrame, path: list[str], value: str,
                      title: str = "") -> go.Figure:
    fig = px.treemap(
        df, path=path, values=value, color=value,
        color_continuous_scale=SEQUENTIAL,
    )
    fig.update_traces(
        marker=dict(line=dict(color=PALETTE["bg"], width=2)),
        hovertemplate="<b>%{label}</b><br>$%{value:,.0f}<extra></extra>",
        textfont=dict(color=PALETTE["text"]),
    )
    fig.update_layout(
        title=title,
        height=420,
        margin=dict(l=0, r=0, t=30, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color=PALETTE["text"]),
        coloraxis_showscale=False,
    )
    return fig


# Pydeck arc map ────────────────────────────────────────────────────────────
# Replaced by a Plotly natural-earth implementation below so the flow map
# matches the look of the choropleth pages and renders token-free.
# ---------------------------------------------------------------------------
def _great_circle_points(lat1: float, lon1: float,
                         lat2: float, lon2: float,
                         n: int = 36) -> tuple[np.ndarray, np.ndarray]:
    """Interpolate `n` points along the great-circle between two coords.

    Returns (lats, lons) as degrees. Handles antimeridian crossings cleanly
    enough for Plotly's natural earth projection — Plotly draws line breaks
    when the longitude jumps > 180°, which we avoid by interpolating in 3D.
    """
    p1 = np.radians([lat1, lon1])
    p2 = np.radians([lat2, lon2])
    # Angular distance
    delta = 2 * np.arcsin(np.sqrt(
        np.sin((p2[0] - p1[0]) / 2) ** 2
        + np.cos(p1[0]) * np.cos(p2[0]) * np.sin((p2[1] - p1[1]) / 2) ** 2
    ))
    if delta == 0:
        return np.array([lat1]), np.array([lon1])
    f = np.linspace(0, 1, n)
    A = np.sin((1 - f) * delta) / np.sin(delta)
    B = np.sin(f * delta) / np.sin(delta)
    x = A * np.cos(p1[0]) * np.cos(p1[1]) + B * np.cos(p2[0]) * np.cos(p2[1])
    y = A * np.cos(p1[0]) * np.sin(p1[1]) + B * np.cos(p2[0]) * np.sin(p2[1])
    z = A * np.sin(p1[0]) + B * np.sin(p2[0])
    lat = np.degrees(np.arctan2(z, np.sqrt(x ** 2 + y ** 2)))
    lon = np.degrees(np.arctan2(y, x))
    return lat, lon


def trade_flow_arc_map(
    flows: pd.DataFrame,
    *,
    src_lat: str = "src_lat", src_lon: str = "src_lon",
    dst_lat: str = "dst_lat", dst_lon: str = "dst_lon",
    value: str = "value",
    src_name: str = "src_name", dst_name: str = "dst_name",
    height: int = 560,
) -> go.Figure:
    """Plotly natural-earth great-circle arc map. Matches the choropleth pages.

    Lines are binned into 4 value-tiers; each tier rendered as one Scattergeo
    trace (single line width / opacity per tier). Endpoint dots overlaid in
    a final trace.
    """
    fig = go.Figure()

    # Apply the same dark globe styling used by the choropleth pages
    fig.update_geos(
        showcoastlines=False, showland=True, landcolor=PALETTE["panel"],
        showocean=True, oceancolor=PALETTE["bg"],
        showcountries=True, countrycolor=PALETTE["border"], countrywidth=0.4,
        showframe=False, projection_type="natural earth",
        bgcolor="rgba(0,0,0,0)",
    )
    fig.update_layout(
        height=height,
        margin=dict(l=0, r=0, t=10, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, system-ui, sans-serif", color=PALETTE["text"]),
        showlegend=False,
        hoverlabel=dict(bgcolor=PALETTE["panel_alt"], bordercolor=PALETTE["border"]),
    )

    if flows is None or len(flows) == 0:
        return fig

    f = flows.dropna(subset=[src_lat, src_lon, dst_lat, dst_lon, value]).copy()
    f = f[f[value] > 0]
    if f.empty:
        return fig

    # Bin into 4 tiers by log-value
    log_v = np.log10(f[value].to_numpy())
    if log_v.max() == log_v.min():
        f["_tier"] = 0
        n_tiers = 1
    else:
        # qcut may yield fewer bins if many ties; allow duplicates="drop"
        try:
            f["_tier"] = pd.qcut(log_v, q=4, labels=False, duplicates="drop")
        except ValueError:
            f["_tier"] = 0
        n_tiers = int(f["_tier"].max()) + 1

    # Visual params per tier (low → high value)
    widths_by_tier    = [0.6, 1.2, 2.2, 3.2]
    opacity_by_tier   = [0.30, 0.50, 0.75, 0.95]
    color_by_tier     = [PALETTE["neutral"], "#7DD3FC", PALETTE["accent_soft"], PALETTE["accent"]]

    # Each tier → one scattergeo line trace with multiple segments separated by None
    for tier in range(n_tiers):
        sub = f[f["_tier"] == tier]
        if sub.empty:
            continue
        lats: list = []
        lons: list = []
        hovers: list = []
        for r in sub.itertuples(index=False):
            la, lo = _great_circle_points(
                getattr(r, src_lat), getattr(r, src_lon),
                getattr(r, dst_lat), getattr(r, dst_lon),
            )
            lats.extend(la.tolist() + [None])
            lons.extend(lo.tolist() + [None])
            label = (f"{getattr(r, src_name)} → {getattr(r, dst_name)}<br>"
                     f"${getattr(r, value):,.0f}")
            hovers.extend([label] * len(la) + [None])
        idx = min(tier, len(widths_by_tier) - 1)
        fig.add_trace(go.Scattergeo(
            mode="lines",
            lon=lons, lat=lats,
            line=dict(
                width=widths_by_tier[idx],
                color=color_by_tier[idx],
            ),
            opacity=opacity_by_tier[idx],
            hoverinfo="text",
            text=hovers,
            name=f"Tier {tier + 1}",
        ))

    # Endpoint markers
    src_pts = f[[src_lat, src_lon, src_name]].rename(
        columns={src_lat: "lat", src_lon: "lon", src_name: "name"}
    )
    dst_pts = f[[dst_lat, dst_lon, dst_name]].rename(
        columns={dst_lat: "lat", dst_lon: "lon", dst_name: "name"}
    )
    pts = (pd.concat([src_pts, dst_pts], ignore_index=True)
             .drop_duplicates(subset=["lat", "lon"]))
    fig.add_trace(go.Scattergeo(
        mode="markers",
        lon=pts["lon"], lat=pts["lat"],
        marker=dict(size=4, color=PALETTE["text"], opacity=0.7,
                    line=dict(width=0)),
        hoverinfo="text",
        text=pts["name"],
        showlegend=False,
    ))

    return fig


# News chart builders ───────────────────────────────────────────────────────
def signal_bar(df: pd.DataFrame, title: str = "Trade signal mix") -> go.Figure:
    """Horizontal bar of signal → article count."""
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No tagged signals in current selection",
                           showarrow=False,
                           font=dict(color=PALETTE["text_muted"]))
        return _apply_layout(fig, height=280, show_legend=False)
    df = df.sort_values("articles")
    fig = go.Figure(go.Bar(
        x=df["articles"], y=df["signal"],
        orientation="h",
        marker_color=PALETTE["accent"],
        hovertemplate="<b>%{y}</b><br>%{x:,} articles<extra></extra>",
    ))
    fig.update_layout(title=title)
    return _apply_layout(fig, height=max(260, 26 * len(df)), show_legend=False)


def sentiment_donut(df: pd.DataFrame, title: str = "Sentiment mix") -> go.Figure:
    """Donut showing sentiment distribution. Unlabeled gets muted color."""
    if df.empty or df["articles"].sum() == 0:
        fig = go.Figure()
        fig.add_annotation(text="No data", showarrow=False,
                           font=dict(color=PALETTE["text_muted"]))
        return _apply_layout(fig, height=280, show_legend=False)
    colors_map = {
        "positive":  PALETTE["pos"],
        "negative":  PALETTE["neg"],
        "neutral":   "#60A5FA",
        "unlabeled": PALETTE["neutral"],
    }
    colors = [colors_map.get(s, PALETTE["accent"]) for s in df["sentiment"]]
    fig = go.Figure(go.Pie(
        labels=df["sentiment"], values=df["articles"],
        hole=0.55,
        marker=dict(colors=colors, line=dict(color=PALETTE["bg"], width=2)),
        textinfo="label+percent",
        hovertemplate="<b>%{label}</b><br>%{value:,} articles (%{percent})<extra></extra>",
    ))
    fig.update_layout(title=title)
    return _apply_layout(fig, height=320, show_legend=False)


def news_timeline_chart(df: pd.DataFrame, title: str = "Coverage volume over time") -> go.Figure:
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No coverage in current selection",
                           showarrow=False,
                           font=dict(color=PALETTE["text_muted"]))
        return _apply_layout(fig, height=280, show_legend=False)
    fig = go.Figure(go.Scatter(
        x=df["period"], y=df["articles"],
        mode="lines+markers",
        line=dict(color=PALETTE["accent"], width=2.5),
        marker=dict(size=5, color=PALETTE["accent"]),
        fill="tozeroy",
        fillcolor="rgba(94, 234, 212, 0.12)",
        hovertemplate="<b>%{x|%b %Y}</b><br>%{y:,} articles<extra></extra>",
    ))
    fig.update_layout(title=title)
    return _apply_layout(fig, height=300, show_legend=False)


# Quadrant scatter for structural vs news risk ─────────────────────────────
def risk_quadrant_scatter(df: pd.DataFrame,
                          x: str = "structural_risk",
                          y: str = "news_risk_score",
                          name: str = "reporter_desc",
                          size: str | None = None,
                          x_label: str = "Structural risk",
                          y_label: str = "News risk",
                          title: str = "") -> go.Figure:
    """Quadrant scatter: structural risk on X, news risk on Y.

    Median lines split the plot into four quadrants:
        Q1 (top-left):     low structural, high news    → "Noisy but resilient"
        Q2 (top-right):    high structural, high news   → "In the storm"
        Q3 (bottom-left):  low structural, low news     → "Stable"
        Q4 (bottom-right): high structural, low news    → "Quietly fragile"

    Points in Q2 and Q4 get labels (outliers and structurally interesting);
    other points stay unlabeled to keep the chart readable.
    """
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No data", showarrow=False,
                           font=dict(color=PALETTE["text_muted"]))
        return _apply_layout(fig, height=520, show_legend=False)

    df = df.dropna(subset=[x, y]).copy()
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No data", showarrow=False,
                           font=dict(color=PALETTE["text_muted"]))
        return _apply_layout(fig, height=520, show_legend=False)

    x_med = df[x].median()
    y_med = df[y].median()

    # Quadrant assignment + color
    def quadrant(row):
        right = row[x] >= x_med
        top   = row[y] >= y_med
        if top and right:      return "In the storm"          # Q2
        if top and not right:  return "Noisy but resilient"   # Q1
        if not top and right:  return "Quietly fragile"       # Q4
        return "Stable"                                       # Q3

    df["_quad"] = df.apply(quadrant, axis=1)
    quad_colors = {
        "In the storm":        PALETTE["neg"],
        "Quietly fragile":     "#FBBF24",
        "Noisy but resilient": "#60A5FA",
        "Stable":              PALETTE["pos"],
    }
    df["_color"] = df["_quad"].map(quad_colors)

    # Show labels for points in "In the storm" and "Quietly fragile" — the
    # two quadrants worth drawing the eye to. Plus any extreme outliers.
    x_p90 = df[x].quantile(0.85)
    y_p90 = df[y].quantile(0.85)
    label_mask = (
        df["_quad"].isin(["In the storm", "Quietly fragile"])
        | (df[x] >= x_p90) | (df[y] >= y_p90)
    )

    # Marker size
    if size and size in df.columns and df[size].sum() > 0:
        s = df[size].fillna(0).to_numpy()
        if s.max() > 0:
            marker_size = 8 + 22 * (s / s.max())
        else:
            marker_size = [12] * len(df)
    else:
        marker_size = [13] * len(df)

    fig = go.Figure()

    # Quadrant shading — very subtle
    fig.add_shape(type="rect", x0=df[x].min() - 5, x1=x_med,
                  y0=y_med, y1=df[y].max() + 5,
                  fillcolor="rgba(96, 165, 250, 0.04)", line_width=0, layer="below")
    fig.add_shape(type="rect", x0=x_med, x1=df[x].max() + 5,
                  y0=y_med, y1=df[y].max() + 5,
                  fillcolor="rgba(248, 113, 113, 0.06)", line_width=0, layer="below")
    fig.add_shape(type="rect", x0=df[x].min() - 5, x1=x_med,
                  y0=df[y].min() - 5, y1=y_med,
                  fillcolor="rgba(52, 211, 153, 0.04)", line_width=0, layer="below")
    fig.add_shape(type="rect", x0=x_med, x1=df[x].max() + 5,
                  y0=df[y].min() - 5, y1=y_med,
                  fillcolor="rgba(251, 191, 36, 0.05)", line_width=0, layer="below")

    # Median lines
    fig.add_vline(x=x_med, line=dict(color=PALETTE["border"], width=1, dash="dot"))
    fig.add_hline(y=y_med, line=dict(color=PALETTE["border"], width=1, dash="dot"))

    # Quadrant labels in corners
    pad_x = (df[x].max() - df[x].min()) * 0.02
    pad_y = (df[y].max() - df[y].min()) * 0.02
    annotations_corner = [
        dict(x=df[x].min() + pad_x, y=df[y].max() - pad_y,
             text="Noisy but resilient", showarrow=False, xanchor="left",
             font=dict(size=10, color="#60A5FA"), opacity=0.7),
        dict(x=df[x].max() - pad_x, y=df[y].max() - pad_y,
             text="In the storm", showarrow=False, xanchor="right",
             font=dict(size=10, color=PALETTE["neg"]), opacity=0.7),
        dict(x=df[x].min() + pad_x, y=df[y].min() + pad_y,
             text="Stable", showarrow=False, xanchor="left",
             font=dict(size=10, color=PALETTE["pos"]), opacity=0.7),
        dict(x=df[x].max() - pad_x, y=df[y].min() + pad_y,
             text="Quietly fragile", showarrow=False, xanchor="right",
             font=dict(size=10, color="#FBBF24"), opacity=0.7),
    ]
    for ann in annotations_corner:
        fig.add_annotation(**ann)

    # Points
    fig.add_trace(go.Scatter(
        x=df[x], y=df[y],
        mode="markers+text",
        text=[n if m else "" for n, m in zip(df[name], label_mask)],
        textposition="top center",
        textfont=dict(size=10, color=PALETTE["text"]),
        marker=dict(
            size=marker_size,
            color=df["_color"],
            opacity=0.85,
            line=dict(width=0.5, color=PALETTE["border"]),
        ),
        customdata=df[[name, "_quad"]].to_numpy(),
        hovertemplate=(
            "<b>%{customdata[0]}</b><br>"
            f"{x_label}: %{{x:.1f}}<br>"
            f"{y_label}: %{{y:.1f}}<br>"
            "Quadrant: %{customdata[1]}<extra></extra>"
        ),
        showlegend=False,
    ))

    fig.update_layout(
        title=title,
        xaxis=dict(title=x_label),
        yaxis=dict(title=y_label),
    )
    return _apply_layout(fig, height=540, show_legend=False)