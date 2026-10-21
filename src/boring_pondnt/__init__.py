"""pondNT: a local stdio MCP server for Copilot in Visual Studio."""

__version__ = "0.2.0rc22"

# Shared policy; neither job engine imports the other engine to read it.
JOB_RETENTION_SECONDS = 7 * 24 * 3600
