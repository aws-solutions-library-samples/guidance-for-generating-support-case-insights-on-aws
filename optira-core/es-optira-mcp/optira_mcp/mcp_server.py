"""FastMCP server construction shared by every runtime.

``build_server`` registers a SINGLE public MCP tool -- ``get_support_insights``
-- on a FastMCP instance. It is reused by:
- the CLI (``server.py``) for stdio / local streamable-HTTP, and
- the AWS Lambda handler (``lambda_handler.py``) for the API Gateway (SigV4)
  deployment used by AWS DevOps Agent.

Only one tool is exposed on purpose: it delegates to the Strands orchestrator,
which internally decides whether to use case metadata (Athena) or the Knowledge
Base for each question -- mirroring the deployed Optira agent Lambda. This
prevents callers from picking the wrong low-level tool (e.g. trying to count
cases via the Knowledge Base, which cannot enumerate).
"""
from __future__ import annotations

from typing import Any, Dict

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .tools.trusted_advisor import (
    get_trusted_advisor_recommendations as _get_trusted_advisor_recommendations,
)

SERVER_NAME = "optira-support-insights"


def build_server(
    *,
    stateless_http: bool = False,
    json_response: bool = False,
    trust_proxy_host: bool = False,
) -> FastMCP:
    """Create a FastMCP server exposing the single Optira insights tool.

    Args:
        stateless_http: When True, each request is fully self-contained (no
            server-side session state). Required for Lambda, where any request
            can land on any execution environment.
        json_response: When True, streamable-HTTP responses are returned as a
            single ``application/json`` body instead of an SSE stream. This is
            what lets the server run behind API Gateway + Lambda (buffered
            request/response) via an ASGI adapter.
        trust_proxy_host: When True, disables FastMCP's DNS-rebinding (Host
            header) protection. Use this only when the server sits behind a
            trusted front door that authenticates callers -- e.g. API Gateway
            with SigV4/IAM -- where the Host header is the gateway domain and
            the browser-oriented rebinding threat does not apply.
    """
    transport_security = None
    if trust_proxy_host:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )

    mcp = FastMCP(
        SERVER_NAME,
        stateless_http=stateless_http,
        json_response=json_response,
        transport_security=transport_security,
    )

    @mcp.tool()
    def get_support_insights(query: str) -> Dict[str, Any]:
        """Answer any question about AWS support cases.

        This is the single entry point for support-case insights. It runs the
        Optira orchestrator (a Strands agent), which decides internally how to
        answer -- so you do not need to choose a data source:

        - Exact counts, totals, and complete lists (e.g. "how many cases", "list
          all case ids and accounts") are answered from support-case metadata
          via Amazon Athena, so results are complete and precise.
        - Narrative questions (root cause, resolution, communication history,
          engineer responses for a specific case) are answered from the Amazon
          Bedrock Knowledge Base.
        - Mixed questions are handled by combining both.

        Just pass the user's natural-language question; the tool routes and
        combines the right sources and returns a synthesized answer.

        Args:
            query: The natural-language support question, e.g. "how many open
                cases per account?" or "root cause and resolution for case
                176125013600585".

        Returns:
            dict with a single ``answer`` string (the synthesized response).
        """
        # Imported lazily so importing this module never requires Strands until
        # the tool is actually invoked.
        from .tools.orchestrator import run_orchestrator

        return {"answer": run_orchestrator(query)}

    @mcp.tool()
    def get_trusted_advisor_recommendations(
        account_id: str = "", max_results: int = 50
    ) -> Dict[str, Any]:
        """List AWS Trusted Advisor recommendations to remediate.

        Returns the actionable (warning/error) Trusted Advisor check results
        collected across the organization's accounts, including each check's
        status, description, and the specific flagged resources. Use this to
        drive remediation -- the ``flagged_resources`` array identifies the
        exact resources (with region and identifiers) that need action.

        Args:
            account_id: Optional 12-digit AWS account id to scope results to a
                single account. Omit to return recommendations for all accounts.
            max_results: Maximum number of recommendations (checks) to return
                (default 50).

        Returns:
            dict with ``count``, ``total_available``, ``truncated`` and a
            ``recommendations`` list. Each recommendation includes
            ``account_id``, ``check_id``, ``status``, ``description``,
            ``flagged_resources_count`` and ``flagged_resources``.
        """
        return _get_trusted_advisor_recommendations(
            account_id=account_id or None, max_results=max_results
        )

    return mcp
