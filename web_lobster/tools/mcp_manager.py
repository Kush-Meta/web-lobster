"""MCP (Model Context Protocol) manager — connects to local MCP servers.

Manages multiple MCP server connections via stdio transport and exposes their
tools in Claude-compatible schema format.
"""

from __future__ import annotations

import contextlib
from typing import Optional

from web_lobster.core.config import MCPServerConfig
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)


class MCPManager:
    """Manages connections to one or more MCP servers."""

    def __init__(self, servers: list[MCPServerConfig]):
        self._configs = servers
        self._sessions: dict[str, object] = {}          # server_name -> ClientSession
        self._tool_to_server: dict[str, str] = {}       # tool_name -> server_name
        self._tools: list[dict] = []                    # Claude-compatible tool schemas
        self._exit_stacks: list[contextlib.AsyncExitStack] = []

    async def start(self) -> None:
        """Connect to all configured MCP servers and collect tool definitions."""
        if not self._configs:
            return

        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            logger.warning("mcp_not_installed", hint="pip install mcp>=1.0.0")
            return

        for server_cfg in self._configs:
            stack = contextlib.AsyncExitStack()
            try:
                params = StdioServerParameters(
                    command=server_cfg.command,
                    args=server_cfg.args,
                    env=server_cfg.env if server_cfg.env else None,
                )

                stdio_transport = await stack.enter_async_context(stdio_client(params))
                read_stream, write_stream = stdio_transport
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()

                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    schema = {
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema if tool.inputSchema else {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        },
                    }
                    self._tools.append(schema)
                    self._tool_to_server[tool.name] = server_cfg.name

                self._sessions[server_cfg.name] = session
                self._exit_stacks.append(stack)
                logger.info(
                    "mcp_server_connected",
                    server=server_cfg.name,
                    tools=len(tools_result.tools),
                )

            except Exception as exc:
                logger.warning(
                    "mcp_server_connection_failed",
                    server=server_cfg.name,
                    error=str(exc),
                )
                # Clean up this stack on failure
                try:
                    await stack.aclose()
                except Exception:
                    pass

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a named MCP tool and return its result as a string."""
        server_name = self._tool_to_server.get(tool_name)
        if server_name is None:
            return f"[MCP error] Unknown tool: {tool_name}"

        session = self._sessions.get(server_name)
        if session is None:
            return f"[MCP error] Server '{server_name}' not connected"

        try:
            result = await session.call_tool(tool_name, arguments)
            # Concatenate all text content blocks
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "\n".join(parts) if parts else "(no text output)"
        except Exception as exc:
            logger.warning("mcp_tool_call_failed", tool=tool_name, error=str(exc))
            return f"[MCP error] {exc}"

    async def stop(self) -> None:
        """Disconnect from all servers."""
        for stack in self._exit_stacks:
            try:
                await stack.aclose()
            except Exception as exc:
                logger.warning("mcp_stop_error", error=str(exc))
        self._exit_stacks.clear()
        self._sessions.clear()
        self._tools.clear()
        self._tool_to_server.clear()

    @property
    def tools(self) -> list[dict]:
        """Claude-compatible tool schemas for all connected MCP servers."""
        return self._tools

    @property
    def has_tools(self) -> bool:
        return bool(self._tools)
