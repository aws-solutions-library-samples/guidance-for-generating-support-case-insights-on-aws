"""Host-agnostic configuration for the Optira MCP server.

All settings are resolved from environment variables at startup (not at import
time), so the same tool code runs unchanged under stdio, HTTP, a container, or
Lambda. Defaults mirror the values injected by the existing CDK stack.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache

from .aws_clients import secrets_manager

logger = logging.getLogger(__name__)

# Default Secrets Manager secret holding the Bedrock Knowledge Base id. The KB
# creation flow (es-optira-kb) writes {"knowledge_base_id": "..."} here, so the
# MCP server resolves the current id at runtime even when deployed after the KB.
DEFAULT_KB_SECRET_NAME = "optira/knowledge-base-id"

# Verbatim copy of the SYSTEM_PROMPT env var set on the es-optira agent Lambda
# (agent-lambda-stack.ts). Used only as a fallback -- the MCP Lambda stack sets
# SYSTEM_PROMPT explicitly, matching the agent for identical behavior.
DEFAULT_SYSTEM_PROMPT = (
    "You are an enterprise support specialist, get the relevant asked "
    "information from the tools available to you SPECIALLY case_aggregation "
    "and knowledge_insight.  To get insight use the Case ID from "
    "case_aggregation and check the knowledge base. Don\u2019t look at end of "
    "life and trusted advisor until it is asked explicitly."
)


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration."""

    aws_region: str
    bedrock_model_id: str
    knowledge_base_id: str
    kb_secret_name: str
    athena_database: str
    athena_output_s3: str
    support_data_bucket: str
    max_tokens: int
    kb_max_results: int
    max_query_execution_time: int
    system_prompt: str

    @classmethod
    def from_env(cls) -> "Settings":
        region = (
            os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        athena_output_s3 = os.getenv("ATHENA_OUTPUT_S3", "")
        kb_secret_name = os.getenv("KB_SECRET_NAME", DEFAULT_KB_SECRET_NAME)
        return cls(
            aws_region=region,
            bedrock_model_id=os.getenv(
                "BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-7"
            ),
            # Explicit env override wins (local/stdio/tests); otherwise resolve
            # the current KB id from Secrets Manager at runtime.
            knowledge_base_id=_resolve_kb_id(region, kb_secret_name),
            kb_secret_name=kb_secret_name,
            athena_database=os.getenv("ATHENA_DATABASE", "optira_database"),
            athena_output_s3=athena_output_s3,
            # Bucket holding collector output (support-cases/ and ta/). Falls
            # back to the bucket parsed from ATHENA_OUTPUT_S3 (s3://<bucket>/...).
            support_data_bucket=(
                os.getenv("SUPPORT_DATA_BUCKET", "")
                or _bucket_from_s3_uri(athena_output_s3)
            ),
            max_tokens=_int_env("MAX_TOKENS", 2000),
            kb_max_results=_int_env("KB_MAX_RESULTS", 5),
            max_query_execution_time=_int_env("MAX_QUERY_EXECUTION_TIME", 300),
            system_prompt=os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        )


def _resolve_kb_id(region: str, secret_name: str) -> str:
    """Resolve the Bedrock Knowledge Base id.

    Order of precedence:
      1. The ``KNOWLEDGEBASE_ID`` env var, if set (local/stdio/tests/override).
      2. The Secrets Manager secret ``secret_name`` (JSON key
         ``knowledge_base_id``), resolved at runtime.

    Any failure to read the secret is logged and yields ``""`` so the server
    still starts (tools relying on the KB will simply return empty results).
    """
    env_override = os.getenv("KNOWLEDGEBASE_ID", "").strip()
    if env_override:
        return env_override

    try:
        response = secrets_manager(region).get_secret_value(SecretId=secret_name)
        secret_string = response.get("SecretString", "")
        if not secret_string:
            logger.warning(
                "Secret %s has no SecretString; KB id unresolved", secret_name
            )
            return ""
        payload = json.loads(secret_string)
        kb_id = str(payload.get("knowledge_base_id", "")).strip()
        if not kb_id:
            logger.warning(
                "Secret %s missing 'knowledge_base_id' key; KB id unresolved",
                secret_name,
            )
        return kb_id
    except Exception as exc:  # noqa: BLE001 - graceful degradation on any error
        logger.warning(
            "Could not resolve Knowledge Base id from secret %s: %s",
            secret_name,
            exc,
        )
        return ""


def _bucket_from_s3_uri(uri: str) -> str:
    """Extract the bucket name from an s3://bucket/prefix URI (or return "")."""
    if uri.startswith("s3://"):
        return uri[len("s3://"):].split("/", 1)[0]
    return ""


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton (resolved once)."""
    return Settings.from_env()
