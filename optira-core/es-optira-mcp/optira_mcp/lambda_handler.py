"""AWS Lambda entrypoint for the Optira MCP server.

Serves the MCP tools over Streamable HTTP behind API Gateway. AWS DevOps Agent
requires the Streamable HTTP transport and signs every request with SigV4,
which API Gateway (AWS_IAM authorization) validates before the request reaches
this function.

The server runs in *stateless* mode with JSON responses so it maps cleanly onto
Lambda's buffered request/response model (no long-lived sessions or SSE
streams). The ASGI app produced by FastMCP is adapted to the Lambda/API Gateway
event shape by Mangum.

Important: FastMCP's Streamable HTTP session manager can only be run once per
instance, and Mangum runs the ASGI lifespan (which starts that manager) on every
Lambda invocation. Reusing a single module-level app therefore fails on the
second invocation ("run() can only be called once per instance"). Because we run
in stateless mode -- every request is fully self-contained -- we build a fresh
server/app per invocation. Construction is lightweight (registering three tools),
and this keeps each request correct and independent.
"""
from __future__ import annotations

from typing import Any

from mangum import Mangum

from .mcp_server import build_server


def handler(event: dict, context: Any) -> dict:
    # A fresh server + ASGI app per invocation: each gets its own session
    # manager, so lifespan startup never re-runs a spent manager.
    # trust_proxy_host=True: API Gateway (SigV4/IAM) is the authenticated front
    # door, and the inbound Host header is the execute-api domain.
    mcp = build_server(
        stateless_http=True, json_response=True, trust_proxy_host=True
    )
    asgi_handler = Mangum(mcp.streamable_http_app(), lifespan="auto")
    return asgi_handler(event, context)
