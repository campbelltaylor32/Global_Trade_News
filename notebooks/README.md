# Notebooks

Exploratory analyses against the warehouse. Notebooks are for discovery and one-off investigation — production logic belongs in `etl/` or the dashboard.

## Contents

| Notebook | Topic |
| --- | --- |
| `eda.ipynb` | Initial exploratory data analysis on the loaded Comtrade fact table. Distributions, coverage by reporter and year, sanity checks on `primary_value_usd`, partner concentration, etc. |

## Running

From the project root:

```bash
pip install jupyter ipykernel
python -m ipykernel install --user --name global-trade-news
jupyter lab notebooks/
```

Connection details for the warehouse live in `.env`. Notebooks should read credentials the same way the ETL scripts do — never hard-code passwords. The pattern:

```python
import os
from urllib.parse import quote_plus
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()
url = (
    f"mysql+pymysql://{quote_plus(os.environ['DB_USER'])}:"
    f"{quote_plus(os.environ['DB_PASS'])}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)
engine = create_engine(url, pool_pre_ping=True)
```

## Conventions

- **One question per notebook.** If an analysis grows past a single coherent question, split it.
- **Run-all-cells-clean.** Before committing, restart the kernel and run all cells; the notebook should execute top-to-bottom without errors.
- **Strip outputs from sensitive cells.** Anything that prints raw credentials or large PII-adjacent slices should be cleared before commit.
- **Promote useful logic.** If a query or transformation in a notebook gets reused, lift it into `dashboard/lib/features.py` or a new module under `etl/` rather than copy-pasting between notebooks.
