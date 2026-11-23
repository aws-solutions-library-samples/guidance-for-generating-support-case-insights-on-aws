# AWS DevOps Agent examples

These examples show how AWS DevOps Agent consumes the [Optira MCP server](../..) once it is registered as a capability provider. They are reference examples of building on the server's `get_support_insights` and `get_trusted_advisor_recommendations` tools — not standalone DevOps Agent assets.

> ⚠️ **Non-production disclaimer:** These are sample code, not intended for production use without additional review and testing. Validate in a non-production environment first.

## Prerequisite

Deploy and register the [Optira MCP server](../..) as a capability provider in your Agent Space first. Both examples call its tools; neither works until the server is connected.

## What's here

- **[`skills/support-and-ta`](skills/support-and-ta/)** — a skill that turns the two MCP tools into a combined reliability report: the top 5 AWS services by support case volume per account, the critical (error/red) Trusted Advisor findings per account, and the overlap between them. Use it in Chat or on demand.
- **[`custom-agents/support-and-ta-report`](custom-agents/support-and-ta-report/)** — a custom agent that runs the skill on a schedule, maintains a single "Daily Support & Trusted Advisor Report" artifact, and emits a tiered Improvements list. Use it for a recurring morning report.

Each example's README covers how to add it to your Agent Space.
