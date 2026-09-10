"""Optional composite tool: the Strands orchestrator.

Reproduces the behavior of the existing Lambda handler (an ``Agent`` with the
two capabilities registered as tools, driven by the same system prompt). Kept
separate so the granular MCP tools do not require Strands at import time.
"""
from __future__ import annotations

import logging

from strands import Agent, tool

from ..config import get_settings
from .case_metadata import query_case_metadata
from .knowledge import search_knowledge

logger = logging.getLogger(__name__)


@tool
def case_aggregation(query: str) -> dict:
    """Query AWS support case metadata using natural language.

    Translates the request into Athena SQL over ``optira_database.case_metadata``
    and returns the generated SQL plus matching rows.
    """
    return query_case_metadata(query)


@tool
def knowledge_insight(user_query: str) -> str:
    """Answer a question using RAG over the support-case Knowledge Base."""
    return search_knowledge(user_query)


def run_orchestrator(query: str) -> str:
    """Run the full Strands agent loop and return its final text answer."""
    settings = get_settings()
    agent = Agent(
        tools=[case_aggregation, knowledge_insight],
        system_prompt=settings.system_prompt,
    )
    formatted_query = f"query:{query}?"
    return str(agent(formatted_query))
