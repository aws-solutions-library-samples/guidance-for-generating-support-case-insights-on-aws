import json, os
import boto3
from typing import Optional

# Constants
MAX_TOKENS = 1000
BEDROCK_MODEL_ID = os.environ['BEDROCK_MODEL_ID']

def invoke_bedrock_api(prompt: str) -> Optional[str]:
    """Invoke Bedrock API to generate SQL query."""
    bedrock_runtime = boto3.client('bedrock-runtime')
    system_prompt = "You are a SQL expert with extensive experience writing queries for AWS Athena."
    combined_prompt = f"{system_prompt}\n\n{prompt}"

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "messages": [{"role": "user", "content": combined_prompt}],
        "max_tokens": MAX_TOKENS
    })

    try:
        response = bedrock_runtime.invoke_model(body=body, modelId=BEDROCK_MODEL_ID)
        response_body = json.loads(response.get('body').read())
        content = response_body.get('content', '')
        
        if isinstance(content, list):
            parts = [item.get('text', str(item)) if isinstance(item, dict) else str(item) for item in content]
            content = ' '.join(parts)
            
        return content.strip()
    except Exception as e:
        print("Error calling Bedrock API:", e)
        return None