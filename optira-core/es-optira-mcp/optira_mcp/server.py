"""Optira MCP server CLI entrypoint.

Runs the Optira MCP tools over either stdio (local hosts) or Streamable HTTP
(local/private hosting). Tool registration lives in ``mcp_server.build_server``
so the CLI and the AWS Lambda handler share exactly the same tools.

For the AWS DevOps Agent integration, the tools are served from Lambda behind
API Gateway (SigV4) -- see ``lambda_handler.py`` and ``infra/``. This CLI is for
local development and stdio-based MCP hosts.

Usage:
    optira-mcp --transport stdio
    optira-mcp --transport http --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import argparse
import logging
import os

from .config import get_settings
from .mcp_server import build_server

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("optira_mcp")


def main() -> None:
    parser = argparse.ArgumentParser(description="Optira support-insights MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="Transport to serve (default: stdio, or MCP_TRANSPORT env).",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST", "127.0.0.1"),
        help="Bind host for HTTP transport (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MCP_PORT", "8000")),
        help="Bind port for HTTP transport (default: 8000).",
    )
    args = parser.parse_args()

    # Resolve config once up front so misconfiguration fails fast and loudly.
    settings = get_settings()
    logger.info(
        "Starting Optira MCP server (transport=%s, region=%s, model=%s, kb=%s)",
        args.transport,
        settings.aws_region,
        settings.bedrock_model_id,
        "set" if settings.knowledge_base_id else "MISSING",
    )

    mcp = build_server()

    if args.transport == "http":
        # SECURITY: this CLI HTTP mode performs no authentication. Use it only
        # for local development or on a trusted private network. The production
        # DevOps Agent deployment enforces SigV4/IAM at API Gateway instead
        # (see lambda_handler.py + infra/).
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
