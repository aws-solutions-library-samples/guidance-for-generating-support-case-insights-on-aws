import { Duration, Stack, StackProps, SymlinkFollowMode } from "aws-cdk-lib";
import { Construct } from "constructs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as iam from "aws-cdk-lib/aws-iam";
import * as path from "path";
import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';

export class OptiraAgentLambdaStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    const packagingDirectory = path.join(__dirname, "../packaging");

    const zipDependencies = path.join(packagingDirectory, "dependencies.zip");
    const zipApp = path.join(packagingDirectory, "app.zip");

    const supportDataBucketName = new cdk.CfnParameter(this, 'SupportDataBucket', {
      type: 'String',
      description: 'Name of the S3 bucket for support data'
    });

    const createNewBucket = new cdk.CfnParameter(this, 'CreateNewBucket', {
      type: 'String',
      default: 'true',
      allowedValues: ['true', 'false'],
      description: 'Create new S3 bucket (true) or use existing bucket (false)'
    });

    const enableWaf = new cdk.CfnParameter(this, 'EnableWAF', {
      type: 'String',
      default: 'false',
      allowedValues: ['true', 'false'],
      description: 'Enable AWS WAF for DDoS protection (true) or disable (false)'
    });

    // Create a lambda layer with dependencies to keep the code readable in the Lambda console
    const dependenciesLayer = new lambda.LayerVersion(this, "OptiraAgentDependenciesLayer", {
      code: lambda.Code.fromAsset(zipDependencies),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      description: "Dependencies needed for agent-based lambda",
    });

    // Define the Lambda function
    const OptiraAgentFunction = new lambda.Function(this, "OptiraAgentLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      functionName: "OptiraAgentFunction",
      description: "A function that invokes support insight agent with case, event and knowledge base tools",
      handler: "lambda_function.lambda_handler",
      code: lambda.Code.fromAsset(zipApp),

      timeout: Duration.seconds(300),
      memorySize: 10240,
      layers: [dependenciesLayer],
      architecture: lambda.Architecture.ARM_64,
    });
    
    const knowledgeBaseSecret = secretsmanager.Secret.fromSecretNameV2(this, 'KnowledgeBaseSecret', 'optira/knowledge-base-id');
    
    
    OptiraAgentFunction.addEnvironment("ATHENA_DATABASE", "optira_database")
    OptiraAgentFunction.addEnvironment("ATHENA_OUTPUT_S3", `s3://${supportDataBucketName.valueAsString}/results/`)
    OptiraAgentFunction.addEnvironment("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-7")
    OptiraAgentFunction.addEnvironment("TRUSTED_ADVISOR_MODEL_ID", "global.anthropic.claude-opus-4-7")
    OptiraAgentFunction.addEnvironment("KNOWLEDGEBASE_ID", knowledgeBaseSecret.secretValueFromJson('knowledge_base_id').unsafeUnwrap())
    OptiraAgentFunction.addEnvironment("MAX_PARALLEL_TOOLS", "3")
    OptiraAgentFunction.addEnvironment("MAX_QUERY_EXECUTION_TIME", "300")
    OptiraAgentFunction.addEnvironment("MAX_TOKENS", "2000")
    OptiraAgentFunction.addEnvironment("SYSTEM_PROMPT", "You are an enterprise support specialist, get the relevant asked information from the tools available to you SPECIALLY case_aggregation and knowledge_insight.  To get insight use the Case ID from case_aggregation and check the knowledge base. Don’t look at end of life and trusted advisor until it is asked explicitly.")

    // Add permissions for the Lambda function to invoke Bedrock APIs
    OptiraAgentFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Retrieve"],
        resources: ["*"],
      }),
    );

    // Add Secrets Manager permissions
    knowledgeBaseSecret.grantRead(OptiraAgentFunction);

    // Add basic Lambda execution permissions
    OptiraAgentFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents'
        ],
        resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:/aws/lambda/OptiraAgentFunction*`]
      })
    );

    // Add Athena permissions
    OptiraAgentFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'athena:StartQueryExecution',
          'athena:GetQueryExecution',
          'athena:GetQueryResults',
          'athena:StopQueryExecution'
        ],
        resources: [
          `arn:aws:athena:${this.region}:${this.account}:workgroup/primary`,
          `arn:aws:athena:${this.region}:${this.account}:datacatalog/AwsDataCatalog`
        ]
      })
    );

    // Add Glue permissions for Athena metadata
    OptiraAgentFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'glue:GetDatabase',
          'glue:GetTable',
          'glue:GetPartitions'
        ],
        resources: [
          `arn:aws:glue:${this.region}:${this.account}:catalog`,
          `arn:aws:glue:${this.region}:${this.account}:database/*`,
          `arn:aws:glue:${this.region}:${this.account}:table/*`
        ]
      })
    );

    // Add S3 permissions for Athena results
    OptiraAgentFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          's3:GetBucketLocation',
          's3:ListBucket'
        ],
        resources:  [
                `arn:aws:s3:::${supportDataBucketName.valueAsString}`
            ]
      })
    );

    OptiraAgentFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          's3:GetObject',
          's3:PutObject',
          's3:DeleteObject'
        ],
        resources: [
               `arn:aws:s3:::${supportDataBucketName.valueAsString}/*`
            ]
      })
    );

    // Add AWS Support API permissions for Trusted Advisor (Support API requires * resources)
    OptiraAgentFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'support:DescribeTrustedAdvisorChecks',
          'support:DescribeTrustedAdvisorCheckResult',
          'support:DescribeTrustedAdvisorCheckSummaries',
           'support:RefreshTrustedAdvisorCheck'
        ],
        resources: ['*']
      })
    );

    // Add STS permissions for account ID resolution
    OptiraAgentFunction.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'sts:GetCallerIdentity'
        ],
        resources: ['*']
      })
    );
  

    // Create the API Gateway
    const api = new apigateway.RestApi(this, 'OptiraAgentApi', 
      {
      restApiName: 'OptiraAgent API',
      description: 'This is API Gateway service integrated with agentInsightFunction backend',
      endpointConfiguration: {
        types: [apigateway.EndpointType.REGIONAL]
      },
      deployOptions: {
        stageName: 'prod',
        loggingLevel: apigateway.MethodLoggingLevel.INFO,
        dataTraceEnabled: true,
      },
      // Enable CloudWatch logs
      cloudWatchRole: true,
    });

        // Create resources and methods
    const items = api.root.addResource('prompt');
    // POST method
    // Change time out explicitly to 180s since script not allowing to go beyond 29s
    items.addMethod('POST', new apigateway.LambdaIntegration(OptiraAgentFunction, {
      proxy: true
    }));

    // Create a usage plan
    const plan = api.addUsagePlan('StandardUsagePlan', {
      name: 'Standard',
      description: 'Standard usage plan with rate limiting and quota',
      throttle: {
        rateLimit: 100,    // requests per second
        burstLimit: 500    // maximum concurrent requests
      },
      quota: {
        limit: 10000,     // number of requests
        period: apigateway.Period.MONTH
      }
    });

    // Create API keys
    const prodApiKey = api.addApiKey('ProdApiKey', {
      apiKeyName: 'optira-prod-api-key',
      description: 'API Key for production use'
    });

    plan.addApiKey(prodApiKey);

        // Associate the usage plan with the API's deployment stage
    plan.addApiStage({
      stage: api.deploymentStage
    });

    // Output the API URL and API key IDs
    new cdk.CfnOutput(this, 'ApiUrl', {
      value: api.url,
      description: 'API Gateway URL',
    });

    new cdk.CfnOutput(this, 'ProdApiKeyId', {
      value: prodApiKey.keyId,
      description: 'Production API Key ID',
    });

    // Optional WAF for DDoS protection
    if (enableWaf.valueAsString === 'true') {
      const webAcl = new wafv2.CfnWebACL(this, 'OptiraApiWAF', {
        scope: 'REGIONAL',
        defaultAction: { allow: {} },
        rules: [
          {
            name: 'RateLimitRule',
            priority: 1,
            statement: {
              rateBasedStatement: {
                limit: 2000,
                aggregateKeyType: 'IP'
              }
            },
            action: { block: {} },
            visibilityConfig: {
              sampledRequestsEnabled: true,
              cloudWatchMetricsEnabled: true,
              metricName: 'RateLimitRule'
            }
          },
          {
            name: 'AWSManagedRulesCommonRuleSet',
            priority: 2,
            overrideAction: { none: {} },
            statement: {
              managedRuleGroupStatement: {
                vendorName: 'AWS',
                name: 'AWSManagedRulesCommonRuleSet'
              }
            },
            visibilityConfig: {
              sampledRequestsEnabled: true,
              cloudWatchMetricsEnabled: true,
              metricName: 'CommonRuleSetMetric'
            }
          }
        ],
        visibilityConfig: {
          sampledRequestsEnabled: true,
          cloudWatchMetricsEnabled: true,
          metricName: 'OptiraApiWAF'
        }
      });

      new wafv2.CfnWebACLAssociation(this, 'OptiraApiWAFAssociation', {
        resourceArn: `arn:aws:apigateway:${this.region}::/restapis/${api.restApiId}/stages/prod`,
        webAclArn: webAcl.attrArn
      });

      new cdk.CfnOutput(this, 'WAFWebACLArn', {
        value: webAcl.attrArn,
        description: 'WAF Web ACL ARN'
      });
    }
  }
}