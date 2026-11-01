"""pondNT: MCP servers over loopback HTTP for Copilot in Visual Studio on a D365 F&O dev box."""

__version__ = "0.2.9"

# Shared policy; neither job engine imports the other engine to read it.
JOB_RETENTION_SECONDS = 7 * 24 * 3600
