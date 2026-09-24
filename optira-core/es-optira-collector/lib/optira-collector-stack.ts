import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as glue from 'aws-cdk-lib/aws-glue';
import { Construct } from 'constructs';
import { Duration, Stack, StackProps } from "aws-cdk-lib";
import * as path from "path";

export class OptiraCollectorStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

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

    const athenaDatabaseName = new cdk.CfnParameter(this, 'AthenaDatabaseName', {
      type: 'String',
      description: 'Name of the Athena database'
    });

    // Trusted Advisor statuses to collect. TA has no severity concept; its
    // statuses are ok | warning | error, where "error" is the red/critical
    // finding. Use "error" to collect only critical recommendations, or
    // "warning,error" (default) for all actionable ones.
    const taStatuses = new cdk.CfnParameter(this, 'TaStatuses', {
      type: 'String',
      default: 'warning,error',
      description: 'Comma-separated Trusted Advisor statuses to collect (ok, warning, error). Use "error" for critical only.'
    });

    const packagingDirectory = path.join(__dirname, "../packaging");

    const zipDependencies = path.join(packagingDirectory, "dependencies.zip");
    const zipApp = path.join(packagingDirectory, "app.zip");

    // Create a lambda layer with dependencies to keep the code readable in the Lambda console
    const dependenciesLayer = new lambda.LayerVersion(this, "OptiraCollectorDependenciesLayer", {
      code: lambda.Code.fromAsset(zipDependencies),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      description: "Dependencies needed for OptiraCollector lambda",
    });

    // Define the Lambda function
    const OptiraCollectorFunction = new lambda.Function(this, "OptiraCollectorLambda", {
      runtime: lambda.Runtime.PYTHON_3_12,
      functionName: "OptiraCollectorFunction",
      description: "A function that collect case and TA",
      handler: "lambda_function.lambda_handler",
      code: lambda.Code.fromAsset(zipApp),

      timeout: Duration.seconds(300),
      memorySize: 10240,
      layers: [dependenciesLayer],
      architecture: lambda.Architecture.ARM_64,
    });

    // Create condition for new bucket creation
    const shouldCreateNewBucket = new cdk.CfnCondition(this, 'ShouldCreateNewBucket', {
      expression: cdk.Fn.conditionEquals(createNewBucket, 'true')
    });

    // Create new S3 bucket conditionally
    const newSupportDataBucket = new s3.Bucket(this, 'SupportDataBucketResource', {
      bucketName: supportDataBucketName.valueAsString,
      versioned: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN
    });
    (newSupportDataBucket.node.defaultChild as cdk.CfnResource).cfnOptions.condition = shouldCreateNewBucket;

    // Reference existing bucket
    const existingSupportDataBucket = s3.Bucket.fromBucketName(this, 'ExistingSupportDataBucket', supportDataBucketName.valueAsString);



    OptiraCollectorFunction.addToRolePolicy(
      new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: [
                'support:DescribeCases',
                'support:DescribeCommunication',
                'support:DescribeTrustedAdvisorChecks',
                'support:DescribeTrustedAdvisorCheckResult',
                'support:CreateCase',
                'support:ResolveCase',
                'support:ReopenCase',
                'support:AddCommunicationToCase',
                'support:DescribeTrustedAdvisorCheckRefreshStatuses',
                // Modern Trusted Advisor API (returns per-resource ARNs)
                'trustedadvisor:ListRecommendations',
                'trustedadvisor:ListRecommendationResources',
                'trustedadvisor:GetRecommendation',
                'organizations:ListAccounts',
                'sts:AssumeRole'
              ],
              resources: ['*']
            })
          )
    OptiraCollectorFunction.addEnvironment("S3_BUCKET_NAME", supportDataBucketName.valueAsString)
    OptiraCollectorFunction.addEnvironment("S3_PREFIX", "support-cases")
    OptiraCollectorFunction.addEnvironment("TA_STATUSES", taStatuses.valueAsString)

    OptiraCollectorFunction.addToRolePolicy(
    new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        's3:GetObject',
        's3:PutObject',
        's3:PutObjectAcl'
      ],
      resources: [`arn:aws:s3:::${supportDataBucketName.valueAsString}/*`]
    }))

    // Create scheduler role - depends on bucket validation
    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com'),
      inlinePolicies: {
        SchedulerLambdaInvocationPolicy: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              effect: iam.Effect.ALLOW,
              actions: ['lambda:InvokeFunction'],
              resources: [OptiraCollectorFunction.functionArn]
            })
          ]
        })
      }
    });


    // EventBridge rule for historical support data (triggered on stack creation)
    const historicalDataRule = new events.Rule(this, 'EventBridgeRuleForHistoricalSupportData', {
      description: 'Triggers a one time historical sync of support cases when stack creation is complete',
      eventPattern: {
        source: ['aws.cloudformation'],
        detailType: ['CloudFormation Stack Status Change'],
        resources: [this.stackId],
        detail: {
          'stack-id': [this.stackId],
          'status-details': {
            status: ['CREATE_COMPLETE']
          }
        }
      }
    });

    historicalDataRule.addTarget(new targets.LambdaFunction(OptiraCollectorFunction, {
      event: events.RuleTargetInput.fromObject({
        past_no_of_days: 180,
        case: true,
        ta: true
      })
    }));

    // EventBridge Scheduler for daily runs
    new scheduler.CfnSchedule(this, 'OptiraCollectorDailySchedule', {
      scheduleExpression: 'cron(0 6 ? * * *)',
      flexibleTimeWindow: {
        mode: 'OFF'
      },
      target: {
        arn: OptiraCollectorFunction.functionArn,
        input: JSON.stringify({
          past_no_of_days: 1,
          bucket_name: supportDataBucketName.valueAsString,
          case: true,
          ta: true
        }),
        roleArn: schedulerRole.roleArn
      }
    });

    // EventBridge rule for Support Case events
    const supportCaseEventRule = new events.Rule(this, 'SupportCaseEventRule', {
      description: 'Rule to capture AWS Support API events',
      eventPattern: {
        source: ['aws.support'],
        detailType: ['Support Case Update'],
        detail: {
          'event-name': ['CreateCase', 'AddCommunicationToCase', 'ResolveCase', 'ReopenCase']
        }
      }
    });

    supportCaseEventRule.addTarget(new targets.LambdaFunction(OptiraCollectorFunction));

    // Create Glue database
    const optiraDatabase = new glue.CfnDatabase(this, 'OptiraDatabase', {
      catalogId: this.account,
      databaseInput: {
        name: athenaDatabaseName.valueAsString,
        description: 'Database for Optira case metadata'
      }
    });

    // Create Glue table for case metadata
    const caseMetadataTable = new glue.CfnTable(this, 'CaseMetadataTable', {
      databaseName: athenaDatabaseName.valueAsString,
      catalogId: this.account,
      tableInput: {
        name: 'case_metadata',
        tableType: 'EXTERNAL_TABLE',
        parameters: { classification: 'csv' },
        storageDescriptor: {
          location: `s3://${supportDataBucketName.valueAsString}/metadata/`,
          inputFormat: 'org.apache.hadoop.mapred.TextInputFormat',
          outputFormat: 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
          serdeInfo: {
            serializationLibrary: 'org.apache.hadoop.hive.serde2.OpenCSVSerde',
            parameters: {
              separatorChar: ',',
              quoteChar: '"',
              'skip.header.line.count': '1'
            }
          },
          columns: [
            { name: 'account_id', type: 'string' },
            { name: 'caseid', type: 'string' },
            { name: 'timecreated', type: 'string' },
            { name: 'severitycode', type: 'string' },
            { name: 'status', type: 'string' },
            { name: 'subject', type: 'string' },
            { name: 'categorycode', type: 'string' },
            { name: 'servicecode', type: 'string' }
          ]
        }
      }
    });

    caseMetadataTable.addDependency(optiraDatabase);
  }
}
