import streamlit as st
import requests

API_URL = "http://localhost:8000"

st.set_page_config(page_title="Trade AI Analyst", page_icon="🛰️", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
}

.stApp {
    background-color: #0a0e17;
    color: #c9d1d9;
}

.title-block {
    border-left: 3px solid #00e5ff;
    padding: 0.4rem 1rem;
    margin-bottom: 1.5rem;
}

.title-block h1 {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.4rem;
    color: #e6edf3;
    margin: 0;
    letter-spacing: 0.05em;
}

.title-block p {
    font-size: 0.78rem;
    color: #8b949e;
    margin: 0.2rem 0 0 0;
    font-family: 'IBM Plex Mono', monospace;
}

.status-pill {
    display: inline-block;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.72rem;
    padding: 3px 10px;
    border-radius: 20px;
    margin-bottom: 1.5rem;
}

.status-ok {
    background: rgba(0, 229, 255, 0.1);
    color: #00e5ff;
    border: 1px solid rgba(0, 229, 255, 0.3);
}

.status-fail {
    background: rgba(255, 80, 80, 0.1);
    color: #ff5050;
    border: 1px solid rgba(255, 80, 80, 0.3);
}

.chat-bubble-user {
    background: rgba(0, 229, 255, 0.07);
    border: 1px solid rgba(0, 229, 255, 0.2);
    border-radius: 8px;
    padding: 0.75rem 1rem;
    margin: 0.5rem 0;
    font-size: 0.9rem;
}

.chat-bubble-assistant {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    padding: 0.75rem 1rem;
    margin: 0.5rem 0;
    font-size: 0.9rem;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.82rem;
    line-height: 1.6;
    white-space: pre-wrap;
}

.label {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.65rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #8b949e;
    margin-bottom: 4px;
}

.divider {
    border: none;
    border-top: 1px solid rgba(255,255,255,0.06);
    margin: 1.2rem 0;
}

/* Override Streamlit input */
.stTextInput > div > div > input {
    background: rgba(255,255,255,0.04) !important;
    border: 1px solid rgba(255,255,255,0.12) !important;
    color: #e6edf3 !important;
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 0.88rem !important;
    border-radius: 6px !important;
}

.stButton > button {
    background: rgba(0, 229, 255, 0.1) !important;
    border: 1px solid rgba(0, 229, 255, 0.4) !important;
    color: #00e5ff !important;
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 0.8rem !important;
    letter-spacing: 0.05em !important;
    border-radius: 6px !important;
    transition: all 0.2s !important;
}

.stButton > button:hover {
    background: rgba(0, 229, 255, 0.2) !important;
}
</style>
""", unsafe_allow_html=True)

# --- Header ---
st.markdown("""
<div class="title-block">
    <h1>🛰️ Trade AI Analyst</h1>
    <p>FastAPI → OpenAI → GCP MySQL</p>
</div>
""", unsafe_allow_html=True)

# --- Health Check ---
try:
    r = requests.get(f"{API_URL}/health", timeout=3)
    if r.status_code == 200:
        st.markdown('<span class="status-pill status-ok">● BACKEND ONLINE</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="status-pill status-fail">● BACKEND ERROR</span>', unsafe_allow_html=True)
except Exception:
    st.markdown('<span class="status-pill status-fail">● BACKEND OFFLINE — is uvicorn running?</span>', unsafe_allow_html=True)

st.markdown('<hr class="divider">', unsafe_allow_html=True)

# --- Session state ---
if "messages" not in st.session_state:
    st.session_state.messages = []

# --- Chat history ---
for msg in st.session_state.messages:
    if msg["role"] == "user":
        st.markdown(f'<div class="label">YOU</div><div class="chat-bubble-user">{msg["content"]}</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="label">ASSISTANT</div><div class="chat-bubble-assistant">{msg["content"]}</div>', unsafe_allow_html=True)

# --- Input ---
col1, col2 = st.columns([6, 1])
with col1:
    user_input = st.text_input(
        label="message",
        label_visibility="collapsed",
        placeholder="Ask about your trade data, schema, tables...",
        key="chat_input"
    )
with col2:
    send = st.button("SEND →")

if send and user_input.strip():
    st.session_state.messages.append({"role": "user", "content": user_input})

    with st.spinner(""):
        try:
            resp = requests.post(
                f"{API_URL}/chat",
                json={"message": user_input},
                timeout=30
            )
            resp.raise_for_status()
            answer = resp.json().get("answer", "No response")
        except requests.exceptions.ConnectionError:
            answer = "❌ Could not connect to backend. Make sure uvicorn is running on port 8000."
        except requests.exceptions.Timeout:
            answer = "❌ Request timed out. The query may have taken too long."
        except Exception as e:
            answer = f"❌ Error: {str(e)}"

    st.session_state.messages.append({"role": "assistant", "content": answer})
    st.rerun()

# --- Clear ---
if st.session_state.messages:
    st.markdown('<hr class="divider">', unsafe_allow_html=True)
    if st.button("CLEAR CHAT"):
        st.session_state.messages = []
        st.rerun()

# --- Raw request inspector ---
with st.expander("RAW REQUEST INSPECTOR"):
    st.markdown('<div class="label">ENDPOINT</div>', unsafe_allow_html=True)
    st.code(f"POST {API_URL}/chat", language="bash")
    st.markdown('<div class="label">PAYLOAD</div>', unsafe_allow_html=True)
    st.code('{"message": "<your input>"}', language="json")
    st.markdown('<div class="label">HEALTH CHECK</div>', unsafe_allow_html=True)
    st.code(f"GET {API_URL}/health", language="bash")