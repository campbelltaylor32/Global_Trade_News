# Global Trade Risk Ledger — Streamlit Dashboard

ADSP 31011 final project · UChicago MS in Applied Data Science
Team: Campbell Taylor · Jack Light · Ryan Dsouza · Amir Farooq

A polished, executive-style BI surface over UN Comtrade data that surfaces
trade volume, growth, concentration, dependency, and emerging-corridor signals.
Includes an AI-powered trade analysis interface backed by a FastAPI service
that queries Cloud SQL directly via Google's Cloud SQL MCP.

---

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/campbelltaylor32/Global_Trade_News.git
cd Global_Trade_News/dashboard
python -m venv .venv && source .venv/bin/activate   # Mac/Linux
# or: python -m venv .venv && .venv\Scripts\activate  # Windows
pip install -r requirements.txt

# 2. Set up your OpenAI key
cp .env.example .env
# edit .env and add:
# OPENAI_API_KEY=your-key-here

# 3. Authenticate with GCP (one-time setup)
gcloud auth application-default login
gcloud auth application-default set-quota-project commoditytrade

# 4. Run the backend (in one terminal)
uvicorn backend.app:app --reload

# 5. Run the frontend (in another terminal)
streamlit run app.py
```

---

## Onboarding a new teammate

1. **Get added to the GCP project** — ask Ryan to add your Google account in GCP Console → IAM with the `Cloud SQL Editor` role.

2. **Install the Google Cloud SDK** — https://cloud.google.com/sdk/docs/install

3. **Clone the repo and install dependencies**
   ```bash
   git clone https://github.com/campbelltaylor32/Global_Trade_News.git
   cd Global_Trade_News/dashboard
   pip install -r requirements.txt
   ```

4. **Add your OpenAI key to `.env`**
   ```bash
   cp .env.example .env
   # edit .env and set OPENAI_API_KEY
   ```

5. **Authenticate with GCP**
   ```bash
   gcloud auth application-default login
   gcloud auth application-default set-quota-project commoditytrade
   ```

6. **Run it**
   ```bash
   uvicorn backend.app:app --reload   # terminal 1
   streamlit run app.py               # terminal 2
   ```

---

## Architecture

```
Streamlit frontend
      │
      ▼
FastAPI backend (dashboard/backend/app.py)
      │
      ├── OpenAI gpt-4o-mini  ──▶  translates question to SQL
      │
      └── Google Cloud SQL MCP  ──▶  executes SQL on Cloud SQL (commoditytrade/final)
```

The AI trade analysis backend uses a three-step approach:
- One small OpenAI call (~300 tokens) to translate the user's question into SQL using the live database schema
- One direct MCP call to execute the SQL against Cloud SQL — no OpenAI involved
- One small OpenAI call to format the raw results into plain English

The database schema is fetched once at startup and cached, so column names are always accurate and the model never guesses.

---

## Pages

| Page | Business question it answers |
| --- | --- |
| **Global Overview** (`app.py`) | Who are the largest trading economies right now, how is global trade growing, who is gaining/losing the most? |
| **Trade Flows** (`pages/1_…`) | Where are the heaviest corridors? Pydeck arc map filterable by year, flow, commodity, and focus country. |
| **Country Profile** (`pages/2_…`) | How exposed is country X to its top partners? What's its commodity basket and how is it shifting? |
| **Commodity Explorer** (`pages/3_…`) | Who dominates global trade in a given HS chapter? Where are alternative suppliers emerging? |
| **Concentration & Risk** (`pages/4_…`) | Which countries look fragile from a structural-trade standpoint (HHI + volatility)? Composite score, scatter, dependency tables. |
| **Trade Analysis** (`pages/5_…`) | Ask natural language questions about the trade data and get AI-powered answers backed by live SQL queries. |

Every page is filterable from the sidebar / inline; all charts share theme,
palette, and KPI-card styling defined in `lib/style.py`.

---

## Project layout

```
Global_Trade_News/
├── dashboard/                       # Streamlit app + FastAPI backend
│   ├── app.py                       # Global Overview (landing)
│   ├── backend/
│   │   └── app.py                   # FastAPI AI analysis backend
│   ├── pages/
│   │   ├── 1_Trade_Flows.py
│   │   ├── 2_Country_Profile.py
│   │   ├── 3_Commodity_Explorer.py
│   │   ├── 4_Concentration_Risk.py
│   │   └── 5_Backend_Test.py        # AI trade analysis interface
│   ├── lib/
│   │   ├── data.py                  # DB + cache + synthetic fallback
│   │   ├── features.py              # Derived metrics (HHI, CAGR, intensity, etc.)
│   │   ├── charts.py                # Plotly + pydeck builders
│   │   └── style.py                 # Theme, CSS, KPI cards, number formatters
│   ├── sql/
│   │   └── analytics_queries.sql    # 7 complex queries (CTEs, window fns, joins)
│   ├── .streamlit/config.toml       # Dark executive theme
│   ├── .env.example                 # Template — copy to .env and fill in
│   └── requirements.txt
├── etl/                             # Data loading scripts
│   ├── comtrade/                    # Comtrade data loaders
│   └── news/                        # News data collectors
├── schema/                          # SQL schema definitions
│   ├── 01_comtrade_schema.sql
│   └── 02_news_schema.sql
├── docs/                            # ERD and data documentation
├── notebooks/                       # EDA notebooks
├── reference_data/                  # Static mapping CSVs
└── README.md
```

---

## Database schema

The data layer is built on Cloud SQL (MySQL) in the `comtrade` database.
Key tables:

| Table | Description |
| --- | --- |
| `fact_trade_granular_v2` | Main trade data — reporter, partner, year, value, flow, commodity |
| `country_mapping` | Country code to name mapping |
| `commodity_code_mapping` | Commodity code to description mapping |
| `tradeflow_mapping` | Trade flow codes (import/export) |
| `country_geo` | Geographic coordinates per country |
| `news_articles` | News articles linked to trade events |

The AI analysis backend fetches the live schema at startup so SQL generation
always uses accurate column names.

---

## Metrics glossary

| Metric | Definition | Used on |
| --- | --- | --- |
| **YoY growth** | `(value_t − value_t-1) / value_t-1` | Overview, Country |
| **3-yr CAGR** | `(end / start)^(1/3) − 1` | Overview, Country |
| **Partner HHI** | `Σ(partner_share²) × 10,000`, computed per reporter-year-flow | Country, Risk |
| **Top-N share** | Share of trade going to top-1 / top-3 / top-5 partners | Country, Risk |
| **Effective partners** | `1 / Σ(share²)` — diversification-equivalent partner count | Country, Risk |
| **Commodity HHI** | HHI across HS chapters within a country | Risk |
| **Market share** | Country's share of global trade in a commodity | Commodity, Risk |
| **Trade intensity** | `(bilat / rep_total) × (bilat / par_total)` | (SQL Q5) |
| **Composite risk** | `0.45·rank(HHI) + 0.35·rank(cmd HHI) + 0.20·rank(vol)` × 100 | Risk |

Volatility uses the std of YoY growth across the full panel.

---

> **Security note** — never commit `.env` or `.streamlit/secrets.toml`.
> If a key is ever exposed, rotate it immediately.