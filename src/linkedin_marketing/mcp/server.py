"""``linkedin-marketing-mcp``: the stdio MCP server over the library.

Only enabled modules (``LINKEDIN_MODULES``) get tools. stdout carries the MCP
protocol, so all logging goes to stderr. Works with the ``mcp`` SDK 1.x
(``FastMCP``) and 2.x (``MCPServer``).
"""

from __future__ import annotations

import logging
import sys
from functools import cache

from linkedin_marketing.client import LinkedInClient
from linkedin_marketing.config import Settings
from linkedin_marketing.mcp.tools import REGISTRARS, ClientFactory

SERVER_NAME = "linkedin-marketing"
INSTRUCTIONS = (
    "Unofficial LinkedIn Marketing API tools. Read tools are safe. Write tools are dry "
    "runs unless confirm=True: show the user the dry-run request and only confirm "
    "after they approve it."
)


def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP as MCPServer
    return MCPServer


def build_server(settings: Settings, get_client: ClientFactory):
    """An MCP server with tools for the modules enabled in ``settings``."""
    server = _server_class()(SERVER_NAME, instructions=INSTRUCTIONS)
    for module in settings.modules:
        REGISTRARS[module](server, get_client)
    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()

    @cache
    def get_client() -> LinkedInClient:
        return LinkedInClient(settings)

    build_server(settings, get_client).run()


if __name__ == "__main__":
    main()
