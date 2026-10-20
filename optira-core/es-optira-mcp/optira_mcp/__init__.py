"""Optira MCP server package.

Exposes Optira's AWS support-case and Trusted Advisor insights as Model Context
Protocol (MCP) tools so any MCP-compatible host can drive them:

- ``get_support_insights``                -- natural-language support-case Q&A;
  runs the Strands orchestrator over Athena (case metadata) and the Bedrock
  Knowledge Base.
- ``get_trusted_advisor_recommendations`` -- actionable Trusted Advisor findings
  and flagged resources, read from the collector's S3 output.

This package is standalone: it does not modify the es-optira REST API or the
other core Optira stacks; it only reads the data they produce.
"""

__version__ = "0.1.0"
