"""Optira MCP server package.

Exposes the Optira AWS support-case insight capabilities as Model Context
Protocol (MCP) tools so any MCP-compatible host can drive them:

- ``query_support_case_metadata`` -- natural language -> Athena SQL over case metadata
- ``search_support_knowledge``   -- RAG over the Bedrock Knowledge Base
- ``get_support_insights``       -- optional composite Strands orchestrator

This package is standalone and does NOT modify or depend on the existing
Lambda/API Gateway deployment (``es-optira``).
"""

__version__ = "0.1.0"
