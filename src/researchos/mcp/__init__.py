"""Optional MCP (Model Context Protocol) clients used by ResearchOS.

The only client shipped today is :class:`~researchos.mcp.arxiv.ArxivMCPClient`,
which talks to ``arxiv-mcp-server`` over stdio. The package is importable even
when the ``mcp`` extra is not installed; constructing a client raises
:class:`MCPUnavailable` in that case so callers can fall back to the plain HTTP
provider.
"""

from __future__ import annotations

from researchos.mcp.arxiv import (
    ArxivMCPClient,
    MCPUnavailable,
    parse_search_papers_result,
)

__all__ = ["ArxivMCPClient", "MCPUnavailable", "parse_search_papers_result"]
