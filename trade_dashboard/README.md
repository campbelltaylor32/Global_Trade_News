# Global Trade Risk Ledger — Streamlit Dashboard

ADSP 31011 final project · UChicago MS in Applied Data Science
Team: Campbell Taylor · Jack Light · Ryan Dsouza · Amir Farooq

A polished, executive-style BI surface over UN Comtrade data that surfaces
trade volume, growth, concentration, dependency, and emerging-corridor signals.
The structure leaves room for the news/event overlay (GDELT / NewsAPI) on a
future Risk Overlay page.

---

## Quick start

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Connect to the warehouse (pick ONE of the two options below)

# Option A — Streamlit secrets (recommended)
#   Copy the template and fill in real values. The file is git-ignored.
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# then edit .streamlit/secrets.toml

# Option B — Environment variables (handy for CI/Docker)
cp .env.example .env
# edit .env, then:
set -a; source .env; set +a

# 3. Run
streamlit run app.py
```

The MySQL driver is **PyMySQL** (pulled in via `requirements.txt`). The
connection URL is assembled from `DB_USER / DB_PASS / DB_HOST / DB_PORT /
DB_NAME` with the password URL-encoded automatically, so special characters
like `!@#` work without manual escaping.

If neither secrets nor env vars are present, the app falls back to a
synthetic dataset so you can demo offline.

> **Security note** — never commit `.streamlit/secrets.toml` or `.env`.
> The course DB credentials should live only on the boxes that need them.
> If a password ever ends up somewhere it shouldn't, rotate it.

---

## Database schema expected

The data layer mirrors the schema you've already built:

```sql
SELECT * FROM fact_trade_granular_v2 v
JOIN country_geo g            ON g.iso_alpha_3 = v.partner_iso
LEFT JOIN unit_quantity_mapping q ON q.qty_code = v.qty_unit_code;
```

Required columns from `fact_trade_granular_v2`:
`ref_year, reporter_iso, reporter_desc, partner_iso, partner_desc, flow_code,
flow_desc, cmd_code, cmd_desc, primary_value_usd, net_weight, qty_unit_code`.

From `country_geo`: `iso_alpha_3, latitude, longitude`.
From `unit_quantity_mapping`: `qty_code, qty_abbr, qty_description`.

The data loader filters `partner_iso IN ('W00','WLD')` for bilateral views
and `cmd_code = 'TOTAL'` for commodity views — the standard Comtrade
aggregate rows.

---

## Pages

| Page | Business question it answers |
| --- | --- |
| **Global Overview** (`app.py`) | Who are the largest trading economies right now, how is global trade growing, who is gaining/losing the most? |
| **Trade Flows** (`pages/1_…`) | Where are the heaviest corridors? Pydeck arc map filterable by year, flow, commodity, and focus country. |
| **Country Profile** (`pages/2_…`) | How exposed is country X to its top partners? What's its commodity basket and how is it shifting? |
| **Commodity Explorer** (`pages/3_…`) | Who dominates global trade in a given HS chapter? Where are alternative suppliers emerging? |
| **Concentration & Risk** (`pages/4_…`) | Which countries look fragile from a structural-trade standpoint (HHI + volatility)? Composite score, scatter, dependency tables. |

Every page is filterable from the sidebar / inline; all charts share theme,
palette, and KPI-card styling defined in `lib/style.py`.

---

## Project layout

```
trade_dashboard/
├── app.py                       # Global Overview (landing)
├── pages/
│   ├── 1_Trade_Flows.py
│   ├── 2_Country_Profile.py
│   ├── 3_Commodity_Explorer.py
│   └── 4_Concentration_Risk.py
├── lib/
│   ├── data.py                  # DB + cache + synthetic fallback
│   ├── features.py              # Derived metrics (HHI, CAGR, intensity, etc.)
│   ├── charts.py                # Plotly + pydeck builders
│   └── style.py                 # Theme, CSS, KPI cards, number formatters
├── sql/
│   └── analytics_queries.sql    # 7 complex queries (CTEs, window fns, joins)
├── .streamlit/config.toml       # Dark executive theme
├── requirements.txt
└── README.md
```

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

## Rubric mapping

| Rubric component | Where it lives |
| --- | --- |
| Business case & proposal | This README + the project proposal PDF |
| EDA | `notebooks/` (add Jupyter notebook documenting profile / nulls / dist) |
| OLTP→OLAP modeling | ER diagrams + grain doc in `docs/` (add for submission) |
| ETL pipelines | Add ingestion scripts and a pipeline diagram in `etl/` |
| Complex SQL | `sql/analytics_queries.sql` — 7 queries with CTEs, windows, joins |
| BI surface | This Streamlit app — 5 pages answering ≥ 5 business questions |
| Presentation | Slides + recording — see `docs/presentation/` |

---

## Extending: news/event overlay

When the news layer is ready, add `pages/5_Risk_Overlay.py`. The cleanest
integration is a second derived metric on top of the trade-only composite:

```
final_risk_score = α · structural_risk + β · news_risk
news_risk        = z-score of tone × event volume × source weight
```

Both signals are 0–1 normalized, so the composite is interpretable and the
weights `(α, β)` become a single slider in the UI. Country-by-year is the
natural join grain.

---

## Contact

Office hours or email Steve / Zach for course-related questions.
