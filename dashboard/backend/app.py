import json
import httpx
import google.auth
import google.auth.transport.requests
from pathlib import Path
from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

client = OpenAI()

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

schema_cache = None


class ChatRequest(BaseModel):
    message: str


def get_gcp_token():
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


async def call_mcp_tool(token: str, tool_name: str, arguments: dict):
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            "https://sqladmin.googleapis.com/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments
                }
            }
        )
        resp.raise_for_status()
        return resp.json()


async def get_schema(token: str) -> str:
    tables = [
        "fact_trade_granular_v2",
        "country_mapping",
        "commodity_code_mapping",
        "tradeflow_mapping"
    ]

    schema_parts = []
    for table in tables:
        try:
            result = await call_mcp_tool(token, "execute_sql_readonly", {
                "project": "commoditytrade",
                "instance": "final",
                "database": "comtrade",
                "sqlStatement": f"DESCRIBE {table}"
            })
            content = result.get("result", {}).get("content", [])
            text = content[0].get("text", "") if content else json.dumps(result)
            schema_parts.append(f"Table: {table}\n{text}")
        except Exception as e:
            print(f"Could not describe {table}: {e}")
            schema_parts.append(f"Table: {table}\n(schema unavailable)")

    return "\n\n".join(schema_parts)


@app.on_event("startup")
async def startup():
    global schema_cache
    try:
        token = get_gcp_token()
        schema_cache = await get_schema(token)
        print("=== SCHEMA LOADED ===")
        print(schema_cache)
        print("=====================")
    except Exception as e:
        print(f"WARNING: Could not load schema at startup: {e}")
        schema_cache = "Schema unavailable — use exact column names carefully."


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
@limiter.limit("10/minute")
async def chat(req: ChatRequest, request: Request):
    token = get_gcp_token()

    sql_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": f"""You are a MySQL expert for a trade analytics database called comtrade.

Here is the exact schema — use ONLY these column names:
{schema_cache}

Rules:
- Return ONLY a valid MySQL SELECT statement
- No markdown, no backticks, no explanation
- Always LIMIT results to 20 rows unless the question asks for something specific
- Join country_mapping to get country names when reporting on countries
- Use fact_trade_granular_v2 as the primary trade data table"""
            },
            {
                "role": "user",
                "content": req.message
            }
        ],
        max_tokens=300
    )

    sql = sql_response.choices[0].message.content.strip()
    print("GENERATED SQL:", sql)

    try:
        mcp_result = await call_mcp_tool(token, "execute_sql_readonly", {
            "project": "commoditytrade",
            "instance": "final",
            "database": "comtrade",
            "sqlStatement": sql
        })
        print("MCP RESULT:", mcp_result)

        content = mcp_result.get("result", {}).get("content", [])
        result_text = content[0].get("text", json.dumps(mcp_result, indent=2)) if content else json.dumps(mcp_result, indent=2)

    except httpx.HTTPStatusError as e:
        print("MCP ERROR:", e.response.text)
        return {"answer": f"Database error: {e.response.text}"}
    except Exception as e:
        print("MCP ERROR:", str(e))
        return {"answer": f"Error calling database: {str(e)}"}

    format_response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You are a trade analytics assistant. Given a SQL query and its results, summarize the answer in clear plain English. Be concise and direct."
            },
            {
                "role": "user",
                "content": f"Question: {req.message}\n\nSQL: {sql}\n\nResults: {result_text}"
            }
        ],
        max_tokens=200
    )

    answer = format_response.choices[0].message.content.strip()

    return {"answer": answer}