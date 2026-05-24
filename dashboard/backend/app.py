import json
from pathlib import Path
from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
import os 

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

from lib.tools import list_databases, list_tables, describe_table
api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=api_key)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class ChatRequest(BaseModel):
    message: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
@limiter.limit("10/minute")
async def chat(req: ChatRequest, request: Request):
    databases = list_databases()

    system_prompt = f"""
You are a trade analytics assistant.

Available databases:
{json.dumps(databases, indent=2)}

Help users understand the schema and data.
"""

    response = client.responses.create(
        model="gpt-4o",
        instructions=system_prompt,
        input=req.message,
        max_output_tokens=500
    )

    return {"answer": response.output_text}
