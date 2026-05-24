"""Shared styling, CSS injection, and small UI primitives (KPI cards, dividers).

Keep all visual constants here so pages stay declarative.
"""
from __future__ import annotations

import streamlit as st

# --- Palette -----------------------------------------------------------------
# Muted, executive-feel colors. Teal accent, neutral backgrounds, semantic
# red/green for deltas only (never as primary chart colors).
PALETTE = {
    "bg":           "#0B1220",
    "panel":        "#111B2E",
    "panel_alt":    "#16223A",
    "border":       "#1F2C45",
    "text":         "#E2E8F0",
    "text_muted":   "#94A3B8",
    "accent":       "#5EEAD4",   # teal — used sparingly
    "accent_soft":  "#2DD4BF",
    "pos":          "#34D399",
    "neg":          "#F87171",
    "neutral":      "#64748B",
}

# Sequential palette for choropleths / heatmaps (low → high)
SEQUENTIAL = [
    "#0F1B2D", "#16314B", "#1B4869", "#1F6086", "#2179A3",
    "#2192C0", "#21ACDC", "#5EC7E4", "#9FDCEC", "#D9F1F7",
]

# Diverging palette for growth (neg → 0 → pos)
DIVERGING = [
    "#B91C1C", "#DC2626", "#F87171", "#FCA5A5",
    "#1F2C45",
    "#6EE7B7", "#34D399", "#10B981", "#047857",
]

# Categorical palette for series like flow direction, top-N, etc.
CATEGORICAL = [
    "#5EEAD4", "#60A5FA", "#FBBF24", "#F472B6",
    "#A78BFA", "#FB923C", "#34D399", "#94A3B8",
]


def inject_css() -> None:
    """Inject global CSS once per page. Call at the top of every page."""
    st.markdown(
        f"""
        <style>
            /* Tighten the main container */
            .block-container {{
                padding-top: 2rem;
                padding-bottom: 3rem;
                max-width: 1400px;
            }}

            /* Headings */
            h1, h2, h3 {{ letter-spacing: -0.02em; font-weight: 600; }}
            h1 {{ font-size: 1.9rem; margin-bottom: 0.2rem; }}
            h2 {{ font-size: 1.3rem; margin-top: 1.5rem; }}
            h3 {{ font-size: 1.0rem; color: {PALETTE['text_muted']}; }}

            /* KPI card */
            .kpi-card {{
                background: {PALETTE['panel']};
                border: 1px solid {PALETTE['border']};
                border-radius: 12px;
                padding: 18px 20px;
                height: 100%;
            }}
            .kpi-label {{
                color: {PALETTE['text_muted']};
                font-size: 0.78rem;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin-bottom: 6px;
            }}
            .kpi-value {{
                font-size: 1.6rem;
                font-weight: 600;
                color: {PALETTE['text']};
                line-height: 1.1;
            }}
            .kpi-delta-pos {{ color: {PALETTE['pos']}; font-size: 0.85rem; }}
            .kpi-delta-neg {{ color: {PALETTE['neg']}; font-size: 0.85rem; }}
            .kpi-delta-neu {{ color: {PALETTE['text_muted']}; font-size: 0.85rem; }}

            /* Subtle section caption */
            .caption {{
                color: {PALETTE['text_muted']};
                font-size: 0.85rem;
                margin-top: -0.4rem;
                margin-bottom: 1rem;
            }}

            /* Section divider */
            .section-rule {{
                height: 1px;
                background: linear-gradient(90deg, {PALETTE['border']} 0%, transparent 100%);
                margin: 1.2rem 0 0.6rem;
            }}

            /* Hide Streamlit chrome that doesn't fit the executive look */
            #MainMenu {{ visibility: hidden; }}
            footer {{ visibility: hidden; }}

            /* Sidebar polish */
            section[data-testid="stSidebar"] {{
                background: {PALETTE['panel']};
                border-right: 1px solid {PALETTE['border']};
            }}

            /* Tabs */
            .stTabs [data-baseweb="tab-list"] {{ gap: 8px; }}
            .stTabs [data-baseweb="tab"] {{
                background: transparent;
                border-radius: 8px 8px 0 0;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def kpi_card(label: str, value: str, delta: str | None = None, sign: int = 0) -> str:
    """Return HTML for a KPI card. Use inside st.markdown(..., unsafe_allow_html=True).

    sign: +1 positive (green), -1 negative (red), 0 neutral.
    """
    if delta is None:
        delta_html = ""
    else:
        cls = "kpi-delta-pos" if sign > 0 else "kpi-delta-neg" if sign < 0 else "kpi-delta-neu"
        delta_html = f'<div class="{cls}">{delta}</div>'
    return (
        f'<div class="kpi-card">'
        f'<div class="kpi-label">{label}</div>'
        f'<div class="kpi-value">{value}</div>'
        f'{delta_html}'
        f'</div>'
    )


def section_rule() -> None:
    st.markdown('<div class="section-rule"></div>', unsafe_allow_html=True)


def caption(text: str) -> None:
    st.markdown(f'<div class="caption">{text}</div>', unsafe_allow_html=True)


# --- Number formatting -------------------------------------------------------
def fmt_money(x: float) -> str:
    """Format USD with adaptive suffix."""
    if x is None or (isinstance(x, float) and (x != x)):
        return "—"
    a = abs(x)
    if a >= 1e12: return f"${x/1e12:,.2f}T"
    if a >= 1e9:  return f"${x/1e9:,.2f}B"
    if a >= 1e6:  return f"${x/1e6:,.2f}M"
    if a >= 1e3:  return f"${x/1e3:,.1f}K"
    return f"${x:,.0f}"


def fmt_pct(x: float, digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and (x != x)):
        return "—"
    return f"{x*100:+.{digits}f}%"


def fmt_int(x: float) -> str:
    if x is None or (isinstance(x, float) and (x != x)):
        return "—"
    return f"{int(x):,}"
