#!/bin/bash

echo "Starting complete cleanup of Optira Core..."

# Set region
REGION=${1:-us-east-1}
echo "Using region: $REGION"

# Function to check if any Optira stacks exist
check_optira_stacks() {
    aws cloudformation list-stacks \
        --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_IN_PROGRESS UPDATE_ROLLBACK_FAILED UPDATE_ROLLBACK_COMPLETE CREATE_FAILED DELETE_FAILED \
        --query 'StackSummaries[?contains(StackName, `Optira`)].{Name:StackName,Status:StackStatus}' \
        --output text --region $REGION 2>/dev/null
}

# Function to get count of remaining Optira stacks
count_optira_stacks() {
    aws cloudformation list-stacks \
        --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_IN_PROGRESS UPDATE_ROLLBACK_FAILED UPDATE_ROLLBACK_COMPLETE CREATE_FAILED DELETE_FAILED \
        --query 'length(StackSummaries[?contains(StackName, `Optira`)])' \
        --output text --region $REGION 2>/dev/null
}

# 1. Delete all CloudFormation stacks
echo "Deleting CloudFormation stacks..."
# Delete from target region
aws cloudformation delete-stack --stack-name OptiraKnowledgeBaseStack --region $REGION 2>/dev/null || true
aws cloudformation delete-stack --stack-name OptiraCollectorStack --region $REGION 2>/dev/null || true
aws cloudformation delete-stack --stack-name OptiraMetadataStack --region $REGION 2>/dev/null || true
aws cloudformation delete-stack --stack-name OptiraAgentLambdaStack --region $REGION 2>/dev/null || true
aws cloudformation delete-stack --stack-name OptiraDataPipelineStack --region $REGION 2>/dev/null || true
aws cloudformation delete-stack --stack-name OptiraAgentStack --region $REGION 2>/dev/null || true
aws cloudformation delete-stack --stack-name OptiraStack --region $REGION 2>/dev/null || true
# MCP server (optional standalone stack). Delete the stack first so its Lambda
# and IAM roles are not orphaned by the generic Optira-name sweep below.
aws cloudformation delete-stack --stack-name OptiraMcpServerStack --region $REGION 2>/dev/null || true

# Also delete from us-west-2 (common default region)
if [ "$REGION" != "us-west-2" ]; then
    echo "Also cleaning up us-west-2 region..."
    aws cloudformation delete-stack --stack-name OptiraKnowledgeBaseStack --region us-west-2 2>/dev/null || true
    aws cloudformation delete-stack --stack-name OptiraCollectorStack --region us-west-2 2>/dev/null || true
    aws cloudformation delete-stack --stack-name OptiraMetadataStack --region us-west-2 2>/dev/null || true
    aws cloudformation delete-stack --stack-name OptiraAgentLambdaStack --region us-west-2 2>/dev/null || true
    aws cloudformation delete-stack --stack-name OptiraDataPipelineStack --region us-west-2 2>/dev/null || true
    aws cloudformation delete-stack --stack-name OptiraAgentStack --region us-west-2 2>/dev/null || true
    aws cloudformation delete-stack --stack-name OptiraStack --region us-west-2 2>/dev/null || true
    aws cloudformation delete-stack --stack-name OptiraMcpServerStack --region us-west-2 2>/dev/null || true
fi

# Force destroy CDK cached state in each component
echo "Destroying CDK cached state..."
for dir in es-optira es-optira-collector es-optira-data-pipeline es-optira-kb; do
    if [ -d "$dir" ]; then
        echo "Force destroying CDK state in $dir..."
        (cd "$dir" && cdk destroy --force --region $REGION 2>/dev/null || true)
        # Also try destroying with specific stack names
        (cd "$dir" && cdk destroy OptiraKnowledgeBaseStack --force --region $REGION 2>/dev/null || true)
        (cd "$dir" && cdk destroy OptiraCollectorStack --force --region $REGION 2>/dev/null || true)
        (cd "$dir" && cdk destroy OptiraMetadataStack --force --region $REGION 2>/dev/null || true)
        (cd "$dir" && cdk destroy OptiraAgentLambdaStack --force --region $REGION 2>/dev/null || true)
        (cd "$dir" && cdk destroy OptiraDataPipelineStack --force --region $REGION 2>/dev/null || true)
    fi
done

# Reset CDK bootstrap if needed
echo "Resetting CDK bootstrap state..."
cdk bootstrap --force --region $REGION 2>/dev/null || true

# Monitor stack deletion with polling
echo "Monitoring stack deletion progress..."
INITIAL_COUNT=$(count_optira_stacks)
if [ "$INITIAL_COUNT" -gt 0 ]; then
    echo "Found $INITIAL_COUNT Optira stack(s) to delete"
    
    POLL_COUNT=0
    MAX_POLLS=60  # 30 minutes maximum wait (30 seconds * 60)
    
    while [ $POLL_COUNT -lt $MAX_POLLS ]; do
        REMAINING_COUNT=$(count_optira_stacks)
        
        if [ "$REMAINING_COUNT" -eq 0 ]; then
            echo "All Optira stacks have been deleted successfully!"
            break
        fi
        
        echo "[$((POLL_COUNT + 1))/60] Still waiting... $REMAINING_COUNT stack(s) remaining:"
        check_optira_stacks | while read name status; do
            if [ -n "$name" ]; then
                echo "  - $name: $status"
            fi
        done
        
        sleep 30
        POLL_COUNT=$((POLL_COUNT + 1))
    done
    
    if [ $POLL_COUNT -eq $MAX_POLLS ]; then
        echo "WARNING: Timeout reached. Some stacks may still be deleting:"
        check_optira_stacks
        echo "You may need to check AWS Console or wait longer before redeploying."
    fi
else
    echo "No Optira stacks found to delete"
fi

# 2. Delete S3 buckets (only ones created by Optira, not user-provided existing ones)
echo "Cleaning up S3 buckets..."
echo "WARNING: This will only delete buckets that were CREATED by Optira deployment."
echo "User-provided existing buckets will be preserved."

# Get list of buckets that might be Optira-related
OPTIRA_BUCKETS=$(aws s3api list-buckets --query 'Buckets[?contains(Name, `optira`)].Name' --output text --region $REGION 2>/dev/null)

if [ -n "$OPTIRA_BUCKETS" ]; then
    echo "Found potential Optira buckets. Checking which ones are safe to delete..."
    
    for bucket in $OPTIRA_BUCKETS; do
        # Check if bucket has CloudFormation tags (indicating it was created by CDK)
        STACK_TAG=$(aws s3api get-bucket-tagging --bucket $bucket --query 'TagSet[?Key==`aws:cloudformation:stack-name`].Value' --output text --region $REGION 2>/dev/null)
        
        if [ -n "$STACK_TAG" ] && [[ "$STACK_TAG" == *"Optira"* ]]; then
            echo "Deleting CDK-created bucket: $bucket (from stack: $STACK_TAG)"
            aws s3 rm s3://$bucket --recursive --region $REGION 2>/dev/null || true
            aws s3api delete-bucket --bucket $bucket --region $REGION 2>/dev/null || true
        else
            echo "PRESERVING bucket: $bucket (appears to be user-provided existing bucket)"
        fi
    done
else
    echo "No Optira-related buckets found"
fi

# 3. Delete IAM roles and policies
echo "Cleaning up IAM roles..."
for role in $(aws iam list-roles --query 'Roles[?contains(RoleName, `Optira`)].RoleName' --output text 2>/dev/null); do
    echo "Deleting IAM role: $role"
    # Detach managed policies
    aws iam list-attached-role-policies --role-name $role --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null | xargs -n1 -I {} aws iam detach-role-policy --role-name $role --policy-arn {} 2>/dev/null || true
    # Delete inline policies
    aws iam list-role-policies --role-name $role --query 'PolicyNames' --output text 2>/dev/null | xargs -n1 -I {} aws iam delete-role-policy --role-name $role --policy-name {} 2>/dev/null || true
    # Delete role
    aws iam delete-role --role-name $role 2>/dev/null || true
done

# 4. Delete Lambda functions
echo "Cleaning up Lambda functions..."
for func in $(aws lambda list-functions --query 'Functions[?contains(FunctionName, `optira`) || contains(FunctionName, `Optira`)].FunctionName' --output text --region $REGION 2>/dev/null); do
    echo "Deleting Lambda function: $func"
    aws lambda delete-function --function-name $func --region $REGION 2>/dev/null || true
done

# 5. Delete Bedrock Knowledge Bases
echo "Cleaning up Bedrock Knowledge Bases..."
for kb in $(aws bedrock-agent list-knowledge-bases --query 'knowledgeBaseSummaries[?contains(name, `optira-support-case-kb`) || contains(name, `optira`) || contains(name, `Optira`)].knowledgeBaseId' --output text --region $REGION 2>/dev/null); do
    echo "Deleting Knowledge Base: $kb"
    aws bedrock-agent delete-knowledge-base --knowledge-base-id $kb --region $REGION 2>/dev/null || true
done

# 6. Delete OpenSearch Serverless collections
echo "Cleaning up OpenSearch Serverless collections..."
for collection in $(aws opensearchserverless list-collections --query 'collectionSummaries[?contains(name, `bedrock-kb-`) || contains(name, `optira`) || contains(name, `Optira`)].name' --output text --region $REGION 2>/dev/null); do
    echo "Deleting OpenSearch collection: $collection"
    aws opensearchserverless delete-collection --name $collection --region $REGION 2>/dev/null || true
done

# 7. Clean up local environment
echo "Cleaning up local environment..."

# Remove virtual environments
sudo rm -rf .venv
find . -name ".venv" -type d -exec sudo rm -rf {} + 2>/dev/null || true

# Remove CDK outputs and context
sudo rm -rf cdk.out
find . -name "cdk.out" -type d -exec sudo rm -rf {} + 2>/dev/null || true
find . -name "cdk.context.json" -delete 2>/dev/null || true

# Clear CDK context in each component directory
for dir in es-optira es-optira-collector es-optira-data-pipeline es-optira-kb; do
    if [ -d "$dir" ]; then
        echo "Clearing CDK context in $dir..."
        (cd "$dir" && cdk context --clear 2>/dev/null || true)
    fi
done

# Remove node_modules
sudo rm -rf node_modules
find . -name "node_modules" -type d -exec sudo rm -rf {} + 2>/dev/null || true

# Remove Python cache
find . -name "__pycache__" -type d -exec sudo rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true

# Clean npm and CDK caches
sudo rm -rf ~/.npm
sudo rm -rf ~/Library/Caches/com.amazonaws.jsii/
sudo rm -rf ~/.cdk

# Remove package-lock files
find . -name "package-lock.json" -delete 2>/dev/null || true

echo "Cleanup complete! All Optira resources have been removed."
echo "You can now start fresh with: ./deploy.sh --region $REGION"
