#!/usr/bin/env python3
from aws_cdk import (
    App, Stack, CfnOutput,
    aws_iam as iam,
    aws_s3 as s3,
)
from constructs import Construct

class OptiraKnowledgeBaseStack(Stack):
    """CDK Stack for Amazon Bedrock Knowledge Base infrastructure"""
    
    def __init__(self, scope: Construct, id: str, bucket_name: str, description: str = None, **kwargs) -> None:
        if description:
            kwargs['description'] = description
        super().__init__(scope, id, **kwargs)
        
        # Reference existing S3 bucket
        kb_bucket = s3.Bucket.from_bucket_name(
            self, "ExistingKnowledgeBaseBucket",
            bucket_name=bucket_name
        )
        
        # Create IAM role for Bedrock Knowledge Base
        kb_role = iam.Role(
            self, "OptiraBedrockKnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="Role for Bedrock Knowledge Base to access S3 and OpenSearch",
            # Add inline policies instead of using add_to_policy to avoid dependency issues
            inline_policies={
                "S3AccessPolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "s3:GetObject",
                                "s3:ListBucket"
                            ],
                            resources=[
                                kb_bucket.bucket_arn,
                                f"{kb_bucket.bucket_arn}/*"
                            ]
                        )
                    ]
                ),
                "OpenSearchPolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[ 
                                "aoss:APIAccessAll",
                                "aoss:BatchGetCollection",
                                "aoss:CreateIndex",
                                "aoss:DeleteIndex",
                                "aoss:UpdateIndex",
                                "aoss:DescribeIndex",
                                "aoss:ReadDocument",
                                "aoss:WriteDocument",
                                "aoss:CreateCollectionItems",
                                "aoss:DeleteCollectionItems",
                                "aoss:UpdateCollectionItems",
                                "aoss:DescribeCollectionItems"
                            ],
                            resources=[
                                f"arn:aws:aoss:{self.region}:{self.account}:collection/*",
                                f"arn:aws:aoss:{self.region}:{self.account}:index/*/*"
                            ]
                        )
                    ]
                ),
                "BedrockPolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="BedrockInvokeModelStatement",
                            actions=[
                                "bedrock:InvokeModel",
                                "bedrock:GetFoundationModel",
                                "bedrock:GetInferenceProfile",
                                "bedrock:ListFoundationModels",
                                "bedrock:InvokeModelWithResponseStream"
                            ],
                            resources=[
                                f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0",
                                f"arn:aws:bedrock:*::foundation-model/*",
                                f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*"
                            ]
                        )
                    ]
                ),
                "SecretsManagerPolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            actions=[
                                "secretsmanager:CreateSecret",
                                "secretsmanager:UpdateSecret",
                                "secretsmanager:GetSecretValue",
                                "secretsmanager:DescribeSecret"
                            ],
                            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:optira/*"]
                        )
                    ]
                )
            }
        )
        
        # Outputs
        CfnOutput(self, "KnowledgeBaseBucketName", value=kb_bucket.bucket_name)
        CfnOutput(self, "KnowledgeBaseRoleArn", value=kb_role.role_arn)

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python kb_cdk.py <bucket_name>")
        sys.exit(1)
        
    bucket_name = sys.argv[1]
    app = App()
    OptiraKnowledgeBaseStack(
        app, 
        "OptiraKnowledgeBaseStack", 
        bucket_name=bucket_name,
        description="Guidance for Generating Support Case Insights Using GenAI Services on AWS (SO9667)"
    )
    app.synth()