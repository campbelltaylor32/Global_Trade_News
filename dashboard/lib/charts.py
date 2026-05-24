"""Reusable chart builders. Keep chart styling in one place so every page
inherits the same look."""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pydeck as pdk

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
def trade_flow_arc_map(
    flows: pd.DataFrame,
    *,
    src_lat: str = "src_lat", src_lon: str = "src_lon",
    dst_lat: str = "dst_lat", dst_lon: str = "dst_lon",
    value: str = "value",
    src_name: str = "src_name", dst_name: str = "dst_name",
    height: int = 560,
) -> pdk.Deck:
    """ArcLayer flow map. Width scales with log-value, color encodes magnitude."""
    if flows.empty:
        # Empty map with no arcs
        view_state = pdk.ViewState(latitude=20, longitude=0, zoom=1.2, pitch=30)
        return pdk.Deck(layers=[], initial_view_state=view_state,
                        map_style="mapbox://styles/mapbox/dark-v10", height=height)

    f = flows.dropna(subset=[src_lat, src_lon, dst_lat, dst_lon, value]).copy()
    f = f[f[value] > 0]
    if f.empty:
        view_state = pdk.ViewState(latitude=20, longitude=0, zoom=1.2, pitch=30)
        return pdk.Deck(layers=[], initial_view_state=view_state,
                        map_style="mapbox://styles/mapbox/dark-v10", height=height)

    # Log-scale width
    vmin, vmax = f[value].min(), f[value].max()
    if vmax == vmin:
        f["_width"] = 2.5
    else:
        log_vals = np.log10(f[value])
        lmin, lmax = log_vals.min(), log_vals.max()
        f["_width"] = 1.0 + 7.0 * (log_vals - lmin) / max(lmax - lmin, 1e-9)

    # Source: teal, target: amber — high contrast on dark map
    f["src_color"] = [[94, 234, 212, 180]] * len(f)
    f["dst_color"] = [[251, 191, 36, 180]] * len(f)

    arc = pdk.Layer(
        "ArcLayer",
        data=f,
        get_source_position=[src_lon, src_lat],
        get_target_position=[dst_lon, dst_lat],
        get_source_color="src_color",
        get_target_color="dst_color",
        get_width="_width",
        pickable=True,
        auto_highlight=True,
        great_circle=True,
    )

    # Origin/destination markers (small subtle dots)
    nodes_src = f[[src_lat, src_lon, src_name]].drop_duplicates().rename(
        columns={src_lat: "lat", src_lon: "lon", src_name: "name"})
    nodes_dst = f[[dst_lat, dst_lon, dst_name]].drop_duplicates().rename(
        columns={dst_lat: "lat", dst_lon: "lon", dst_name: "name"})
    nodes = pd.concat([nodes_src, nodes_dst], ignore_index=True).drop_duplicates()

    scatter = pdk.Layer(
        "ScatterplotLayer",
        data=nodes,
        get_position=["lon", "lat"],
        get_radius=40000,
        get_fill_color=[226, 232, 240, 200],
        pickable=False,
        stroked=False,
    )

    view_state = pdk.ViewState(latitude=20, longitude=10, zoom=1.2, pitch=35, bearing=0)

    return pdk.Deck(
        layers=[arc, scatter],
        initial_view_state=view_state,
        map_style="mapbox://styles/mapbox/dark-v10",
        tooltip={
            "html": (
                "<b>{" + src_name + "} → {" + dst_name + "}</b><br/>"
                "Value: $${" + value + "}"
            ),
            "style": {
                "backgroundColor": PALETTE["panel"],
                "color": PALETTE["text"],
                "border": f"1px solid {PALETTE['border']}",
                "borderRadius": "8px",
                "padding": "8px",
            },
        },
        height=height,
    )
