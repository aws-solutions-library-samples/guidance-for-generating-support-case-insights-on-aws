"""Trusted Advisor tool: read collected TA recommendations from S3.

The es-optira collector writes actionable (warning/error) Trusted Advisor check
results to S3 as one JSON object per (account, check):

    ta/{account_id}/{check_id}.json  ->  {"account_id": ..., "recommendation": {...}}

Trusted Advisor data is NOT ingested into the Knowledge Base and has no Athena
table, so this tool reads the ``ta/`` objects directly. The dataset is bounded
(accounts x checks, overwritten each run), so a direct S3 read is appropriate.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from .. import aws_clients
from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

TA_PREFIX = "ta/"
# Safety caps so a single response can't balloon.
_DEFAULT_MAX_RECOMMENDATIONS = 50
_MAX_FLAGGED_RESOURCES_PER_CHECK = 100


def _list_ta_keys(s3, bucket: str, prefix: str) -> List[str]:
    keys: List[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                keys.append(obj["Key"])
    return keys


def get_trusted_advisor_recommendations(
    account_id: Optional[str] = None,
    max_results: Optional[int] = None,
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    """Return collected Trusted Advisor recommendations from S3.

    Args:
        account_id: If provided, only that account's recommendations
            (``ta/{account_id}/``); otherwise all accounts.
        max_results: Max number of recommendations (checks) to return.

    Returns a dict with ``count``, ``truncated`` and a ``recommendations`` list.
    """
    settings = settings or get_settings()
    bucket = settings.support_data_bucket
    if not bucket:
        return {"error": "SUPPORT_DATA_BUCKET is not configured"}

    prefix = TA_PREFIX if not account_id else f"{TA_PREFIX}{account_id}/"
    limit = max_results or _DEFAULT_MAX_RECOMMENDATIONS
    s3 = aws_clients.s3(settings.aws_region)

    try:
        keys = _list_ta_keys(s3, bucket, prefix)
    except ClientError as exc:
        logger.error("Error listing Trusted Advisor objects: %s", exc)
        return {"error": f"Failed to list Trusted Advisor data: {exc}"}

    recommendations: List[Dict[str, Any]] = []
    for key in keys:
        if len(recommendations) >= limit:
            break
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            data = json.loads(obj["Body"].read())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping Trusted Advisor object %s: %s", key, exc)
            continue

        rec = data.get("recommendation", {}) or {}
        flagged = rec.get("flaggedResources", []) or []
        recommendations.append(
            {
                "account_id": data.get("account_id"),
                "check_id": rec.get("checkId"),
                "status": rec.get("status"),
                "description": rec.get("description"),
                "flagged_resources_count": len(flagged),
                "flagged_resources": flagged[:_MAX_FLAGGED_RESOURCES_PER_CHECK],
                "flagged_resources_truncated": len(flagged)
                > _MAX_FLAGGED_RESOURCES_PER_CHECK,
            }
        )

    return {
        "count": len(recommendations),
        "truncated": len(keys) > len(recommendations),
        "total_available": len(keys),
        "recommendations": recommendations,
    }
