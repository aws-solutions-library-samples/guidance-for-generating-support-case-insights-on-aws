import json
from collections import defaultdict
import boto3

session = boto3.Session()

# Trusted Advisor / Support API is only available in us-east-1.
SUPPORT_REGION = "us-east-1"
CROSS_ACCOUNT_ROLE = "OrganizationAccountAccessRole"

# Load trustedadvisorchecksinfo.json
with open("ta_checks_info.json", "r", encoding="utf-8") as f:
    checks_info = json.load(f)

# Create a dictionary for easy lookup
checks_info_dict = {
    check["checkId"]: {"name": check["name"], "description": check["description"]}
    for check in checks_info
}


def save_to_s3(recommendations_by_account, bucket_name):
    region = session.region_name
    s3 = session.client("s3", region_name=region)

    print(f"The TA recommendations are being uploaded to S3 bucket {bucket_name}...")
    for account_id, recommendations in recommendations_by_account.items():
        for recommendation in recommendations:
            status = recommendation["recommendation"]["status"].lower()
            # Filter for warning or error status
            if status in [
                "warning",
                "error",
                "yellow",
                "red",
            ]:
                # Extract the checkId from the recommendation
                check_id = recommendation["recommendation"]["checkId"]
                # Get the description from the checks_info_dict
                description = checks_info_dict.get(check_id, {}).get(
                    "description", "No description provided"
                )
                # Update the recommendation with name and modified description
                recommendation["recommendation"][
                    "description"
                ] = f"The Trusted Advisor (TA) recommendation is for AWS account Id {account_id} that has TA status as '{status}'. This status {status} indicates the account owner should take action on the resources stated here as per this recommendation. The recommendation is as follows: {description}"
                recommendation_json = json.dumps(
                    recommendation, ensure_ascii=False
                ).encode("utf-8")
                # Construct the file key using account_id, date, and checkId
                file_key = f"ta/{account_id}/{check_id}.json"
                s3.put_object(
                    Bucket=bucket_name, Key=file_key, Body=recommendation_json
                )
                print(f"Uploaded {file_key}")
    print("TA upload done!")


def get_ta_recommendations(support_client=None):
    """Get Trusted Advisor check results using the given support client.

    Defaults to the current account's support client (us-east-1). Pass a client
    built from assumed cross-account credentials to collect another account's
    recommendations.
    """
    if support_client is None:
        support_client = session.client("support", region_name=SUPPORT_REGION)

    recommendations = []

    # Call describe_trusted_advisor_checks directly
    checks = support_client.describe_trusted_advisor_checks(language="en")["checks"]

    for check in checks:
        result = support_client.describe_trusted_advisor_check_result(
            checkId=check["id"], language="en"
        )
        recommendations.append(result["result"])

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
    """Build a support client from assumed credentials and pull TA results."""
    support_client = session.client(
        "support",
        region_name=SUPPORT_REGION,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
    )
    return get_ta_recommendations(support_client)


def upload_all_recommendations_to_s3(bucket_name, account_id):
    """Collect Trusted Advisor recommendations across ALL organization accounts.

    Mirrors the support-case collector: the current account is queried with the
    local session; every other org account is queried via an assumed
    OrganizationAccountAccessRole. Falls back to the current account only if the
    organization cannot be listed.
    """
    recommendations_by_account = defaultdict(list)

    # Determine which accounts to collect from.
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
                # Current account - use existing session
                recommendations = get_ta_recommendations()
            else:
                # Cross-account - assume role
                credentials = assume_role_in_account(target_account_id)
                recommendations = get_ta_recommendations_for_account(credentials)

            print(f"Finding TA recommendations in {target_account_id}")
            for recommendation in recommendations:
                recommendations_by_account[target_account_id].append(
                    {
                        "account_id": target_account_id,
                        "recommendation": recommendation,
                    }
                )
        except Exception as e:
            print(
                f"Error collecting Trusted Advisor for account "
                f"{target_account_id}: {e}"
            )
            continue

    save_to_s3(recommendations_by_account, bucket_name)
