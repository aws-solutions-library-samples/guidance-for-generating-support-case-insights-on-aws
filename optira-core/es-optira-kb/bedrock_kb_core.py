#!/usr/bin/env python3
import boto3,sys
import json
import time
import uuid
import logging
from botocore.exceptions import ClientError
import requests
from requests_aws4auth import AWS4Auth

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


PARSING_MODEL_ID="global.anthropic.claude-opus-4-7"
EMBEDDING_MODEL_ID="amazon.titan-embed-text-v2:0"

class OptiraKnowledgeBase:
    """Core functionality for Amazon Bedrock Knowledge Base"""
    
    def __init__(self, region_name, s3_bucket_name, kb_role_arn, s3_prefix="support-cases"):
        self.region_name = region_name
        self.s3_bucket_name = s3_bucket_name
        self.s3_prefix = s3_prefix
        self.kb_role_arn = kb_role_arn
        
        # Initialize AWS clients
        self.bedrock_agent = boto3.client('bedrock-agent', region_name=region_name)
        self.opensearch = boto3.client('opensearchserverless', region_name=region_name)
        self.s3 = boto3.client('s3', region_name=region_name)
        self.sts = boto3.client('sts', region_name=region_name)
        self.secrets_manager = boto3.client('secretsmanager', region_name=region_name)
        
        # Get account ID
        self.account_id = self.sts.get_caller_identity()['Account']
        logger.info(f"AWS Account ID: {self.account_id}")
        logger.info(f"AWS Region: {region_name}")
        logger.info(f"Using KB Role ARN: {kb_role_arn}")
        
        # Verify bucket exists
        try:
            self.s3.head_bucket(Bucket=s3_bucket_name)
            logger.info(f"Using existing S3 bucket: {s3_bucket_name}")
        except ClientError as e:
            logger.error(f"S3 bucket {s3_bucket_name} does not exist or you don't have access to it")
            raise
        
        # Vector dimension for embedding
        self.VECTOR_DIMENSION = 1024



    def create_opensearch_collection(self):
        """Create OpenSearch Serverless collection and vector index"""
        unique_suffix = uuid.uuid4().hex[:8]
        collection_name = f"bedrock-kb-{unique_suffix}"
        index_name = "bedrock-kb-index"
        
        # 1. Create encryption policy
        encryption_policy = {
            "Rules": [
                {
                    "ResourceType": "collection",
                    "Resource": [f"collection/{collection_name}"]
                }
            ],
            "AWSOwnedKey": True
        }
        
        self.opensearch.create_security_policy(
            name=f'kb-enc-{unique_suffix}',
            type='encryption',
            policy=json.dumps(encryption_policy)
        )
        logger.info(f"Created encryption policy for collection: {collection_name}")
        
        # 2. Create network policy - temporarily allow public access for index creation
        network_policy = [{
            "Rules": [{
                "ResourceType": "collection",
                "Resource": [f"collection/{collection_name}"]
            }],
            "AllowFromPublic": True
        }]
        
        self.opensearch.create_security_policy(
            name=f'kb-net-{unique_suffix}',
            type='network',
            policy=json.dumps(network_policy)
        )
        logger.info(f"Created network policy for collection: {collection_name}")
        
        # 3. Create data access policy
        current_user = self.sts.get_caller_identity()
        current_user_arn = current_user['Arn']
        
        access_policy = [{
            "Rules": [
                {
                    "ResourceType": "collection",
                    "Resource": [f"collection/{collection_name}"],
                    "Permission": [
                        "aoss:CreateCollectionItems",
                        "aoss:DeleteCollectionItems",
                        "aoss:UpdateCollectionItems",
                        "aoss:DescribeCollectionItems"
                    ]
                },
                {
                    "ResourceType": "index",
                    "Resource": [
                        f"index/{collection_name}/*",
                        f"index/{collection_name}/{index_name}"
                    ],
                    "Permission": [
                        "aoss:CreateIndex",
                        "aoss:DeleteIndex",
                        "aoss:UpdateIndex",
                        "aoss:DescribeIndex",
                        "aoss:ReadDocument",
                        "aoss:WriteDocument"
                    ]
                }
            ],
            "Principal": [
                f"arn:aws:iam::{self.account_id}:root",
                current_user_arn,
                self.kb_role_arn
            ]
        }]
        
        self.opensearch.create_access_policy(
            name=f"kb-data-{unique_suffix}",
            type="data",
            policy=json.dumps(access_policy)
        )
        logger.info(f"Created data access policy for collection: {collection_name}")
        
        # 4. Wait for policies to propagate
        logger.info("Waiting for access policies to propagate...")
        time.sleep(60)
        
        # 5. Create and wait for collection
        collection_response = self.opensearch.create_collection(
            name=collection_name,
            type='VECTORSEARCH'
        )
        collection_id = collection_response['createCollectionDetail']['id']
        logger.info(f"Created OpenSearch collection: {collection_id}")
        
        logger.info("Waiting for OpenSearch collection to become active...")
        while True:
            status_response = self.opensearch.batch_get_collection(names=[collection_name])
            
            if status_response['collectionDetails']:
                status = status_response['collectionDetails'][0]['status']
                logger.info(f"Collection status: {status}")
                
                if status == 'ACTIVE':
                    collection_arn = status_response['collectionDetails'][0]['arn']
                    break
                elif status in ['FAILED', 'DELETED']:
                    raise Exception(f"Collection creation failed with status: {status}")
            
        
        # 6. Create vector index
        endpoint = f"https://{collection_id}.{self.region_name}.aoss.amazonaws.com"
        logger.info("Waiting for collection and policies to be fully ready...")
        time.sleep(60)
        
        credentials = boto3.Session().get_credentials()
        awsauth = AWS4Auth(
            credentials.access_key,
            credentials.secret_key,
            self.region_name,
            'aoss',
            session_token=credentials.token
        )
        
        index_mapping = {
            "mappings": {
                "dynamic": True,
                "properties": {
                    "text": {"type": "text"},
                    "metadata": {
                        "type": "text",
                        "fields": {
                            "keyword": {"type": "keyword", "ignore_above": 256}
                        }
                    },
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": self.VECTOR_DIMENSION,
                        "method": {
                            "name": "hnsw",
                            "space_type": "l2",
                            "engine": "faiss",
                            "parameters": {
                                "ef_construction": 128,
                                "m": 16
                            }
                        }
                    }
                }
            },
            "settings": {
                "index": {
                    "knn": True,
                    "knn.algo_param.ef_search": 100
                }
            }
        }
        
        url = f"{endpoint}/{index_name}"
        logger.info(f"Creating index at URL: {url}")

        # Create the index, retrying to absorb data-access-policy propagation delays (403s)
        # and transient errors. A 2xx here does NOT mean the index is immediately queryable,
        # since AOSS index creation is asynchronous and eventually consistent.
        created = False
        last_detail = ""
        for attempt in range(1, 11):
            try:
                response = requests.put(
                    url,
                    auth=awsauth,
                    json=index_mapping,
                    headers={"Content-Type": "application/json"},
                    timeout=30
                )
                last_detail = f"{response.status_code}, {response.text}"
                logger.info(f"Index creation attempt {attempt}/10: {last_detail}")

                if response.status_code in [200, 201]:
                    logger.info(f"Successfully created vector index: {index_name}")
                    created = True
                    break
                # Index already exists from a previous run -> treat as success
                if response.status_code == 400 and "resource_already_exists_exception" in response.text:
                    logger.info(f"Vector index already exists: {index_name}")
                    created = True
                    break
                # 403 usually means the data access policy has not propagated yet
                logger.warning(f"Index creation attempt {attempt} not successful, retrying in 15s...")
            except Exception as e:
                last_detail = str(e)
                logger.warning(f"Exception during index creation attempt {attempt}: {last_detail}")
            time.sleep(15)

        if not created:
            raise Exception(
                f"Failed to create vector index '{index_name}' after retries. Last response: {last_detail}"
            )

        # Verify the index is actually queryable before returning. Bedrock's CreateKnowledgeBase
        # validates the index up front and fails with "no such index" if it is not yet visible.
        logger.info("Verifying vector index is available...")
        index_ready = False
        for attempt in range(1, 21):
            try:
                check = requests.get(
                    url,
                    auth=awsauth,
                    headers={"Content-Type": "application/json"},
                    timeout=30
                )
                if check.status_code == 200:
                    logger.info(f"Vector index '{index_name}' is available")
                    index_ready = True
                    break
                logger.info(f"Index not visible yet (status {check.status_code}), attempt {attempt}/20")
            except Exception as e:
                logger.info(f"Index availability check attempt {attempt} error: {str(e)}")
            time.sleep(15)

        if not index_ready:
            raise Exception(
                f"Vector index '{index_name}' was created but did not become available in time"
            )

        # Extra settle time for eventual consistency before Bedrock validates the index
        logger.info("Waiting for index to fully propagate before Knowledge Base creation...")
        time.sleep(60)

        # 7. Update network policy to restrict access after index creation
        self.update_network_policy_for_bedrock(collection_name, unique_suffix)
        
        return {
            'collection_arn': collection_arn,
            'collection_name': collection_name,
            'endpoint': endpoint,
            'index_name': index_name
        }

    def update_network_policy_for_bedrock(self, collection_name, unique_suffix):
        """Update network policy to restrict access to Bedrock service only"""
        try:
            restricted_network_policy = [{
                "Rules": [{
                    "ResourceType": "collection",
                    "Resource": [f"collection/{collection_name}"]
                }],
                "AllowFromPublic": False,
                "SourceServices": ["bedrock.amazonaws.com"]
            }]
            
            self.opensearch.update_security_policy(
                name=f'kb-net-{unique_suffix}',
                type='network',
                policy=json.dumps(restricted_network_policy),
                policyVersion=self.opensearch.get_security_policy(
                    name=f'kb-net-{unique_suffix}',
                    type='network'
                )['securityPolicyDetail']['policyVersion']
            )
            logger.info(f"Updated network policy to restrict access for collection: {collection_name}")
        except Exception as e:
            logger.warning(f"Failed to update network policy (non-critical): {str(e)}")

    def create_knowledge_base(self, role_arn, collection_arn):
        """Create or get existing Bedrock Knowledge Base"""
        kb_name = f"optira-support-case-kb"
        
        # Check if knowledge base already exists
        try:
            kb_list = self.bedrock_agent.list_knowledge_bases()
            for kb in kb_list['knowledgeBaseSummaries']:
                if kb['name'] == kb_name:
                    kb_id = kb['knowledgeBaseId']
                    logger.info(f"Found existing Knowledge Base: {kb_name} with ID: {kb_id}")
                    return kb_id
        except Exception as e:
            logger.warning(f"Error checking existing knowledge bases: {str(e)}")
        
        # Create new knowledge base if not found
        try:
            kb_response = self.bedrock_agent.create_knowledge_base(
                name=kb_name,
                description="Knowledge base for support case files",
                roleArn=role_arn,
                knowledgeBaseConfiguration={
                    'type': 'VECTOR',
                    'vectorKnowledgeBaseConfiguration': {
                        'embeddingModelArn': f'arn:aws:bedrock:{self.region_name}::foundation-model/{EMBEDDING_MODEL_ID}'
                    }
                },
                storageConfiguration={
                    'type': 'OPENSEARCH_SERVERLESS',
                    'opensearchServerlessConfiguration': {
                        'collectionArn': collection_arn,
                        'vectorIndexName': 'bedrock-kb-index',
                        'fieldMapping': {
                            'vectorField': 'embedding',
                            'textField': 'text',
                            'metadataField': 'metadata'
                        }
                    }
                }
            )
            
            kb_id = kb_response['knowledgeBase']['knowledgeBaseId']
            logger.info(f"Created new Knowledge Base with ID: {kb_id}")
        except ClientError as e:
            if 'already exists' in str(e):
                # Fallback: try to find by name again
                kb_list = self.bedrock_agent.list_knowledge_bases()
                for kb in kb_list['knowledgeBaseSummaries']:
                    if kb['name'] == kb_name:
                        kb_id = kb['knowledgeBaseId']
                        logger.info(f"Using existing Knowledge Base: {kb_name} with ID: {kb_id}")
                        return kb_id
                raise Exception(f"Knowledge Base {kb_name} exists but cannot be found")
            else:
                raise
        
        # Wait for knowledge base to become active
        logger.info("Waiting for Knowledge Base to become active...")
        while True:
            status_response = self.bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
            status = status_response['knowledgeBase']['status']
            logger.info(f"Knowledge Base status: {status}")
            
            if status == 'ACTIVE':
                break
            elif status in ['FAILED', 'DELETED']:
                raise Exception(f"Knowledge Base creation failed with status: {status}")
            
        
        return kb_id

    def create_data_source(self, kb_id, s3_bucket_name=None, s3_prefix=None):
        """Create or get existing data source for the knowledge base"""
        if not s3_bucket_name:
            s3_bucket_name = self.s3_bucket_name
        
        if not s3_prefix:
            s3_prefix = self.s3_prefix
        
        # Check if data source already exists
        try:
            data_sources = self.bedrock_agent.list_data_sources(knowledgeBaseId=kb_id)
            for ds in data_sources['dataSourceSummaries']:
                if s3_bucket_name in ds.get('description', '') or 'support' in ds.get('name', '').lower():
                    data_source_id = ds['dataSourceId']
                    logger.info(f"Found existing data source with ID: {data_source_id}")
                    return data_source_id
        except Exception as e:
            logger.warning(f"Error checking existing data sources: {str(e)}")
            
        data_source_response = self.bedrock_agent.create_data_source(
            knowledgeBaseId=kb_id,
            name=f"s3-data-source-{uuid.uuid4().hex[:8]}",
            description="S3 data source for support files",
            dataDeletionPolicy='RETAIN',
            dataSourceConfiguration={
                'type': 'S3',
                's3Configuration': {
                    'bucketArn': f"arn:aws:s3:::{s3_bucket_name}",
                    'inclusionPrefixes': [s3_prefix]
                }
            },
            vectorIngestionConfiguration={
                'parsingConfiguration': {
                    'parsingStrategy': 'BEDROCK_FOUNDATION_MODEL',
                    'bedrockFoundationModelConfiguration': {
                        'modelArn': f'arn:aws:bedrock:{self.region_name}:{self.account_id}:inference-profile/{PARSING_MODEL_ID}',
                        'parsingPrompt': {
                            'parsingPromptText': 'Parse AWS Support case JSON and create separate chunks for: 1) Case metadata: extract caseId, subject, status, severity, service, category, account_id, submittedBy, timeCreated from root level, 2) Each communication as individual chunk: for each item in recentCommunications.communications array extract caseId, timeCreated, submittedBy, and split body content into 500-word segments if longer than 500 words, preserving context with "Communication from [submittedBy] on [timeCreated]:" prefix, 3) Chat messages as separate chunks: for each item in chatTranscript.transcript array extract timestamp, text from data.text, participant from data.from, action type, creating individual chunks for each message with "Chat message from [participant] at [timestamp]:" prefix, 4) Technical details chunk: extract all AWS ARNs, endpoints, error codes, performance metrics, timestamps as consolidated technical summary. Ensure each chunk is under 1000 characters and maintains searchable context.'
                        }
                    }
                },
                'chunkingConfiguration': {
                    'chunkingStrategy': 'SEMANTIC',
                    'semanticChunkingConfiguration': {
                        'maxTokens': 500,
                        'bufferSize': 1,
                        'breakpointPercentileThreshold': 95
                    }
                }
            }
        )
        
        data_source_id = data_source_response['dataSource']['dataSourceId']
        logger.info(f"Created data source with ID: {data_source_id}")
        
        # Wait for data source to become active
        logger.info("Waiting for data source to become active...")
        while True:
            status_response = self.bedrock_agent.get_data_source(
                knowledgeBaseId=kb_id,
                dataSourceId=data_source_id
            )
            status = status_response['dataSource']['status']
            logger.info(f"Data source status: {status}")
            
            if status == 'AVAILABLE':
                break
            elif status in ['FAILED', 'DELETED']:
                raise Exception(f"Data source creation failed with status: {status}")
            
        
        return data_source_id

    def start_ingestion_job(self, kb_id, data_source_id):
        """Start ingestion job for the data source"""
        ingestion_response = self.bedrock_agent.start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id
        )
        
        ingestion_job_id = ingestion_response['ingestionJob']['ingestionJobId']
        logger.info(f"Started ingestion job with ID: {ingestion_job_id}")
        
        # Skip waiting for ingestion job to allow parallel deployment
        logger.info("Ingestion job started successfully - continuing deployment without waiting")
        logger.info("The ingestion job will continue running in the background")
        logger.info(f"You can check ingestion status later with job ID: {ingestion_job_id}")
        
        return ingestion_job_id

    def store_kb_id_in_secrets_manager(self, kb_id, secret_name="optira/knowledge-base-id"):
        """Store knowledge base ID in AWS Secrets Manager"""
        secret_value = {
            "knowledge_base_id": kb_id
        }
        
        try:
            # Try to update existing secret first
            self.secrets_manager.update_secret(
                SecretId=secret_name,
                SecretString=json.dumps(secret_value)
            )
            logger.info(f"Updated existing secret: {secret_name}")
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                # Secret doesn't exist, create it
                self.secrets_manager.create_secret(
                    Name=secret_name,
                    Description="Optira Knowledge Base ID and configuration",
                    SecretString=json.dumps(secret_value)
                )
                logger.info(f"Created new secret: {secret_name}")
            else:
                logger.error(f"Failed to store KB ID in Secrets Manager: {str(e)}")
                raise
        
        logger.info(f"Knowledge Base ID {kb_id} stored in Secrets Manager as {secret_name}")
        return secret_name

    def setup_complete_kb(self, secret_name="optira/knowledge-base-id"):
        """Set up a complete knowledge base with all components"""
        # 1. Create OpenSearch collection
        logger.info("Creating OpenSearch collection...")
        opensearch_config = self.create_opensearch_collection()
        
        # 2. Create knowledge base
        logger.info("Creating knowledge base...")
        kb_id = self.create_knowledge_base(self.kb_role_arn, opensearch_config['collection_arn'])
        
        # 3. Create data source
        logger.info("Creating data source...")
        data_source_id = self.create_data_source(kb_id)
        
        # 4. Start ingestion job
        logger.info("Starting ingestion job...")
        ingestion_job_id = self.start_ingestion_job(kb_id, data_source_id)
        
        # 5. Store KB ID in Secrets Manager
        logger.info("Storing Knowledge Base ID in Secrets Manager...")
        secret_name = self.store_kb_id_in_secrets_manager(kb_id, secret_name)
        
        logger.info(f"Knowledge base setup complete. KB ID: {kb_id}")
        logger.info(f"Knowledge Base ID stored in Secrets Manager: {secret_name}")
        
        return {
            'kb_id': kb_id,
            'data_source_id': data_source_id,
            'role_arn': self.kb_role_arn,
            'collection_arn': opensearch_config['collection_arn'],
            'secret_name': secret_name
        }

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Amazon Bedrock Knowledge Base Manager')
    parser.add_argument('--region', type=str, default='us-west-1', help='AWS region')
    parser.add_argument('--bucket', type=str, required=True, help='Existing S3 bucket name')
    parser.add_argument('--prefix', type=str, default='support-cases', help='S3 prefix')
    parser.add_argument('--role-arn', type=str, required=True, help='IAM role ARN from CDK stack')
    parser.add_argument('--secret-name', type=str, default='optira/knowledge-base-id', help='Secrets Manager secret name for storing KB ID')
    
    args = parser.parse_args()
    
    try:
        kb = OptiraKnowledgeBase(
            region_name=args.region,
            s3_bucket_name=args.bucket,
            kb_role_arn=args.role_arn,
            s3_prefix=args.prefix
        )
        
        result = kb.setup_complete_kb(secret_name=args.secret_name)
        
        print(f"\n=== Knowledge Base Setup Complete ===")
        print(f"Knowledge Base ID: {result['kb_id']}")
        print(f"Data Source ID: {result['data_source_id']}")
        print(f"Secret Name: {result['secret_name']}")
        print(f"Collection ARN: {result['collection_arn']}")
        print(f"Role ARN: {result['role_arn']}")
        print(f"\nThe Knowledge Base ID has been stored in AWS Secrets Manager.")
        print(f"You can retrieve it using: aws secretsmanager get-secret-value --secret-id {result['secret_name']}")
        print(f"\nTo test the Knowledge Base, run:")
        print(f"python3 test_kb.py --region {args.region} --action query --kb-id {result['kb_id']} --query-text 'Your question'")
        print(f"\nTo start ingestion of new files, upload Optira files to s3://{args.bucket}/{args.prefix}/")
        print(f"Then run: python3 test_kb.py --region {args.region} --action ingest --kb-id {result['kb_id']}")
        
    except Exception as e:
        logger.error(f"Failed to create Knowledge Base: {str(e)}")
        sys.exit(1)