"""Backend Test — natural-language chat against the trade warehouse.

Talks to a FastAPI service that translates chat messages to SQL and queries
the warehouse. Visual styling matches the rest of the dashboard (teal accent,
dark panel backgrounds, IBM Plex Sans).
"""
from __future__ import annotations

import requests
import streamlit as st

from lib.style import (
    inject_css, render_sidebar, caption, section_rule, PALETTE,
)

# Backend URL — override via Streamlit secrets [backend] url or env if needed
try:
    API_URL = st.secrets["backend"]["url"]
except Exception:
    API_URL = "http://localhost:8000"

st.set_page_config(
    page_title="AI Trade Analysis",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject_css()
render_sidebar()

# Page-local CSS — just the bits that aren't covered by the global theme
st.markdown(
    f"""
    <style>
        /* Status pill */
        .be-status {{
            display: inline-flex; align-items: center; gap: 6px;
            font-size: 0.72rem;
            padding: 4px 12px;
            border-radius: 999px;
            font-weight: 500;
            margin-bottom: 0.5rem;
        }}
        .be-status-ok {{
            background: rgba(52, 211, 153, 0.10);
            color: {PALETTE['pos']};
            border: 1px solid rgba(52, 211, 153, 0.35);
        }}
        .be-status-fail {{
            background: rgba(248, 113, 113, 0.10);
            color: {PALETTE['neg']};
            border: 1px solid rgba(248, 113, 113, 0.35);
        }}
        .be-status-dot {{
            width: 6px; height: 6px; border-radius: 50%;
            background: currentColor;
        }}

        /* Chat bubbles */
        .be-bubble {{
            border-radius: 10px;
            padding: 12px 16px;
            margin: 6px 0 14px 0;
            font-size: 0.92rem;
            line-height: 1.55;
        }}
        .be-bubble-user {{
            background: rgba(94, 234, 212, 0.06);
            border: 1px solid rgba(94, 234, 212, 0.20);
            color: {PALETTE['text']};
        }}
        .be-bubble-assistant {{
            background: {PALETTE['panel']};
            border: 1px solid {PALETTE['border']};
            color: {PALETTE['text']};
            white-space: pre-wrap;
            font-family: ui-monospace, "SF Mono", Menlo, Monaco, "Cascadia Code", monospace;
            font-size: 0.85rem;
        }}
        .be-role {{
            font-size: 0.66rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: {PALETTE['text_muted']};
            font-weight: 600;
            margin-top: 6px;
        }}
    </style>
    """,
    unsafe_allow_html=True,
)

# ─── Header ────────────────────────────────────────────────────────────────
st.title("AI Trade Analysis")
caption(
    "Natural-language chat against the trade warehouse. Routed through a "
    "MCP Connector that translates the message, queries MySQL, and returns "
    "a formatted answer."
)

# ─── Health check ──────────────────────────────────────────────────────────
hc_col, _ = st.columns([1, 4])
backend_ok = False
status_html = ""
try:
    r = requests.get(f"{API_URL}/health", timeout=3)
    if r.status_code == 200:
        backend_ok = True
        status_html = (
            '<div class="be-status be-status-ok">'
            '<span class="be-status-dot"></span>Backend online'
            '</div>'
        )
    else:
        status_html = (
            '<div class="be-status be-status-fail">'
            f'<span class="be-status-dot"></span>Backend error · HTTP {r.status_code}'
            '</div>'
        )
except requests.exceptions.ConnectionError:
    status_html = (
        '<div class="be-status be-status-fail">'
        '<span class="be-status-dot"></span>Backend offline · is uvicorn running?'
        '</div>'
    )
except Exception as e:
    status_html = (
        '<div class="be-status be-status-fail">'
        f'<span class="be-status-dot"></span>{str(e)[:80]}'
        '</div>'
    )
hc_col.markdown(status_html, unsafe_allow_html=True)

section_rule()

# ─── Chat session state ────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = []

# ─── Chat history render ───────────────────────────────────────────────────
for msg in st.session_state.messages:
    if msg["role"] == "user":
        st.markdown(
            f'<div class="be-role">You</div>'
            f'<div class="be-bubble be-bubble-user">{msg["content"]}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="be-role">Assistant</div>'
            f'<div class="be-bubble be-bubble-assistant">{msg["content"]}</div>',
            unsafe_allow_html=True,
        )

# ─── Input row ─────────────────────────────────────────────────────────────
in_col, send_col = st.columns([6, 1])
with in_col:
    user_input = st.text_input(
        label="message",
        label_visibility="collapsed",
        placeholder="Ask about the trade data, schema, recent flows, top corridors…",
        key="chat_input",
    )
with send_col:
    send = st.button("Send", type="primary", use_container_width=True)

if send and user_input.strip():
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.spinner("Querying the warehouse…"):
        try:
            resp = requests.post(
                f"{API_URL}/chat",
                json={"message": user_input},
                timeout=30,
            )
            resp.raise_for_status()
            answer = resp.json().get("answer", "No response")
        except requests.exceptions.ConnectionError:
            answer = "❌ Could not connect to backend. Make sure uvicorn is running on port 8000."
        except requests.exceptions.Timeout:
            answer = "❌ Request timed out. The query may have taken too long."
        except Exception as e:
            answer = f"❌ Error: {e}"

    st.session_state.messages.append({"role": "assistant", "content": answer})
    st.rerun()

# ─── Clear button + Raw inspector ──────────────────────────────────────────
if st.session_state.messages:
    section_rule()
    if st.button("Clear chat"):
        st.session_state.messages = []
        st.rerun()

with st.expander("Raw request inspector"):
    st.markdown(
        f'<div style="color:{PALETTE["text_muted"]};font-size:0.7rem;'
        f"letter-spacing:0.1em;text-transform:uppercase;margin-bottom:4px;\">"
        f"Endpoint</div>",
        unsafe_allow_html=True,
    )
    st.code(f"POST {API_URL}/chat", language="bash")

    st.markdown(
        f'<div style="color:{PALETTE["text_muted"]};font-size:0.7rem;'
        f"letter-spacing:0.1em;text-transform:uppercase;margin-bottom:4px;"
        f'margin-top:0.5rem;">Payload</div>',
        unsafe_allow_html=True,
    )
    st.code('{"message": "<your input>"}', language="json")

    st.markdown(
        f'<div style="color:{PALETTE["text_muted"]};font-size:0.7rem;'
        f"letter-spacing:0.1em;text-transform:uppercase;margin-bottom:4px;"
        f'margin-top:0.5rem;">Health check</div>',
        unsafe_allow_html=True,
    )
    st.code(f"GET {API_URL}/health", language="bash")

    st.markdown(
        f'<div style="color:{PALETTE["text_muted"]};font-size:0.78rem;'
        f'margin-top:0.8rem;">'
        "Override the URL by adding <code>[backend]</code><br/>"
        "<code>url = \"https://your-backend\"</code><br/>"
        "to <code>.streamlit/secrets.toml</code>."
        "</div>",
        unsafe_allow_html=True,
    )