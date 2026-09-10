#!/usr/bin/env python3
"""CDK app: deploy the Optira MCP server for AWS DevOps Agent.

Architecture (serverless, scale-to-zero):

    AWS DevOps Agent --SigV4--> API Gateway (AWS_IAM auth, execute-api)
                                     |
                                     v
                              Lambda (non-VPC)
                        FastMCP stateless Streamable HTTP
                                     |
                     Bedrock . Athena . KB retrieve . Glue . S3

Why this shape:
- AWS DevOps Agent requires the Streamable HTTP transport and SigV4 auth.
- API Gateway with AWS_IAM authorization validates the SigV4 signature and the
  caller's IAM permissions before the request reaches Lambda.
- A non-VPC Lambda reaches the AWS service endpoints without a NAT gateway.
- No ALB, no NAT, no 24/7 Fargate task -> ~$0 fixed cost.

Deploy:
    python3 bin/package_for_lambda.py          # build app.zip + dependencies.zip
    cdk deploy --app "python3 app.py <support_bucket_name>" --require-approval never

Register the ``McpEndpointUrl`` output in the DevOps Agent console using AWS
SigV4 auth (Region from output, service name ``execute-api``) and the
``DevOpsAgentInvokeRoleArn`` role.
"""
import os

from aws_cdk import (
    App,
    CfnOutput,
    Duration,
    Stack,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGING_DIR = os.path.join(BASE_DIR, "packaging")
APP_ZIP = os.path.join(PACKAGING_DIR, "app.zip")
DEPS_ZIP = os.path.join(PACKAGING_DIR, "dependencies.zip")

# Verbatim copy of the SYSTEM_PROMPT set on the es-optira agent Lambda
# (agent-lambda-stack.ts), so the composite orchestrator tool behaves
# identically here. Used by the get_support_insights tool.
SYSTEM_PROMPT = (
    "You are an enterprise support specialist, get the relevant asked "
    "information from the tools available to you SPECIALLY case_aggregation "
    "and knowledge_insight.  To get insight use the Case ID from "
    "case_aggregation and check the knowledge base. Don\u2019t look at end of "
    "life and trusted advisor until it is asked explicitly."
)


class OptiraMcpServerStack(Stack):
    """Lambda + API Gateway (SigV4/IAM) MCP server for AWS DevOps Agent."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        support_bucket_name: str,
        kb_secret_name: str = "optira/knowledge-base-id",
        bedrock_model_id: str = "global.anthropic.claude-opus-4-7",
        athena_database: str = "optira_database",
        system_prompt: str = SYSTEM_PROMPT,
        kb_max_results: int = 25,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        if not (os.path.exists(APP_ZIP) and os.path.exists(DEPS_ZIP)):
            raise FileNotFoundError(
                "Missing build artifacts. Run 'python3 bin/package_for_lambda.py' "
                "before 'cdk deploy'."
            )

        # Dependencies as a layer to keep the function code readable/small.
        deps_layer = lambda_.LayerVersion(
            self,
            "OptiraMcpDependenciesLayer",
            code=lambda_.Code.from_asset(DEPS_ZIP),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            compatible_architectures=[lambda_.Architecture.ARM_64],
            description="Dependencies for the Optira MCP Lambda",
        )

        # KB id from the same Secrets Manager secret the agent Lambda uses.
        kb_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "KbSecret", kb_secret_name
        )

        mcp_function = lambda_.Function(
            self,
            "OptiraMcpFunction",
            function_name="OptiraMcpServer",
            description="Optira MCP server (Streamable HTTP) for AWS DevOps Agent",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="optira_mcp.lambda_handler.handler",
            code=lambda_.Code.from_asset(APP_ZIP),
            layers=[deps_layer],
            timeout=Duration.seconds(300),
            memory_size=1024,
            environment={
                "BEDROCK_MODEL_ID": bedrock_model_id,
                "ATHENA_DATABASE": athena_database,
                "ATHENA_OUTPUT_S3": f"s3://{support_bucket_name}/results/",
                # Bucket holding collector output; the Trusted Advisor tool reads
                # ta/ objects from here (support case data lives here too).
                "SUPPORT_DATA_BUCKET": support_bucket_name,
                "SYSTEM_PROMPT": system_prompt,
                # A case is fragmented into many small KB chunks; retrieve enough
                # to reconstruct a full communication thread (default 5 is too
                # few for the DevOps Agent path).
                "KB_MAX_RESULTS": str(kb_max_results),
                "KNOWLEDGEBASE_ID": kb_secret.secret_value_from_json(
                    "knowledge_base_id"
                ).unsafe_unwrap(),
            },
        )

        # --- Task/execution permissions (read-only where possible) ----------
        mcp_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Retrieve",
                ],
                resources=["*"],
            )
        )
        mcp_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "athena:StartQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                    "athena:StopQueryExecution",
                ],
                resources=[
                    f"arn:aws:athena:{self.region}:{self.account}:workgroup/primary",
                    f"arn:aws:athena:{self.region}:{self.account}:datacatalog/AwsDataCatalog",
                ],
            )
        )
        mcp_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "glue:GetDatabase",
                    "glue:GetTable",
                    "glue:GetPartitions",
                ],
                resources=[
                    f"arn:aws:glue:{self.region}:{self.account}:catalog",
                    f"arn:aws:glue:{self.region}:{self.account}:database/*",
                    f"arn:aws:glue:{self.region}:{self.account}:table/*",
                ],
            )
        )
        # Athena reads/writes query results in this bucket. Mirrors the working
        # agent Lambda: bucket-level actions (incl. s3:GetBucketLocation, which
        # Athena uses to verify the output bucket -- without it Athena fails
        # with "Unable to verify/create output bucket") and object-level actions.
        mcp_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetBucketLocation", "s3:ListBucket"],
                resources=[f"arn:aws:s3:::{support_bucket_name}"],
            )
        )
        mcp_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[f"arn:aws:s3:::{support_bucket_name}/*"],
            )
        )
        kb_secret.grant_read(mcp_function)

        # --- API Gateway with SigV4/IAM authorization ------------------------
        api = apigw.RestApi(
            self,
            "OptiraMcpApi",
            rest_api_name="OptiraMcpApi",
            description="Optira MCP server endpoint (SigV4/IAM) for AWS DevOps Agent",
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.REGIONAL]
            ),
            deploy_options=apigw.StageOptions(
                stage_name="prod",
                logging_level=apigw.MethodLoggingLevel.INFO,
            ),
            cloud_watch_role=True,
        )

        mcp_resource = api.root.add_resource("mcp")
        integration = apigw.LambdaIntegration(mcp_function, proxy=True)
        # Streamable HTTP primarily uses POST; GET/DELETE are included so any
        # compliant client probe is authenticated rather than anonymously 4xx'd.
        for method in ("POST", "GET", "DELETE"):
            mcp_resource.add_method(
                method,
                integration,
                authorization_type=apigw.AuthorizationType.IAM,
            )

        # --- IAM role that AWS DevOps Agent assumes to SigV4-sign requests ---
        # Trust policy per the DevOps Agent docs: only the aidevops service
        # principal, with confused-deputy protection (SourceAccount/SourceArn).
        devops_agent_role = iam.Role(
            self,
            "OptiraMcpDevOpsAgentInvokeRole",
            role_name="OptiraMcpDevOpsAgentInvokeRole",
            description=(
                "Role AWS DevOps Agent assumes to SigV4-sign requests to the "
                "Optira MCP API"
            ),
            assumed_by=iam.ServicePrincipal(
                "aidevops.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:aidevops:{self.region}:{self.account}:service/*"
                        )
                    },
                },
            ),
        )
        devops_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["execute-api:Invoke"],
                resources=[api.arn_for_execute_api()],
            )
        )

        # --- Outputs needed for DevOps Agent registration --------------------
        CfnOutput(
            self,
            "McpEndpointUrl",
            value=f"{api.url}mcp",
            description="MCP Streamable HTTP endpoint to register in AWS DevOps Agent",
        )
        CfnOutput(
            self,
            "DevOpsAgentInvokeRoleArn",
            value=devops_agent_role.role_arn,
            description="IAM role ARN to provide during SigV4 registration",
        )
        CfnOutput(
            self,
            "SigV4Service",
            value="execute-api",
            description="Service name to enter in the DevOps Agent SigV4 config",
        )
        CfnOutput(
            self, "SigV4Region", value=self.region, description="SigV4 signing Region"
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python app.py <support_bucket_name>")
        sys.exit(1)

    support_bucket = sys.argv[1]

    app = App()
    OptiraMcpServerStack(
        app,
        "OptiraMcpServerStack",
        support_bucket_name=support_bucket,
        description=(
            "Guidance for Generating Support Case Insights Using GenAI Services on AWS (SO9667)"
        ),
    )
    app.synth()
