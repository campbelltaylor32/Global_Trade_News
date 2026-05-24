# Reference Data

Source CSVs the ETL pipelines read. Put your mapping files here.

## Expected files

| File | Used by | Notes |
| --- | --- | --- |
| `Country_Mapping_Data.csv` | `load_reference_tables.py`, `comtrade_granular_loader.py` (for reporter list and chapter subdivision) | M49 codes + ISO2/ISO3 + dates + isGroup. Variable column count; the parser handles it. |
| `Commodity_Code_Mapping.csv` | `load_reference_tables.py`, `comtrade_granular_loader.py` (for leaf-code batching) | Self-referential HS hierarchy. `cp1252` encoded. |
| `Frequency_Mapping.csv` | `load_reference_tables.py` | freq_code → freq_desc |
| `Tradeflow_Mapping.csv` | `load_reference_tables.py` | flow_code → flow_desc |
| `Transport_Mapping.csv` | `load_reference_tables.py` | mot_code → mot_desc |
| `Consumption_Mapping.csv` | `load_reference_tables.py` | Services modes |
| `Unit_Quantity_Mapping.csv` | `load_reference_tables.py` | qty_code → qty_abbr + description. `cp1252` encoded. |
| `country-longitude-latitude.csv` | `load_country_geo.py` | ISO3 → lat/lon + Wikidata metadata |
| `commodity_final.csv` | `generate_search_terms.py` (input) | Curated `(cmd_code, cmd_desc)` list — your project's chosen HS chapters |
| `commodity_search_terms.csv` | `load_search_terms.py` (output of generator) | Generated; regenerable. Commit it so the team works from the same vocabulary. |

## Where to get the Comtrade reference CSVs

These are published by UN Comtrade and updated periodically. Check the official reference endpoints:

- Reporters / partners — `https://comtradeapi.un.org/files/v1/app/reference/Reporters.json`
- HS codes — `https://comtradeapi.un.org/files/v1/app/reference/HS.json`
- Flows — `https://comtradeapi.un.org/files/v1/app/reference/tradeFlows.json`
- MoT — `https://comtradeapi.un.org/files/v1/app/reference/modeOfTransport.json`
- Units — `https://comtradeapi.un.org/files/v1/app/reference/unitOfQty.json`

`comtrade_loader.py --seed-refs` will pull all of these for you and write them straight into the dimension tables, so you don't strictly need local CSVs if you're using that loader.

The bilateral-trade-friendly CSVs we used were originally exported from Comtrade's bulk reference downloads and lightly cleaned. If you regenerate them, keep the column names consistent — the parsers in `load_reference_tables.py` are written to those specific layouts (especially `Country_Mapping_Data.csv`, which has variable column counts the parser handles by content).

## Encoding

Several of the CSVs come `cp1252` encoded (Comtrade ships them that way). The loaders all use `encoding="cp1252"` for those files — don't re-save them as UTF-8 unless you also update the loader, because m², n.e.s., and certain country names contain non-ASCII bytes that get mangled by a naive re-encode.

## Refresh cadence

- Reporters/partners — annually (new countries added/historical entities expiring).
- HS — every five years for the major revision, plus minor patches in between.
- Flows, MoT, units, consumption — rarely change.
- Country geo — once unless borders change.

The Comtrade fact loader inserts placeholder rows for any code it sees that's missing from these CSVs, so a stale CSV doesn't break a run — but the dashboard will display "UNKNOWN (123)" until you refresh.
