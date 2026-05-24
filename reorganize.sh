#!/usr/bin/env bash
# reorganize.sh
# -----------------------------------------------------------------------------
# Move the current flat repo layout into the proposed Global_Trade_News
# subfolder structure. Idempotent: rerunning after a partial move is safe.
#
# Run from the project root (the folder that currently contains
# comtrade_loader.py, Schema_Build.sql, trade_dashboard/, etc.).
#
#   chmod +x reorganize.sh
#   ./reorganize.sh
#
# Uses `git mv` when inside a git repo so history is preserved; falls back to
# plain `mv` otherwise.
# -----------------------------------------------------------------------------

set -euo pipefail

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    MV="git mv"
    echo "Detected git repo — using 'git mv' to preserve history."
else
    MV="mv"
    echo "Not in a git repo — using plain 'mv'."
fi

move_if_exists() {
    local src="$1"
    local dst="$2"
    if [[ -e "$src" ]]; then
        mkdir -p "$(dirname "$dst")"
        $MV "$src" "$dst"
        echo "  moved  $src  ->  $dst"
    fi
}

echo
echo "Creating target directories..."
mkdir -p schema
mkdir -p etl/comtrade etl/news etl/reference
mkdir -p reference_data
mkdir -p dashboard
mkdir -p docs
mkdir -p notebooks
mkdir -p legacy

echo
echo "Moving SQL schema files..."
move_if_exists "Schema_Build.sql"   "schema/01_comtrade_schema.sql"
move_if_exists "news_schema.sql"    "schema/02_news_schema.sql"

echo
echo "Moving Comtrade ETL scripts..."
move_if_exists "comtrade_loader.py"           "etl/comtrade/comtrade_loader.py"
move_if_exists "comtrade_granular_loader.py"  "etl/comtrade/comtrade_granular_loader.py"
move_if_exists "oltp_loader.py"               "etl/comtrade/oltp_loader.py"
move_if_exists "olap_loader.py"               "etl/comtrade/olap_loader.py"

echo
echo "Moving News ETL scripts..."
move_if_exists "generate_search_terms.py"     "etl/news/generate_search_terms.py"
move_if_exists "load_search_terms.py"         "etl/news/load_search_terms.py"
move_if_exists "commodity_news_loader.py"     "etl/news/commodity_news_loader.py"
move_if_exists "commodity_news_collector.py"  "etl/news/commodity_news_collector.py"

echo
echo "Moving Reference ETL scripts..."
move_if_exists "load_reference_tables.py"     "etl/reference/load_reference_tables.py"
move_if_exists "load_country_geo.py"          "etl/reference/load_country_geo.py"

echo
echo "Moving reference data CSVs..."
for csv in \
    Country_Mapping_Data.csv \
    Commodity_Code_Mapping.csv \
    Frequency_Mapping.csv \
    Tradeflow_Mapping.csv \
    Transport_Mapping.csv \
    Consumption_Mapping.csv \
    Unit_Quantity_Mapping.csv \
    country-longitude-latitude.csv \
    commodity_final.csv \
    commodity_search_terms.csv \
    commodity_news_plan.csv
do
    move_if_exists "$csv" "reference_data/$csv"
done

echo
echo "Moving documentation and schema diagrams..."
move_if_exists "ComtradePlus_DataItems.xlsx"  "docs/ComtradePlus_DataItems.xlsx"
move_if_exists "EER_Comtrade.png"             "docs/EER_Comtrade.png"
move_if_exists "EER_Model_v1.mwb"             "docs/EER_Model_v1.mwb"

echo
echo "Moving notebooks..."
move_if_exists "eda.ipynb"                    "notebooks/eda.ipynb"

echo
echo "Renaming trade_dashboard/ -> dashboard/ ..."
if [[ -d "trade_dashboard" ]]; then
    if [[ -d "dashboard" ]] && [[ -z "$(ls -A dashboard 2>/dev/null)" ]]; then
        rmdir dashboard
    fi
    if [[ ! -e "dashboard/app.py" && ! -e "dashboard/lib" ]]; then
        $MV "trade_dashboard" "dashboard"
        echo "  moved  trade_dashboard/  ->  dashboard/"
    fi
fi

echo
echo "Moving legacy single-file Streamlit..."
move_if_exists "comtrade_streamlitapp.py"     "legacy/comtrade_streamlitapp.py"

echo
echo "NOT moving loader output directories (comtrade_output/, output/) -"
echo "the loaders default to those paths. They are now in .gitignore so future"
echo "runs will not bloat git. To untrack the currently-committed copies"
echo "without deleting them on disk:"
echo "    git rm --cached -r comtrade_output output"

echo
echo "Done."
echo
echo "Next steps:"
echo "  1. cp .env.example .env  (and fill in real values)"
echo "  2. git add -A && git commit -m 'Reorganize repo into subfolders'"
echo "  3. Re-run any loaders FROM THE PROJECT ROOT so relative paths resolve:"
echo "       python etl/reference/load_reference_tables.py"
echo "       python etl/comtrade/comtrade_granular_loader.py"
echo "       python etl/news/commodity_news_loader.py"