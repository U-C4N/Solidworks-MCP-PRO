"""Read-only launcher/config diagnostics; does not start SOLIDWORKS or an MCP server."""
from solidworks_mcp_pro.connection_doctor import main


if __name__ == "__main__":
    raise SystemExit(main())
