README – Backend Setup for Comtrade Database
Overview

This backend connects to the existing Comtrade Cloud SQL environment through Google's Cloud SQL MCP endpoint and uses OpenAI models to translate natural language questions into SQL queries.

The database configuration is already embedded in app.py, so developers do not need to configure project IDs, database names, instance names, or MCP endpoints.

Configured in app.py:

PROJECT_ID = "commoditytrade"
INSTANCE_NAME = "final"
REGION = "us-central1"
ZONE = "us-central1-a"
MCP_ENDPOINT = "https://sqladmin.googleapis.com/mcp"
DB_NAME = "comtrade"
Prerequisites
1. Python

Install Python 3.11 or newer.

Verify:

python --version
2. OpenAI API Key

Create an OpenAI API key and set it as an environment variable.

Windows
setx OPENAI_API_KEY "your-api-key"

Restart the terminal after setting.

macOS / Linux
export OPENAI_API_KEY="your-api-key"
3. Google Cloud Access

You must have access to the commoditytrade Google Cloud project.

Your Google account must be able to:

Authenticate with Google Cloud
Access Cloud SQL resources
Use the Cloud SQL MCP endpoint
Execute SQL through MCP tools
Google Authentication

Login with the Google account that has access to the project:

gcloud auth login

Then configure Application Default Credentials:

gcloud auth application-default login

Verify:

gcloud auth application-default print-access-token

A valid access token should be returned.

Install Dependencies

From the backend root directory:

pip install -r requirements.txt
Run the Backend

Start the FastAPI server:

uvicorn app:app --reload

Expected startup:

INFO: Uvicorn running on http://127.0.0.1:8000
Verify Backend Health

Open:

http://localhost:8000/health

Expected response:

{
  "status": "healthy"
}
Test the Chat Endpoint

Example request:

curl -X POST http://localhost:8000/chat \
-H "Content-Type: application/json" \
-d "{\"message\":\"Show me the top 3 commodities by volume\"}"

Example response:

{
  "response": "| Commodity | Volume |\n|-----------|--------|\n..."
}
Troubleshooting
OpenAI Authentication Error

Error:

401 Unauthorized

Verify:

echo $OPENAI_API_KEY

or on Windows:

echo $env:OPENAI_API_KEY

Ensure a valid OpenAI API key is configured.

Google Authentication Error

Error examples:

401 Unauthorized
403 Permission Denied

Run:

gcloud auth login
gcloud auth application-default login

Then retry.

MCP Tool Access Error

If SQL tool calls fail:

Confirm you are authenticated to the correct Google account.
Confirm your account has access to project:
commoditytrade
Verify Application Default Credentials are active:
gcloud auth application-default print-access-token
Dependency Issues

Reinstall dependencies:

pip install -r requirements.txt --upgrade
Notes
Database configuration is already embedded in app.py.
No local database setup is required.
No Cloud SQL connection strings need to be configured.
No .env variables are required for database connectivity.
Only an OpenAI API key and valid Google Cloud authentication are needed.