"""Knowledge base tool: RAG over the Bedrock Knowledge Base.

Faithful copy of the es-optira ``knowledgeBaseTool.py`` logic
(``retrieve_from_kb`` + ``query_model`` + ``knowledge_insight``). The only
change from the Lambda original is that env values (KB id, model, region) are
read from the shared Settings object instead of module-level ``os.environ``
reads, so the same code runs under stdio, HTTP, and Lambda. Behavior and return
values match es-optira exactly: a plain-string answer, retrieving
``numberOfResults`` chunks with no metadata filter.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from .. import aws_clients
from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


def _retrieve(
    query: str, num_results: int, settings: Settings
) -> List[Dict[str, Any]]:
    """Retrieve relevant chunks from the Knowledge Base (es-optira retrieve_from_kb)."""
    client = aws_clients.bedrock_agent_runtime(settings.aws_region)
    try:
        response = client.retrieve(
            knowledgeBaseId=settings.knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": num_results}
            },
        )
        return response.get("retrievalResults", [])
    except ClientError as exc:
        logger.error("Error retrieving from Knowledge Base: %s", exc)
        return []


def _synthesize(
    query: str, retrieved_context: str, settings: Settings
) -> Optional[str]:
    """Query Claude with context from the Knowledge Base (es-optira query_model)."""
    formatted_prompt = f"""
        Context from Knowledge Base:
        {retrieved_context}

        Human Question:
        {query}

        Please provide a comprehensive answer based on the context provided above.
        If the context doesn't contain enough information, please mention that.
        """
    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": formatted_prompt}],
    }
    try:
        client = aws_clients.bedrock_runtime(settings.aws_region)
        response = client.invoke_model(
            modelId=settings.bedrock_model_id, body=json.dumps(request_body)
        )
        response_body = json.loads(response["body"].read())
        return response_body["content"][0]["text"]
    except Exception as exc:  # noqa: BLE001
        logger.error("Error querying model: %s", exc)
        return None


def search_knowledge(
    query: str,
    max_results: Optional[int] = None,
    settings: Optional[Settings] = None,
) -> Optional[str]:
    """Process a query using both the Knowledge Base and Claude.

    Faithful copy of es-optira ``knowledge_insight``: retrieves KB chunks,
    concatenates them as context, and asks the model for a grounded answer.
    Returns a plain-string answer (or a fixed apology string).
    """
    settings = settings or get_settings()
    num_results = max_results or settings.kb_max_results
    try:
        # First, retrieve relevant information from KB
        kb_results = _retrieve(query, num_results, settings)

        if not kb_results:
            return (
                "Sorry, I couldn't retrieve any relevant information from the "
                "Knowledge Base."
            )

        # Format the retrieved context
        context = "\n\n".join(
            f"Source {i + 1}:\n{result.get('content', {}).get('text', '')}"
            for i, result in enumerate(kb_results)
        )

        # Query model with the context
        return _synthesize(query, context, settings)
    except Exception as exc:  # noqa: BLE001
        logger.error("Error processing query: %s", exc)
        return "Sorry, an error occurred while processing your query."
