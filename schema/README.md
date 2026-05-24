# Schema

DDL for the MySQL warehouse. Run these once when provisioning the database, in order.

## Files

| File | Contents |
| --- | --- |
| `01_comtrade_schema.sql` | All Comtrade dimensions + `fact_trade_granular` + `load_manifest`. Includes bootstrap rows for codes the API emits but the reference CSVs don't include (e.g. partner `0` "World"). |
| `02_news_schema.sql` | `commodity_search_terms`, `news_articles`, `news_events`, `news_linking`. All foreign-keyed to `commodity_code_mapping.cmd_code`. |

## Order matters

The fact tables have foreign keys into the dimension tables, so:

1. Apply `01_comtrade_schema.sql`.
2. Load the reference CSVs (`etl/reference/load_reference_tables.py`) so dimensions are populated.
3. Apply `02_news_schema.sql`.
4. Load search terms (`etl/news/load_search_terms.py`).

The Comtrade fact loader has its own `_ensure_parent_rows()` safety net that inserts placeholder dimension rows when the API returns codes the mapping CSVs don't include, so step 2 doesn't have to be exhaustive — but it should at least include all HS chapters present in your run.

## Conventions

- All tables are InnoDB with `utf8mb4 / utf8mb4_0900_ai_ci` to avoid collation mismatches on FK columns.
- `ON DELETE RESTRICT` is used everywhere — you can't accidentally orphan billions of fact rows.
- `ON UPDATE CASCADE` lets you correct a code's spelling/casing without rewriting facts.

## Applying

```bash
mysql -h $DB_HOST -u $DB_USER -p $DB_NAME < schema/01_comtrade_schema.sql
mysql -h $DB_HOST -u $DB_USER -p $DB_NAME < schema/02_news_schema.sql
```
