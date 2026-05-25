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

# --- Pages registry — single source of truth for the custom sidebar nav -----
PAGES = [
    {"path": "app.py",                            "label": "Global Overview",       "icon": "🌐"},
    {"path": "pages/1_Trade_Flows.py",            "label": "Trade Flows",           "icon": "🛰"},
    {"path": "pages/2_Country_Profile.py",        "label": "Country Profile",       "icon": "📍"},
    {"path": "pages/3_Commodity_Explorer.py",     "label": "Commodity Explorer",    "icon": "📦"},
    {"path": "pages/4_Concentration_Risk.py",     "label": "Concentration & Risk",  "icon": "⚠"},
    {"path": "pages/5_Backend_Test.py",           "label": "AI Trade Analysis",     "icon": "🤖"},
]

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

            /* ── Sidebar ──────────────────────────────────────────── */
            section[data-testid="stSidebar"] {{
                background: {PALETTE['panel']};
                border-right: 1px solid {PALETTE['border']};
            }}
            section[data-testid="stSidebar"] > div {{
                padding-top: 1.25rem;
                padding-bottom: 2rem;
            }}
            /* Hide Streamlit's auto-generated multipage nav — we render our own */
            section[data-testid="stSidebar"] ul[data-testid="stSidebarNavItems"],
            section[data-testid="stSidebar"] div[data-testid="stSidebarNav"] {{
                display: none !important;
            }}

            /* ── Project header (top of sidebar) ─────────────────── */
            .sb-project {{
                padding: 4px 4px 0 4px;
            }}
            .sb-project-label {{
                color: {PALETTE['text_muted']};
                font-size: 0.66rem;
                text-transform: uppercase;
                letter-spacing: 0.14em;
                margin-bottom: 8px;
                font-weight: 600;
            }}
            .sb-project-body {{
                color: {PALETTE['text']};
                font-size: 0.82rem;
                line-height: 1.5;
                margin-bottom: 10px;
            }}

            /* ── Brand block ─────────────────────────────────────── */
            .sb-brand {{
                display: flex; align-items: center; gap: 8px;
                padding: 0 4px 6px 4px;
            }}
            .sb-brand-accent {{
                width: 4px; height: 22px;
                background: {PALETTE['accent']};
                border-radius: 2px;
                margin-right: 4px;
            }}
            .sb-brand-title {{
                font-size: 1.05rem; font-weight: 600; letter-spacing: -0.01em;
                color: {PALETTE['text']};
            }}
            .sb-brand-sub {{
                color: {PALETTE['text_muted']};
                font-size: 0.7rem;
                letter-spacing: 0.1em;
                text-transform: uppercase;
                padding: 0 4px 10px 16px;
            }}
            .sb-brand-blurb {{
                color: {PALETTE['text_muted']};
                font-size: 0.82rem;
                line-height: 1.5;
                padding: 4px 4px 4px 16px;
            }}

            /* ── Section label ───────────────────────────────────── */
            .sb-section-label {{
                color: {PALETTE['text_muted']};
                font-size: 0.66rem;
                text-transform: uppercase;
                letter-spacing: 0.14em;
                padding: 0 4px 10px 4px;
                font-weight: 600;
            }}

            /* ── Divider ─────────────────────────────────────────── */
            .sb-divider {{
                height: 1px;
                background: {PALETTE['border']};
                margin: 18px 0;
                opacity: 0.8;
            }}

            /* ── Nav links ───────────────────────────────────────── */
            section[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"] {{
                display: flex; align-items: center;
                padding: 9px 12px;
                margin: 3px 0;
                border-radius: 8px;
                border: 1px solid transparent;
                color: {PALETTE['text_muted']} !important;
                font-size: 0.92rem;
                font-weight: 500;
                transition: background 0.12s ease, color 0.12s ease, border-color 0.12s ease;
                text-decoration: none !important;
            }}
            section[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"]:hover {{
                background: {PALETTE['panel_alt']};
                color: {PALETTE['text']} !important;
                border-color: {PALETTE['border']};
            }}
            /* Current page — Streamlit adds aria-current="page" */
            section[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"][aria-current="page"] {{
                background: {PALETTE['panel_alt']};
                color: {PALETTE['accent']} !important;
                border-color: {PALETTE['border']};
                box-shadow: inset 3px 0 0 {PALETTE['accent']};
            }}
            section[data-testid="stSidebar"] a[data-testid="stPageLink-NavLink"] p {{
                margin: 0 !important;
                font-size: 0.92rem !important;
                color: inherit !important;
            }}

            /* ── Version pill ────────────────────────────────────── */
            .sb-pill {{
                display: inline-block;
                padding: 2px 10px;
                font-size: 0.68rem;
                background: {PALETTE['panel_alt']};
                color: {PALETTE['accent']};
                border: 1px solid {PALETTE['border']};
                border-radius: 999px;
                font-weight: 500;
            }}

            /* ── Hero banner (body landing card) ────────────────────── */
            .hero {{
                background: linear-gradient(135deg,
                    {PALETTE['panel']} 0%,
                    {PALETTE['panel_alt']} 100%);
                border: 1px solid {PALETTE['border']};
                border-radius: 14px;
                padding: 22px 26px 20px 26px;
                margin: 4px 0 22px 0;
                position: relative;
                overflow: hidden;
            }}
            .hero::before {{
                content: "";
                position: absolute;
                left: 0; top: 0; bottom: 0;
                width: 4px;
                background: {PALETTE['accent']};
            }}
            .hero-eyebrow {{
                color: {PALETTE['accent']};
                font-size: 0.72rem;
                text-transform: uppercase;
                letter-spacing: 0.12em;
                margin-bottom: 6px;
                font-weight: 600;
            }}
            .hero-title {{
                color: {PALETTE['text']};
                font-size: 1.5rem;
                font-weight: 600;
                line-height: 1.2;
                margin-bottom: 8px;
                letter-spacing: -0.02em;
            }}
            .hero-tagline {{
                color: {PALETTE['text_muted']};
                font-size: 0.95rem;
                line-height: 1.5;
                margin-bottom: 16px;
                max-width: 720px;
            }}
            .hero-guide {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 10px 18px;
                margin-top: 6px;
            }}
            .hero-guide-item {{
                font-size: 0.82rem;
                line-height: 1.4;
                color: {PALETTE['text_muted']};
            }}
            .hero-guide-item strong {{
                color: {PALETTE['text']};
                font-weight: 600;
                display: block;
                margin-bottom: 2px;
            }}

            /* Questions block — sits below the page guide */
            .hero-questions {{
                margin-top: 18px;
                padding-top: 16px;
                border-top: 1px dashed {PALETTE['border']};
            }}
            .hero-questions-label {{
                color: {PALETTE['accent']};
                font-size: 0.7rem;
                text-transform: uppercase;
                letter-spacing: 0.12em;
                font-weight: 600;
                margin-bottom: 10px;
            }}
            .hero-q-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 8px 22px;
            }}
            .hero-q-item {{
                display: flex; align-items: flex-start; gap: 10px;
                font-size: 0.84rem;
                line-height: 1.45;
                color: {PALETTE['text']};
            }}
            .hero-q-num {{
                color: {PALETTE['accent']};
                font-weight: 600;
                font-size: 0.78rem;
                min-width: 14px;
                margin-top: 1px;
            }}

            /* About expander */
            .about-primary {{
                background: {PALETTE['panel_alt']};
                border-left: 3px solid {PALETTE['accent']};
                border-radius: 6px;
                padding: 10px 14px;
                margin-bottom: 14px;
            }}
            .about-primary-label {{
                color: {PALETTE['accent']};
                font-size: 0.66rem;
                text-transform: uppercase;
                letter-spacing: 0.12em;
                font-weight: 600;
                margin-bottom: 4px;
            }}
            .about-primary-text {{
                color: {PALETTE['text']};
                font-size: 0.92rem;
                line-height: 1.45;
            }}
            .about-sub-label {{
                color: {PALETTE['text_muted']};
                font-size: 0.66rem;
                text-transform: uppercase;
                letter-spacing: 0.12em;
                font-weight: 600;
                margin: 8px 0 8px 0;
            }}
            .about-table {{
                display: flex; flex-direction: column;
            }}
            .about-row {{
                display: grid;
                grid-template-columns: 1fr auto;
                gap: 16px;
                padding: 7px 0;
                border-top: 1px solid {PALETTE['border']};
                font-size: 0.84rem;
                line-height: 1.4;
                align-items: baseline;
            }}
            .about-row:first-child {{
                border-top: none;
                padding-top: 2px;
            }}
            .about-q {{ color: {PALETTE['text']}; }}
            .about-where {{
                color: {PALETTE['text_muted']};
                font-size: 0.78rem;
                white-space: nowrap;
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


# ---------------------------------------------------------------------------
# Sidebar — call render_sidebar() at the top of every page (after inject_css).
# Builds a consistent branded nav, hides Streamlit's auto multipage list, and
# pins a footer with metadata.
# ---------------------------------------------------------------------------
def render_sidebar() -> None:
    """Render the custom sidebar. Idempotent within a page run."""
    with st.sidebar:
        # ── Project header (top of sidebar) ────────────────────────
        st.markdown(
            '<div class="sb-project">'
            '<div class="sb-project-label">Project</div>'
            '<div class="sb-project-body">'
            "ADSP 31011 · Cloud-Native Data Engineering"
            "<br/>UChicago · MS Applied Data Science"
            '</div>'
            '<div class="sb-pill">v1.0 demo</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        st.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

        # ── Brand ─────────────────────────────────────────────────
        st.markdown(
            '<div class="sb-brand">'
            '<div class="sb-brand-accent"></div>'
            '<div class="sb-brand-title">Trade Risk Ledger</div>'
            '</div>'
            '<div class="sb-brand-sub">UN Comtrade · News Signals</div>'
            '<div class="sb-brand-blurb">'
            "A view of global trade — where the volume is, where it's "
            "shifting, and where the headlines are coming from."
            '</div>',
            unsafe_allow_html=True,
        )

        st.markdown('<div class="sb-divider"></div>', unsafe_allow_html=True)

        # ── Nav ───────────────────────────────────────────────────
        st.markdown(
            '<div class="sb-section-label">Navigate</div>',
            unsafe_allow_html=True,
        )

        for p in PAGES:
            try:
                st.page_link(p["path"], label=p["label"], icon=p["icon"])
            except Exception:
                pass


def hero_banner(
    eyebrow: str,
    title: str,
    tagline: str,
    guide_items: list[tuple[str, str]] | None = None,
    questions: list[str] | None = None,
) -> None:
    """Render a body hero banner. Use on landing / overview pages.

    guide_items: optional list of (label, description) tuples shown as a
        responsive grid below the tagline — meant for explaining what each
        page of the dashboard answers.
    questions: optional list of business questions the dashboard answers,
        rendered as a numbered grid below the guide. Use on the Overview
        page to make the rubric questions explicit.
    """
    guide_html = ""
    if guide_items:
        items_html = "".join(
            f'<div class="hero-guide-item"><strong>{label}</strong>{desc}</div>'
            for label, desc in guide_items
        )
        guide_html = f'<div class="hero-guide">{items_html}</div>'

    questions_html = ""
    if questions:
        q_items_html = "".join(
            f'<div class="hero-q-item">'
            f'<span class="hero-q-num">{i + 1:02d}</span>'
            f'<span>{q}</span>'
            f'</div>'
            for i, q in enumerate(questions)
        )
        questions_html = (
            '<div class="hero-questions">'
            '<div class="hero-questions-label">Business questions this dashboard answers</div>'
            f'<div class="hero-q-grid">{q_items_html}</div>'
            '</div>'
        )

    st.markdown(
        f'<div class="hero">'
        f'<div class="hero-eyebrow">{eyebrow}</div>'
        f'<div class="hero-title">{title}</div>'
        f'<div class="hero-tagline">{tagline}</div>'
        f'{guide_html}'
        f'{questions_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


def about_expander(
    primary_question: str,
    sub_questions: list[tuple[str, str]],
    expanded: bool = False,
) -> None:
    """Render an 'About this page' expander listing the business questions
    answered, each linked to the chart name where the answer lives.

    Args:
        primary_question: the top-level question this page owns.
        sub_questions: list of (question, chart_or_section) tuples.
        expanded: whether the expander starts open. Defaults to closed.
    """
    rows_html = "".join(
        f'<div class="about-row">'
        f'<div class="about-q">{q}</div>'
        f'<div class="about-where">{where}</div>'
        f'</div>'
        for q, where in sub_questions
    )
    body_html = (
        f'<div class="about-primary">'
        f'<div class="about-primary-label">Primary question</div>'
        f'<div class="about-primary-text">{primary_question}</div>'
        f'</div>'
        f'<div class="about-sub-label">This page also answers</div>'
        f'<div class="about-table">{rows_html}</div>'
    )
    with st.expander("About this page · business questions answered",
                     expanded=expanded):
        st.markdown(body_html, unsafe_allow_html=True)