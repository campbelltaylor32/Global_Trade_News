import json
import httpx
import google.auth
import google.auth.transport.requests
from pathlib import Path
from fastapi import FastAPI, Request
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Load environment variables
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

client = OpenAI()
limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Configuration for your Cloud SQL setup
PROJECT_ID = "commoditytrade"
INSTANCE_NAME = "final"
REGION = "us-central1"
ZONE = "us-central1-a"
MCP_ENDPOINT = "https://sqladmin.googleapis.com/mcp"
DB_NAME = "comtrade"

# Table list for context
CORE_TABLES = ["fact_trade_granular_v2", "country_mapping", "commodity_code_mapping", "tradeflow_mapping", "frequency_mapping", "transport_mapping", "unit_quantity_mapping"]
NEWS_TABLES = ["news_articles", "news_events", "news_linking", "news_load_manifest"]
COMMODITY_TABLES = ["commodity_articles", "commodity_events", "commodity_search_terms"]

class ChatRequest(BaseModel):
    message: str

def get_gcp_token():
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


@app.get("/health")
def health():
    return {"status": "ok"}

async def get_mcp_tools(token: str):
    """Fetch tool definitions from Google Cloud MCP server and format for OpenAI."""
    async with httpx.AsyncClient() as http:
        resp = await http.post(
            MCP_ENDPOINT,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        resp.raise_for_status()
        mcp_data = resp.json()
        
        openai_tools = []
        for tool in mcp_data.get("result", {}).get("tools", []):
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["inputSchema"]
                }
            })
        return openai_tools

async def call_mcp_tool(token: str, tool_name: str, arguments: dict):
    """Execute the chosen tool on the Cloud SQL MCP server."""
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            MCP_ENDPOINT,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments}
            }
        )
        resp.raise_for_status()
        return resp.json()

@app.post("/chat")
@limiter.limit("10/minute")
async def chat_handler(request: Request, chat_req: ChatRequest):
    try:
        print(f"\n--- New Request: {chat_req.message} ---")
        token = get_gcp_token()
        tools = await get_mcp_tools(token)
        
        system_prompt = (
            f"You are a database assistant for project '{PROJECT_ID}'. "
            f"The active Cloud SQL instance is '{INSTANCE_NAME}' in '{ZONE}'. "
            f"The target database is '{DB_NAME}'. "
            f"When you query, you MUST use the database '{DB_NAME}'. "
            "You MUST use the 'execute_sql' tool and pass the query in the 'sql_statement' argument. "
            "Tables: country_mapping, fact_trade_granular_v2, commodity_code_mapping, etc."
        )

        system_prompt += " When the database returns data, summarize it in a Markdown table immediately."


        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": chat_req.message}
        ]

        # 1. Initial LLM call
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools
        )

        response_message = response.choices[0].message
        if not response_message.tool_calls:
            return {"response": response_message.content or "I processed the request but found no data."}

        # 2. Process Tool Calls
        messages.append(response_message)
        
        for tool_call in response_message.tool_calls:
            function_name = tool_call.function.name
            function_args = json.loads(tool_call.function.arguments)

            
            if function_name == "execute_sql":
                # 1. Capture the query from any potential key the LLM sent
                sql_query = (
                    function_args.get("sql_statement") or 
                    function_args.get("sqlStatement") or 
                    function_args.get("sql") or 
                    function_args.get("query")
                )
                
                if not sql_query:
                    # If LLM didn't provide one, default to a safe query
                    sql_query = f"SELECT * FROM {DB_NAME}.fact_trade_granular_v2 LIMIT 3;"
                
                # 2. Add 'USE database;' prefix
                if not sql_query.strip().lower().startswith("use"):
                    sql_query = f"USE {DB_NAME}; {sql_query}"
                
                # 3. CONSTRUCT THE EXACT ARGS FOR GOOGLE
                # We provide both snake_case and camelCase to ensure compatibility
                function_args = {
                    "project": PROJECT_ID,
                    "instance": INSTANCE_NAME,
                    "sql_statement": sql_query,
                    "sqlStatement": sql_query  # The key Google's internal API likely wants
                }
                print(f"DEBUG: FINAL SQL SENT: {sql_query}")


            # Execute the call
            mcp_response = await call_mcp_tool(token, function_name, function_args)

            is_error = mcp_response.get("result", {}).get("isError", False)
            try:
                actual_content = mcp_response["result"]["content"][0]["text"]
                if is_error:
                    actual_content = f"DATABASE ERROR: {actual_content}"
            except (KeyError, IndexError):
                actual_content = f"System Error: {json.dumps(mcp_response)}"

            print(f"DEBUG: Tool Output: {actual_content[:100]}...")

            messages.append({
                "tool_call_id": tool_call.id,
                "role": "tool",
                "name": function_name,
                "content": actual_content
            })

            # Detect SQL schema errors and allow one recovery attempt
        
        needs_retry = False

        for msg in reversed(messages):
            if (
                msg.get("role") == "tool"
                and "Unknown column" in msg.get("content", "")
            ):
                needs_retry = True
                break

        if needs_retry:
            print("DEBUG: Detected Unknown column error")
            print("DEBUG: Requesting schema inspection and query regeneration")

            messages.append({
                "role": "user",
                "content": (
                    f"The previous query failed because a column does not exist. "
                    f"First inspect the schema of fact_trade_granular_v2 using execute_sql. "
                    f"Then generate a corrected query and execute it."
                )
            })

            retry_response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=tools
            )

            retry_message = retry_response.choices[0].message
            messages.append(retry_message)

            if retry_message.tool_calls:

                print(
                    f"DEBUG: Retry generated {len(retry_message.tool_calls)} tool call(s)"
                )

                for tool_call in retry_message.tool_calls:

                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)

                    sql_query = (
                        function_args.get("sql_statement")
                        or function_args.get("sqlStatement")
                        or function_args.get("sql")
                        or function_args.get("query")
                    )

                    if sql_query and not sql_query.strip().lower().startswith("use"):
                        sql_query = f"USE {DB_NAME}; {sql_query}"

                    function_args = {
                        "project": PROJECT_ID,
                        "instance": INSTANCE_NAME,
                        "sql_statement": sql_query,
                        "sqlStatement": sql_query
                    }

                    print(f"DEBUG: RETRY SQL SENT: {sql_query}")

                    retry_tool_result = await call_mcp_tool(
                        token,
                        function_name,
                        function_args
                    )

                    try:
                        retry_content = (
                            retry_tool_result["result"]["content"][0]["text"]
                        )
                    except Exception:
                        retry_content = json.dumps(retry_tool_result)

                    print(
                        f"DEBUG: RETRY TOOL OUTPUT: {retry_content[:300]}"
                    )

                    messages.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": function_name,
                        "content": retry_content
                    })

        # Give GPT a chance to generate a corrected SQL query
        correction_response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools
        )

        correction_message = correction_response.choices[0].message

        print("DEBUG: CORRECTION MESSAGE:")
        print(correction_message)

        if correction_message.tool_calls:

            messages.append(correction_message)

            for tool_call in correction_message.tool_calls:

                function_args = json.loads(tool_call.function.arguments)

                sql_query = (
                    function_args.get("sql_statement")
                    or function_args.get("sqlStatement")
                    or function_args.get("sql")
                    or function_args.get("query")
                )

                if not sql_query.lower().startswith("use"):
                    sql_query = f"USE {DB_NAME}; {sql_query}"

                print(f"DEBUG: CORRECTED SQL SENT: {sql_query}")

                result = await call_mcp_tool(
                    token,
                    tool_call.function.name,
                    {
                        "project": PROJECT_ID,
                        "instance": INSTANCE_NAME,
                        "sql_statement": sql_query,
                        "sqlStatement": sql_query
                    }
                )

                content = result["result"]["content"][0]["text"]

                print(f"DEBUG: CORRECTED QUERY RESULT: {content[:500]}")

                messages.append({
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": tool_call.function.name,
                    "content": content
                })

        print("DEBUG: Requesting final answer")

        final_response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )

        answer = final_response.choices[0].message.content

        print(f"DEBUG: FINAL AI OUTPUT: {answer}")

        return {
            "response": answer or "Data retrieved, but I couldn't summarize it."
        }
        
    except Exception as e:
        print(f"CRITICAL ERROR: {str(e)}")
        return {"response": f"An error occurred in the backend: {str(e)}"}