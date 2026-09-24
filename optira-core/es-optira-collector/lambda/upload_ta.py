"""Collect AWS Trusted Advisor recommendations across the organization.

Uses the modern **Trusted Advisor API** (`trustedadvisor` service), whose
resources include a first-class ``arn`` -- unlike the legacy Support API, whose
flaggedResources only carry an opaque resourceId. Each recommendation is stored
to S3 as one JSON object per (account, check):

    ta/{account_id}/{check_id}.json
        { "account_id": ..., "recommendation": { checkId, name, status,
          recommendationArn, flaggedResources:[{arn, awsResourceId,
          regionCode, status, metadata}], ... } }

The per-account collection mirrors the support-case collector: the current
account uses the local session; other org accounts are queried via an assumed
OrganizationAccountAccessRole.
"""
import json
import os
import random
import time
from collections import defaultdict

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

session = boto3.Session()

# The trustedadvisor service is global, reached via the us-east-1 endpoint.
TA_REGION = "us-east-1"
CROSS_ACCOUNT_ROLE = "OrganizationAccountAccessRole"

# Which recommendation statuses to collect. Trusted Advisor has no "severity";
# its statuses are ok | warning | error, where **error** is the red/critical
# finding and **warning** is yellow. Set TA_STATUSES to narrow collection, e.g.
# "error" to pull only critical recommendations (also cuts the number of
# ListRecommendationResources calls, easing throttling). Defaults to the
# actionable set (warning + error).
_VALID_STATUSES = {"ok", "warning", "error"}


def _statuses_from_env():
    raw = os.getenv("TA_STATUSES", "warning,error")
    statuses = {s.strip().lower() for s in raw.split(",") if s.strip()}
    invalid = statuses - _VALID_STATUSES
    if invalid:
        print(
            f"Ignoring invalid TA_STATUSES values {sorted(invalid)}; "
            f"valid values are {sorted(_VALID_STATUSES)}"
        )
    statuses &= _VALID_STATUSES
    return statuses or {"warning", "error"}


ACTIONABLE_STATUS = _statuses_from_env()

# The trustedadvisor API has low request rate limits, so let botocore retry
# throttled calls automatically (adaptive mode also rate-limits client-side).
TA_CLIENT_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

# Belt-and-suspenders backoff on top of botocore retries, for the paginated
# ListRecommendationResources calls that throttle most aggressively.
_THROTTLE_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "Throttling",
    "RequestLimitExceeded",
}
_MAX_RETRIES = 8


def _call_with_backoff(fn, **kwargs):
    """Call a boto3 operation, retrying throttling errors with backoff."""
    attempt = 0
    while True:
        try:
            return fn(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code not in _THROTTLE_CODES or attempt >= _MAX_RETRIES:
                raise
            # Exponential backoff with full jitter, capped at 20s.
            delay = min(20.0, (2 ** attempt)) + random.uniform(0, 1)
            print(
                f"Throttled ({code}); retry {attempt + 1}/{_MAX_RETRIES} "
                f"in {delay:.1f}s"
            )
            time.sleep(delay)
            attempt += 1


def _ta_client(credentials=None):
    """Trusted Advisor client for the local account or an assumed role."""
    if credentials:
        return session.client(
            "trustedadvisor",
            region_name=TA_REGION,
            config=TA_CLIENT_CONFIG,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )
    return session.client(
        "trustedadvisor", region_name=TA_REGION, config=TA_CLIENT_CONFIG
    )


def _check_id_from_arn(check_arn):
    """Extract the classic checkId from a check ARN.

    e.g. 'arn:aws:trustedadvisor:::check/rSs93HQwa1' -> 'rSs93HQwa1'.
    Returns None for recommendations that are not backed by a classic check.
    """
    if check_arn and "/" in check_arn:
        return check_arn.rsplit("/", 1)[-1]
    return None


def _list_recommendation_resources(ta, recommendation_arn):
    """Page through a recommendation's resources, keeping the ARN of each."""
    resources = []
    next_token = None
    while True:
        kwargs = {"recommendationIdentifier": recommendation_arn, "maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = _call_with_backoff(ta.list_recommendation_resources, **kwargs)
        for r in resp.get("recommendationResourceSummaries", []):
            resources.append(
                {
                    "arn": r.get("arn"),
                    "awsResourceId": r.get("awsResourceId"),
                    "regionCode": r.get("regionCode"),
                    "status": r.get("status"),
                    "metadata": r.get("metadata", {}),
                }
            )
        next_token = resp.get("nextToken")
        if not next_token:
            return resources


def _list_recommendation_summaries(ta, statuses):
    """List recommendation summaries, filtering by status server-side.

    The ListRecommendations ``status`` parameter is single-valued, so we make
    one paginated pass per requested status. Server-side filtering keeps the
    result set (and the follow-on ListRecommendationResources calls) small.
    """
    summaries = []
    for status in sorted(statuses):
        next_token = None
        while True:
            kwargs = {"maxResults": 100, "status": status}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = _call_with_backoff(ta.list_recommendations, **kwargs)
            summaries.extend(resp.get("recommendationSummaries", []))
            next_token = resp.get("nextToken")
            if not next_token:
                break
    return summaries


def get_ta_recommendations(ta_client=None):
    """Return actionable Trusted Advisor recommendations (with resource ARNs)."""
    ta = ta_client or _ta_client()

    summaries = _list_recommendation_summaries(ta, ACTIONABLE_STATUS)

    recommendations = []
    for s in summaries:
        # Safety net: server-side filtering already restricts to ACTIONABLE_STATUS.
        if (s.get("status") or "").lower() not in ACTIONABLE_STATUS:
            continue
        recommendation_arn = s.get("arn")
        check_arn = s.get("checkArn")
        recommendations.append(
            {
                "checkId": _check_id_from_arn(check_arn),
                "recommendationId": s.get("id"),
                "recommendationArn": recommendation_arn,
                "checkArn": check_arn,
                "name": s.get("name"),
                "status": s.get("status"),
                "pillars": s.get("pillars"),
                "awsServices": s.get("awsServices"),
                "source": s.get("source"),
                "resourcesAggregates": s.get("resourcesAggregates"),
                "flaggedResources": _list_recommendation_resources(
                    ta, recommendation_arn
                ),
            }
        )
    return recommendations


def get_organization_accounts():
    """Get all ACTIVE account ids in the organization."""
    org_client = session.client("organizations")
    accounts = []
    paginator = org_client.get_paginator("list_accounts")
    for page in paginator.paginate():
        accounts.extend(page["Accounts"])
    return [acc["Id"] for acc in accounts if acc["Status"] == "ACTIVE"]


def assume_role_in_account(account_id, role_name=CROSS_ACCOUNT_ROLE):
    """Assume the cross-account collection role in a target account."""
    sts_client = session.client("sts")
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    response = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"TACollection-{account_id}",
    )
    return response["Credentials"]


def get_ta_recommendations_for_account(credentials):
    """Pull Trusted Advisor recommendations using assumed-role credentials."""
    return get_ta_recommendations(_ta_client(credentials))


def save_to_s3(recommendations_by_account, bucket_name):
    """Write one object per (account, check): ta/{account_id}/{key}.json."""
    region = session.region_name
    s3 = session.client("s3", region_name=region)

    print(f"The TA recommendations are being uploaded to S3 bucket {bucket_name}...")
    for account_id, recommendations in recommendations_by_account.items():
        for rec in recommendations:
            # Prefer the classic checkId for the key (keeps the check-catalog
            # mapping working); fall back to the recommendation id.
            key_id = rec.get("checkId") or rec.get("recommendationId")
            if not key_id:
                continue
            obj = {"account_id": account_id, "recommendation": rec}
            file_key = f"ta/{account_id}/{key_id}.json"
            s3.put_object(
                Bucket=bucket_name,
                Key=file_key,
                Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            )
            print(f"Uploaded {file_key}")
    print("TA upload done!")


def upload_all_recommendations_to_s3(bucket_name, account_id):
    """Collect Trusted Advisor recommendations across ALL organization accounts.

    The current account is queried with the local session; every other org
    account is queried via an assumed OrganizationAccountAccessRole. Falls back
    to the current account only if the organization cannot be listed.
    """
    recommendations_by_account = defaultdict(list)

    try:
        org_accounts = get_organization_accounts()
        print(f"Found {len(org_accounts)} accounts in organization")
    except Exception as e:
        print(
            f"Could not list organization accounts ({e}); "
            "collecting Trusted Advisor for the current account only"
        )
        org_accounts = [account_id]

    for target_account_id in org_accounts:
        try:
            if target_account_id == account_id:
                recommendations = get_ta_recommendations()
            else:
                credentials = assume_role_in_account(target_account_id)
                recommendations = get_ta_recommendations_for_account(credentials)

            print(
                f"Found {len(recommendations)} actionable TA recommendations "
                f"in {target_account_id}"
            )
            recommendations_by_account[target_account_id].extend(recommendations)
        except Exception as e:
            print(
                f"Error collecting Trusted Advisor for account "
                f"{target_account_id}: {e}"
            )
            continue

    save_to_s3(recommendations_by_account, bucket_name)
