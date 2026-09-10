"""Case metadata tool: natural language -> Athena SQL -> results.

Ported from the Lambda modules ``caseAggregationTool.py``, ``bedrockAPI.py``
and ``queryExecutor.py``, decoupled from module-level env reads. The return
value matches the es-optira ``case_aggregation`` tool exactly (statusCode /
headers / JSON-string body with the raw Athena results).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from typing import Any, Dict, Optional

from .. import aws_clients
from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

_ATHENA_POLL_START = 1.0  # seconds
_ATHENA_POLL_MAX = 5.0    # seconds


def _build_sql_prompt(user_query: str) -> str:
    """Prompt template for NL -> Athena SQL (mirrors get_case_prompt)."""
    return (
        "You are an SQL expert familiar with AWS Athena. "
        "Using the database 'optira_database', which contains table "
        "'case_metadata' (fields: account_id, caseId, timeCreated, "
        "severityCode, status, subject, categoryCode, serviceCode), generate "
        "an Athena SQL query matching the following natural language request: "
        f"'{user_query}'. Important rules: "
        "(1) Always use the SQL LIKE operator (not '=') with wildcards ('%') "
        "when filtering the field 'serviceCode'. "
        "(2) Use plain string literals for date conditions (e.g., "
        "'YYYY-MM-DD') rather than TIMESTAMP literals. "
        "(3) Return only the SQL query without commentary. "
        "(4) When filtering for specific dates, use "
        "SUBSTRING(timeCreated, 1, 10) = 'YYYY-MM-DD' format. "
        "(5) Severity is either of the following: high, low, normal, urgent, "
        "critical. "
        "(6) Always use LOWER() function when matching serviceCode to ensure "
        "case-insensitive comparison. "
        "(7) If there is mention of UTC then (otherwise ignore this rule): you "
        "can use from_iso8601_timestamp. "
        "Keep it simple, dont use timezone function. DONT USE ANY MARKDOWN)"
    )


def _generate_sql(user_query: str, settings: Settings) -> Optional[str]:
    """Ask Bedrock (Claude messages API) to produce an Athena SQL query."""
    client = aws_clients.bedrock_runtime(settings.aws_region)
    system_prompt = (
        "You are a SQL expert with extensive experience writing queries for "
        "AWS Athena."
    )
    combined_prompt = f"{system_prompt}\n\n{_build_sql_prompt(user_query)}"
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "messages": [{"role": "user", "content": combined_prompt}],
            # Matches es-optira bedrockAPI.py, which hardcodes MAX_TOKENS = 1000
            # for SQL generation (independent of the KB synthesis token budget).
            "max_tokens": 1000,
        }
    )
    try:
        response = client.invoke_model(body=body, modelId=settings.bedrock_model_id)
        payload = json.loads(response["body"].read())
        content: Any = payload.get("content", "")
        if isinstance(content, list):
            parts = [
                item.get("text", str(item)) if isinstance(item, dict) else str(item)
                for item in content
            ]
            content = " ".join(parts)
        return content.strip()
    except Exception as exc:  # noqa: BLE001 - surface as tool error, don't crash server
        logger.error("Bedrock SQL generation failed: %s", exc)
        return None


def _run_athena(sql: str, settings: Settings) -> Dict[str, Any]:
    """Execute an Athena query with exponential-backoff polling."""
    client = aws_clients.athena(settings.aws_region)
    try:
        response = client.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": settings.athena_database},
            ResultConfiguration={"OutputLocation": settings.athena_output_s3},
        )
        query_id = response["QueryExecutionId"]
        start = time.time()
        interval = _ATHENA_POLL_START

        while True:
            execution = client.get_query_execution(QueryExecutionId=query_id)
            status = execution["QueryExecution"]["Status"]["State"]

            if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break

            if time.time() - start > settings.max_query_execution_time:
                client.stop_query_execution(QueryExecutionId=query_id)
                return {
                    "error": (
                        f"Query execution timed out after "
                        f"{settings.max_query_execution_time} seconds"
                    )
                }

            time.sleep(min(interval, _ATHENA_POLL_MAX))
            interval *= 2

        if status == "SUCCEEDED":
            return client.get_query_results(QueryExecutionId=query_id)

        if status == "FAILED":
            reason = (
                execution["QueryExecution"]["Status"].get(
                    "StateChangeReason", "Unknown error"
                )
            )
            return {"error": f"Query failed: {reason}"}

        return {"error": "Query was cancelled"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def query_case_metadata(
    query: str, settings: Optional[Settings] = None
) -> Dict[str, Any]:
    """Translate a natural language question into Athena SQL and run it.

    Returns the SAME response shape as the es-optira ``case_aggregation`` tool:
    an API-Gateway-style dict with ``statusCode``, ``headers`` and a JSON-string
    ``body``. On success the body contains ``generated_query`` and the raw
    Athena ``athena_results`` payload; on failure it contains an ``error``.
    """
    settings = settings or get_settings()
    user_query = query

    if not user_query:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "No query provided in the event"}),
        }

    user_query = urllib.parse.unquote(user_query)

    sql_query = _generate_sql(user_query, settings)
    if not sql_query:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {"error": "Failed to generate SQL query from Bedrock"}
            ),
        }

    logger.info("Generated SQL Query: %s", sql_query)
    athena_results = _run_athena(sql_query, settings)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {"generated_query": sql_query, "athena_results": athena_results}
        ),
    }
