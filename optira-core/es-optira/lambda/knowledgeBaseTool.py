"""
AWS tool for querying knowledge base data using Bedrock for query generation.
"""
import json, os
from strands import tool
import boto3
import logging
from botocore.exceptions import ClientError

# Constants
JSON_CONTENT_TYPE = {"Content-Type": "application/json"}
BEDROCK_MODEL_ID = os.environ['BEDROCK_MODEL_ID'] 
MAX_TOKENS = 2000
KB_ID = os.environ['KNOWLEDGEBASE_ID']
MAX_RESULTS=5
session = boto3.Session()
REGION_NAME = session.region_name 

def retrieve_from_kb(query):
    """
    Retrieve information from Knowledge Base
    """
    bedrock_agent_runtime = boto3.client(service_name='bedrock-agent-runtime', region_name=REGION_NAME)
    try:
        response = bedrock_agent_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={
                'text': query
            },
            retrievalConfiguration={
                'vectorSearchConfiguration': {
                    'numberOfResults': MAX_RESULTS
                }
            }
        )

        return response.get('retrievalResults', [])

    except ClientError as e:
        print(f"Error retrieving from Knowledge Base: {str(e)}")
        return []

def query_model(prompt, retrieved_context):
    """
    Query Claude with context from Knowledge Base
    """
    try:
        # Construct prompt with context
        formatted_prompt = f"""
        Context from Knowledge Base:
        {retrieved_context}

        Human Question:
        {prompt}

        Please provide a comprehensive answer based on the context provided above.
        If the context doesn't contain enough information, please mention that.
        """

        # Prepare the request body for model
        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2000,
            "messages": [
                {
                    "role": "user",
                    "content": formatted_prompt
                }
            ]
        }
        bedrock_runtime = boto3.client(service_name='bedrock-runtime', region_name=REGION_NAME)
        # Invoke model
        response = bedrock_runtime.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps(request_body)
        )

        response_body = json.loads(response['body'].read())
        return response_body['content'][0]['text']

    except Exception as e:
        print(f"Error querying Claude: {str(e)}")
        return None

@tool
def knowledge_insight(user_query: str) -> str:
    """
    Process a query using both Knowledge Base and Claude
    """
    try:
        # First, retrieve relevant information from KB
        kb_results = retrieve_from_kb(user_query)
        
        if not kb_results:
            return "Sorry, I couldn't retrieve any relevant information from the Knowledge Base."

        # Format the retrieved context
        context = "\n\n".join([
            f"Source {i+1}:\n{result.get('content', {}).get('text', '')}"
            for i, result in enumerate(kb_results)
        ])

        # Query model with the context
        final_response = query_model(user_query, context)
        
        return final_response

    except Exception as e:
        print(f"Error processing query: {str(e)}")
        return "Sorry, an error occurred while processing your query."