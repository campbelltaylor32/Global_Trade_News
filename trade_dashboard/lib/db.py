import os
from urllib.parse import quote_plus
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

HOST = os.getenv("DB_HOST")
PORT = os.getenv("DB_PORT", "3306")
USER = os.getenv("DB_USER")
PASSWORD = os.getenv("DB_PASS")
DEFAULT_DATABASE = os.getenv("DEFAULT_DATABASE", "comtrade")
password = quote_plus(PASSWORD)

engine = create_engine(
    f"mysql+pymysql://{USER}:{password}@{HOST}:{PORT}/{DEFAULT_DATABASE}",
    pool_pre_ping=True
)

def run_query(sql: str):
    with engine.connect() as conn:
        result = conn.execute(text(sql))

        if result.returns_rows:
            return [dict(row._mapping) for row in result]

        return []