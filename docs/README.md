# Docs

Reference materials and design artifacts. None of these are read by the ETL pipelines at runtime — they're documentation for humans.

## Contents

| File | Purpose |
| --- | --- |
| `ComtradePlus_DataItems.xlsx` | Official UN Comtrade Plus field reference. Definitions for every column the API returns (cifValue vs fobValue vs primaryValue, qtyUnitCode, partner2Code, motCode, customsCode, all the estimation flags, etc.). The Python loaders' rename maps and type coercions are derived from this. |
| `EER_Comtrade.png` | Rendered EER (entity-relationship) diagram of the Comtrade warehouse — `fact_trade_granular` and its seven dimension tables, with FK relationships. Useful for orientation and for slide decks. |
| `EER_Model_v1.mwb` | MySQL Workbench source file for the EER diagram. Open in Workbench to edit the model and regenerate the PNG. |

## When to update

- **`ComtradePlus_DataItems.xlsx`** — refresh if UN Comtrade adds/renames API fields. Last verified against the current Comtrade Plus reference. If you update this and find new fields worth keeping, add them to `COLUMN_RENAME` in `etl/comtrade/comtrade_granular_loader.py` and to the `fact_trade_granular` schema.
- **EER files** — re-export the PNG from Workbench whenever `schema/01_comtrade_schema.sql` or `schema/02_news_schema.sql` changes meaningfully. The two SQL files are the source of truth; the diagram is the visual.

## Suggested additions (not yet here)

- A short architecture write-up (markdown) describing the two Comtrade pipelines and why both exist.
- The project proposal / final report once it's submitted.
- Slide deck PDFs.
