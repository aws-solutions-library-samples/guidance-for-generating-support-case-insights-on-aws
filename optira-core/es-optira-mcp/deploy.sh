#!/bin/bash
#
# Standalone, opt-in deployment for the Optira MCP server.
#
# This is intentionally SEPARATE from optira-core/deploy.sh: the MCP server is
# an optional add-on (e.g. for AWS DevOps Agent integration), so it is only
# deployed by customers who want it. It does not touch the core Optira stacks.
#
# Prerequisites: the core Optira solution is already deployed (the support S3
# bucket and the optira/knowledge-base-id secret must exist), AWS credentials
# are configured, and Node.js + Python 3.12 are installed.
#
# Usage:
#   ./deploy.sh --bucket <support_bucket_name> [--region <region>]

set -e

BUCKET=""
REGION=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --bucket)
            BUCKET="$2"; shift 2 ;;
        --region)
            REGION="$2"; shift 2 ;;
        *)
            echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [ -z "$BUCKET" ]; then
    echo "ERROR: --bucket <support_bucket_name> is required."
    echo "Usage: ./deploy.sh --bucket <support_bucket_name> [--region <region>]"
    exit 1
fi

if [ -z "$REGION" ]; then
    REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
fi

# Run from the es-optira-mcp directory.
if [ ! -f "deploy.sh" ] || [ ! -d "infra" ]; then
    echo "ERROR: run this script from optira-core/es-optira-mcp/"
    exit 1
fi

echo "[INFO] Deploying Optira MCP server"
echo "[INFO] Bucket: $BUCKET"
echo "[INFO] Region: $REGION"

cd infra

echo "[INFO] Setting up CDK virtual environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

echo "[INFO] Building Lambda artifacts (app.zip + dependencies.zip)..."
python3 bin/package_for_lambda.py

echo "[INFO] Bootstrapping CDK (if needed)..."
npx cdk bootstrap --region "$REGION"

echo "[INFO] Deploying OptiraMcpServerStack..."
export AWS_DEFAULT_REGION="$REGION"
npx cdk deploy --app "python3 app.py $BUCKET" --require-approval never --region "$REGION"

echo ""
echo "[INFO] Deployment complete."
echo "[INFO] Stack outputs:"
aws cloudformation describe-stacks \
    --stack-name OptiraMcpServerStack \
    --query 'Stacks[0].Outputs' \
    --output table \
    --region "$REGION" 2>/dev/null || echo "[WARN] Could not fetch stack outputs."

echo ""
echo "[INFO] Next: register the McpEndpointUrl in the AWS DevOps Agent console"
echo "       using AWS SigV4 auth, service name 'execute-api', the Region above,"
echo "       and the DevOpsAgentInvokeRoleArn role. See README.md."
