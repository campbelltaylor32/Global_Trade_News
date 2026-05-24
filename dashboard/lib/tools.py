from sqlalchemy import text
from lib.db import engine

ALLOWED_DATABASES = [
    "comtrade",
    "comtrade_oltp",
    "ingestion",
    "raw",
    "raw_comtrade"
]

def list_databases():
    sql = """
    SELECT schema_name
    FROM information_schema.schemata
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql))
        return [r[0] for r in rows]

def list_tables(database: str):
    if database not in ALLOWED_DATABASES:
        raise ValueError("Database not allowed")

    sql = f"SHOW TABLES FROM `{database}`"

    with engine.connect() as conn:
        rows = conn.execute(text(sql))
        return [r[0] for r in rows]

def describe_table(database: str, table: str):
    if database not in ALLOWED_DATABASES:
        raise ValueError("Database not allowed")

    sql = f"DESCRIBE `{database}`.`{table}`"

    with engine.connect() as conn:
        rows = conn.execute(text(sql))
        return [dict(r._mapping) for r in rows]