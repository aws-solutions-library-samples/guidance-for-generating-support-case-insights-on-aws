"""Lazy, cached boto3 client factories.

Clients are created on first use (not at import time) and cached per
(service, region) so the module imports cleanly even without AWS credentials
present -- important for schema generation and unit tests.
"""
from __future__ import annotations

from functools import lru_cache

import boto3


@lru_cache(maxsize=8)
def _session(region: str) -> "boto3.Session":
    return boto3.Session(region_name=region)


@lru_cache(maxsize=32)
def _client(service: str, region: str):
    return _session(region).client(service)


def bedrock_runtime(region: str):
    """Client for Bedrock model inference (invoke_model)."""
    return _client("bedrock-runtime", region)


def bedrock_agent_runtime(region: str):
    """Client for Bedrock Knowledge Base retrieval (retrieve)."""
    return _client("bedrock-agent-runtime", region)


def athena(region: str):
    """Client for Amazon Athena query execution."""
    return _client("athena", region)


def s3(region: str):
    """Client for Amazon S3 (reads Trusted Advisor recommendation objects)."""
    return _client("s3", region)
