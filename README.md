# Global Trade News

> **A commodity-level trade intelligence warehouse linking UN Comtrade trade flows to GDELT news and geopolitical events.**
> University of Chicago — ADSP 31011 Data Engineering Platforms & Technologies — Final Project
> Team: Campbell Taylor · Jack Light · Ryan Dsouza · Amir Farooq

---

## What this project does

It builds an end-to-end pipeline that turns two open data sources into one warehouse you can query and visualize:

1. **UN Comtrade** — every reporter country's monthly/annual imports and exports, drilled down to 6-digit HS commodity codes, every bilateral partner, every transport mode.
2. **GDELT** — news articles and CAMEO-coded geopolitical events that mention those commodities, scored for sentiment and trade-relevant signals (tariffs, sanctions, embargoes, weather, strikes, etc.).

The two sides join cleanly on `cmd_code`, so for any HS chapter in any month you can ask: *how did trade volume move, and what was the world saying about it?*

A multi-page Streamlit dashboard sits on top of the warehouse for executive-style exploration: corridors, country profiles, commodity market share, concentration risk, and a small FastAPI/OpenAI chat MCP connection backend for natural-language schema queries.

---

## Architecture at a glance

```
                                ┌───────────────────────────┐
                                │      Reference CSVs       │
                                │  countries, HS codes,     │
                                │  flows, MoT, units, geo   │
                                └─────────────┬─────────────┘
                                              │
                                              ▼
┌────────────────────┐                ┌───────────────────────────┐
│   UN Comtrade API  │ ──── ETL ────► │      MySQL warehouse      │
│  (HS goods + EBOPS │                │                           │
│   services)        │                │   Dimensions              │
└────────────────────┘                │     country_mapping       │
                                      │     commodity_code_map.   │
┌────────────────────┐                │     tradeflow_mapping     │
│  GDELT DOC API     │ ──── ETL ────► │     transport_mapping     │
│  + GDELT Events    │                │     unit_quantity_map.    │
│  2.0 bulk CSVs     │                │     frequency_mapping     │
└────────────────────┘                │     country_geo           │
                                      │                           │
                                      │   Facts                   │
                                      │     fact_trade_granular   │
                                      │     news_articles         │
                                      │     news_events           │
                                      │     news_linking (rollup) │
                                      └─────────────┬─────────────┘
                                                    │
                                                    ▼
                                ┌───────────────────────────────────┐
                                │     Streamlit dashboard +         │
                                │     FastAPI chat MCP backend          │
                                └───────────────────────────────────┘
```

Two parallel Comtrade loading strategies coexist in the repo because we used them at different stages:

- **Direct OLAP path** — `comtrade_loader.py` (monthly) and `comtrade_granular_loader.py` (historical, AG6 with chapter-batched fallback for 100k-row truncation). Writes straight into `fact_trade_granular`.
- **OLTP → OLAP path** — `oltp_loader.py` pulls raw API rows into `comtrade_oltp.raw_trade_records`; `olap_loader.py` transforms those into `fact_trade_granular` and upserts dimensions. Useful when you want a staging buffer between API calls and the analytical model.

Pick the one that fits your operational story; both produce the same final fact table.

---

## Repository layout

```
Global_Trade_News/
├── README.md                       <- you are here
├── .env.example                    <- template for required env vars
├── requirements.txt                <- shared Python deps for ETL
│
├── schema/                         <- DDL for the MySQL warehouse
│   ├── 01_comtrade_schema.sql      <- dimensions + fact_trade_granular
│   ├── 02_news_schema.sql          <- news_articles / events / linking
│   └── README.md
│
├── etl/                            <- all loaders, organized by source
│   ├── README.md
│   ├── comtrade/
│   │   ├── comtrade_loader.py            <- monthly HS+EBOPS
│   │   ├── comtrade_granular_loader.py   <- AG6 historical, resumable
│   │   ├── oltp_loader.py                <- raw → comtrade_oltp
│   │   └── olap_loader.py                <- OLTP → fact_trade_granular
│   ├── news/
│   │   ├── generate_search_terms.py      <- HS desc → search-term CSV
│   │   ├── load_search_terms.py          <- CSV → commodity_search_terms
│   │   ├── commodity_news_loader.py      <- GDELT → MySQL (recommended)
│   │   └── commodity_news_collector.py   <- GDELT → CSV (standalone)
│   └── reference/
│       ├── load_reference_tables.py      <- 7 mapping tables
│       └── load_country_geo.py           <- ISO3 → lat/lon
│
├── reference_data/                 <- source CSVs the loaders read
│   ├── Country_Mapping_Data.csv
│   ├── Commodity_Code_Mapping.csv
│   ├── Frequency_Mapping.csv
│   ├── Tradeflow_Mapping.csv
│   ├── Transport_Mapping.csv
│   ├── Consumption_Mapping.csv
│   ├── Unit_Quantity_Mapping.csv
│   ├── country-longitude-latitude.csv
│   ├── commodity_search_terms.csv        <- generated, regenerable
│   └── README.md
│
├── dashboard/                      <- multi-page Streamlit + FastAPI
│   ├── app.py                            <- landing: Global Overview
│   ├── requirements.txt
│   ├── .streamlit/
│   ├── pages/                            <- 5 themed pages
│   ├── lib/                              <- data / features / charts
│   ├── backend/                          <- FastAPI chat API
│   ├── sql/analytics_queries.sql         <- 7 reference analytics queries
│   └── README.md
│
├── docs/                           <- reference docs, EER diagram, API field defs
│   ├── ComtradePlus_DataItems.xlsx
│   ├── EER_Comtrade.png
│   └── EER_Model_v1.mwb
│
├── notebooks/                      <- exploratory analyses
│   └── eda.ipynb
│   └── full_eda.ipynb
│
├── comtrade_output/                <- loader artifacts (gitignored going forward)
├── output/                         <- legacy news-collector artifacts (gitignored)
│
└── legacy/
    └── comtrade_streamlitapp.py    <- single-file early dashboard
```

---

## Quickstart

### 1. Provision the database

Any MySQL 8 instance works (we used Google Cloud SQL). Create a schema named `comtrade` and grant your user write access.

```bash
mysql -h $DB_HOST -u $DB_USER -p $DB_NAME < schema/01_comtrade_schema.sql
mysql -h $DB_HOST -u $DB_USER -p $DB_NAME < schema/02_news_schema.sql
```

The Comtrade schema enforces foreign keys, so **dimensions must exist before facts**. The reference loader in step 3 handles that.

### 2. Configure environment

Copy and fill out `.env.example`:

```bash
cp .env.example .env
```

Required keys (see `.env.example` for the full list):

| Variable | Purpose |
| --- | --- |
| `DB_USER` / `DB_PASS` / `DB_HOST` / `DB_PORT` / `DB_NAME` | MySQL connection |
| `COMTRADE_SUBSCRIPTION_KEY` | UN Comtrade Plus API key |
| `START_DATE` / `END_DATE` | Window for news collection (YYYY-MM-DD) |
| `START_PERIOD` / `END_PERIOD` | Window for trade collection (YYYY or YYYYMM) |
| `FREQ_CODE` | `A` annual or `M` monthly |
| `OPENAI_API_KEY` | Optional — backend chat in the dashboard |

### 3. Load reference data

```bash
python etl/reference/load_reference_tables.py
python etl/reference/load_country_geo.py --csv reference_data/country-longitude-latitude.csv
```

### 4. Load trade data

Pick **one** of the two Comtrade strategies:

**A. Direct OLAP — granular historical (recommended for first load):**

```bash
python etl/comtrade/comtrade_granular_loader.py
```

This iterates every (period, reporter, flow) chunk at `AG6` granularity, falls back to chapter batching when a chunk hits the 100k-row API cap, and writes resumably. Re-running picks up where it left off via `manifest.csv` and `budget.json`.

**B. OLTP → OLAP split (better for streaming/incremental):**

```bash
python etl/comtrade/oltp_loader.py     # raw → comtrade_oltp
python etl/comtrade/olap_loader.py     # OLTP → fact_trade_granular
```

### 5. Load news data

```bash
# Generate per-commodity search terms (one-time, or whenever HS revision changes)
python etl/news/generate_search_terms.py \
    --input  reference_data/commodity_final.csv \
    --output reference_data/commodity_search_terms.csv

# Load them into MySQL
python etl/news/load_search_terms.py \
    --csv reference_data/commodity_search_terms.csv

# Pull articles + (optionally) events from GDELT
python etl/news/commodity_news_loader.py
```

The news loader applies **strict attribution**: every article is scored against every matching search term and is then written exactly once with its top-scoring `cmd_code`. The runner-up `cmd_code` is preserved for QA.

After the collection finishes the loader rebuilds `news_linking`, the pre-aggregated `cmd_code × year_month` rollup table that the dashboard joins against.

### 6. Run the dashboard

```bash
cd dashboard
pip install -r requirements.txt
streamlit run app.py
```

Optionally, start the FastAPI backend in a second terminal:

```bash
uvicorn dashboard.backend.app:app --reload --port 8000
```

---

## The data model

### Dimensions

| Table | Grain | Source |
| --- | --- | --- |
| `frequency_mapping` | `freq_code` | Comtrade reference |
| `tradeflow_mapping` | `flow_code` | Comtrade reference |
| `transport_mapping` | `mot_code` | Comtrade reference |
| `unit_quantity_mapping` | `qty_code` | Comtrade reference |
| `country_mapping` | M49 `country_code` | Comtrade reference, augmented with ISO2/ISO3 |
| `commodity_code_mapping` | HS `cmd_code` | Comtrade reference, self-referential (HS chapter → heading → subheading) |
| `country_geo` | ISO3 | external lat/lon lookup, joins via `iso_alpha_3` |
| `commodity_search_terms` | `(cmd_code, term, language)` | generated curated + extracted vocabulary for GDELT |

### Facts

- **`fact_trade_granular`** — one row per `(period, reporter, flow, partner, partner2, cmd, customs, mot)`. Foreign-keyed to every dimension above except `country_geo` (it joins on ISO3, not M49). Carries `cif_value_usd`, `fob_value_usd`, `primary_value_usd`, weights, quantities, and estimation flags.
- **`news_articles`** — one row per article URL after strict attribution. Sentiment, signal flags, runner-up commodity for audit.
- **`news_events`** — one row per `(cmd_code, source_url_hash, event_code)` from GDELT Event 2.0 with CAMEO code, Goldstein scale, actor/location.
- **`news_linking`** — materialized `cmd_code × year_month` rollup: article counts, event counts, average sentiment/tone/Goldstein, signal counters. The dashboard joins **this** to the fact table, not the raw article/event tables.

### Joining trade to news

```sql
SELECT
    f.period,
    f.reporter_desc,
    f.partner_desc,
    f.cmd_desc,
    f.primary_value_usd,
    n.article_count,
    n.avg_sentiment,
    n.signal_tariff,
    n.signal_sanction
FROM fact_trade_granular_v2 f
LEFT JOIN news_linking n
    ON n.cmd_code = f.cmd_code
   AND n.period   = f.period;
```

---

## Additional Operational Information

### Comtrade rate limits

The free Comtrade Plus subscription is capped at ~500 calls/day. `comtrade_granular_loader.py` tracks usage in `comtrade_output/budget.json` and stops cleanly when the buffer is hit. The manifest at `comtrade_output/manifest.csv` makes the next day's run idempotent.

When a chunk returns exactly 100,000 rows (the API page cap), the loader interprets it as truncated and refetches the chunk in URL-length-safe batches of 6-digit leaf codes pulled from `Commodity_Code_Mapping.csv`.

### GDELT rate limits

The GDELT DOC API throttles aggressively. `commodity_news_loader.py` defaults to conservative pacing (`SLEEP_SECONDS=8`, exponential backoff with jitter on 429 / 5xx / non-JSON responses) and is *resumable* via `news_load_manifest`. Two query strategies are supported:

- `combined` — one big `term AND (ctx1 OR ctx2 OR …)` call per commodity (faster but more likely to be rejected on long queries).
- `separate_context` (default) — many smaller `term AND ctx` calls (more API calls, but more reliable).

### Loader output directories

The Comtrade and news loaders write to `comtrade_output/` and `news_output/` (or `output/` for the legacy collector) by default. Early in the project these were committed to source control as shared artifacts; they're now in `.gitignore` so future runs don't bloat git history. To untrack the currently-committed copies without deleting them on disk:

```bash
git rm --cached -r comtrade_output output news_output
git commit -m "Stop tracking loader output directories"
```

Each output directory contains:

- `manifest.csv` (Comtrade) / `news_load_manifest` (news, in MySQL) — chunk-level resumability ledger.
- `budget.json` — daily API call counter that resets at UTC midnight.
- `loader.log` — full streaming log of the run.
- For the Comtrade granular loader, `comtrade_master.csv` — the appended master CSV of every cleaned row when `WRITE_MASTER_CSV=true`.

---

## Dashboard pages

| Page | Business question |
| --- | --- |
| **Global Overview** | Who are the largest trading economies right now, how is global trade growing, who's gaining/losing the most? |
| **Trade Flows** | Where are the heaviest corridors? Pydeck arc map filterable by year, flow, commodity, and focus country. |
| **Country Profile** | How exposed is country X to its top partners? What's its commodity basket and how is it shifting? |
| **Commodity Explorer** | Who dominates global trade in a given HS chapter? Where are alternative suppliers emerging? |
| **Concentration & Risk** | Which countries look fragile from a structural-trade standpoint (HHI + volatility)? |
| **AI Trade Analysis** | Natural-language schema/data queries via FastAPI + OpenAI + MCP. |

Concentration risk metrics: partner HHI, top-N share, effective partners, commodity HHI, YoY volatility, and a rank-based composite score. None of these use the news layer yet — adding a "Risk Overlay" page that combines structural concentration with GDELT tone is the obvious next step.

---

## Reference analytics queries

`dashboard/sql/analytics_queries.sql` contains seven self-documenting queries that exercise the warehouse end-to-end (CTEs, window functions, multi-dim joins). Use them as starting points for your own analyses:

1. Top-15 corridors with YoY growth (window LAG)
2. Country partner HHI and Top-3 dependency (nested aggregation)
3. 3-year CAGR of exports per country
4. Commodity market share — top-5 exporters per HS chapter
5. Bilateral trade intensity (relationship strength on both sides)
6. Trade balance leaderboard (top surpluses + deficits)
7. Commodity-level HHI with rolling 3-year average

---

## Development workflow

```bash
# create and activate a venv
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# install ETL deps
pip install -r requirements.txt

# run loaders from the project root so relative paths resolve
python etl/reference/load_reference_tables.py
python etl/comtrade/comtrade_granular_loader.py --help
python etl/news/commodity_news_loader.py --dry-run

# dashboard
cd dashboard
pip install -r requirements.txt
streamlit run app.py
```

### Useful flags

| Loader | Flag | Effect |
| --- | --- | --- |
| `comtrade_granular_loader.py` | sets `MAX_DAILY_CALLS` in `.env` | Hard daily call ceiling |
| `comtrade_granular_loader.py` | `LOAD_TO_MYSQL=true` | Append to fact table as well as CSV |
| `commodity_news_loader.py` | `--dry-run` | Preview plan without API calls |
| `commodity_news_loader.py` | `--skip-events` | Articles only |
| `commodity_news_loader.py` | `--rebuild-linking` | Rebuild rollup without recollecting |
| `commodity_news_loader.py` | `--max-priority 2` | Only top-priority search terms |
| `commodity_news_loader.py` | `--cmd-code 10,27,72` | Limit run to specific HS chapters |
| `commodity_news_loader.py` | `--reset-failed` | Retry failed/in-progress manifest rows |
| `load_search_terms.py` | `--replace` | Truncate before reload |

---

## Known limitations and next steps

- Comtrade returns occasional reporter/HS codes that the published mapping CSVs don't include (new HS revisions, "Areas, n.e.s."). They're inserted as placeholders; refresh the mapping CSVs periodically.
- Strict attribution in the news loader resolves multi-commodity articles to one `cmd_code`. The runner-up is preserved, but a soft-attribution (weighted) variant could be added if multi-cmd analyses become important.

---

## Credits and sources

- **UN Comtrade** — trade data, courtesy of the UN Statistics Division. <https://comtradeplus.un.org>
- **GDELT Project** — news articles and event data. <https://www.gdeltproject.org>
- **HS classification** — World Customs Organization Harmonized System.
- **Streamlit, Plotly, pydeck, SQLAlchemy, pandas, FastAPI** — open-source dashboard and ETL stack.

Built as the final project for ADSP 31011, MS in Applied Data Science, University of Chicago.
