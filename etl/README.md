# ETL

All loaders for the warehouse. Organized by data source.

```
etl/
├── reference/  <- mapping tables (countries, HS codes, units, geo). Load first.
├── comtrade/   <- trade facts. Two parallel strategies, pick one.
└── news/       <- GDELT articles and events, plus search-term tooling.
```

## Order

1. `reference/load_reference_tables.py` — populates dimensions.
2. `reference/load_country_geo.py` — populates `country_geo` (lat/lon for ISO3).
3. `comtrade/comtrade_granular_loader.py` **OR** `comtrade/oltp_loader.py` + `comtrade/olap_loader.py` — populates `fact_trade_granular`.
4. `news/generate_search_terms.py` → `news/load_search_terms.py` → `news/commodity_news_loader.py` — populates `news_articles`, `news_events`, `news_linking`.

## Run from the repo root

All loaders use relative paths (`reference_data/Country_Mapping_Data.csv`, etc.) that resolve correctly only when invoked from the project root:

```bash
# good
python etl/comtrade/comtrade_granular_loader.py

# bad — relative paths to reference_data/ will fail
cd etl/comtrade && python comtrade_granular_loader.py
```

If you prefer running from anywhere, point `REFERENCE_DIR`, `COUNTRY_MAPPING_CSV`, and `COMMODITY_MAPPING_CSV` at absolute paths in your `.env`.

## Logging

Each loader writes a `loader.log` in its output directory (`comtrade_output/`, `news_output/`) and also streams to stdout. Tail the file to watch progress on long runs:

```bash
tail -f comtrade_output/loader.log
tail -f news_output/loader.log
```

## Resumability

- `comtrade_granular_loader.py` — `comtrade_output/manifest.csv` records every chunk; re-runs skip completed work.
- `commodity_news_loader.py` — `news_load_manifest` table tracks success/failure per (cmd_code, term, window). Use `--reset-failed` to retry only the failed rows.
- Daily API budgets are persisted in `budget.json` (Comtrade) and `news_output/budget.json` (GDELT). Stop and restart anytime; the budget resets at UTC midnight.
