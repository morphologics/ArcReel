"""Adapter for the ArcReel Agent (embedded Claude Agent SDK).

The project is determined by the session, so the schema has no ``project``; unscoped declarations do not receive the
session project. The structured result is written into content as a JSON text block, placed after the summary, so it
does not depend on whether ``structuredContent`` is forwarded to the model; image blocks (if any) come last.

The embedded server turns off MCP-layer inputSchema pre-validation: arguments of declared tools are always validated by
the request model, so bad arguments get an ``invalid_request`` problem, the same as on remote hosts, rather than MCP's
plain-text validation error.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from claude_agent_sdk import McpSdkServerConfig
from mcp import types
from mcp.server import Server

from server.agent_toolset.declaration import (
    AgentToolDeclaration,
    UnscopedToolDeclaration,
    invoke_declaration,
    invoke_unscoped_declaration,
    tool_description,
)
from server.agent_toolset.envelope import encode_outcome
from server.tool_runtime import CallerContext, ProjectScope, Services, ToolOutcome


def embedded_result(declaration: AgentToolDeclaration, outcome: ToolOutcome[Any]) -> types.CallToolResult:
    envelope = encode_outcome(declaration, outcome)
    return types.CallToolResult(
        content=[
            *(types.TextContent(type="text", text=text) for text in envelope.texts),
            *(
                types.ImageContent(type="image", data=image.base64_data, mimeType=image.mime_type)
                for image in envelope.images
            ),
        ],
        isError=envelope.is_error,
    )


def embedded_server(
    declarations: Iterable[AgentToolDeclaration],
    *,
    name: str,
    version: str,
    scope: ProjectScope,
    caller: CallerContext,
    services: Services,
) -> McpSdkServerConfig:
    """In-process MCP server that exposes the declared tools against the session project."""
    declared = {declaration.name: declaration for declaration in declarations}
    listed = [
        types.Tool(
            name=declaration.name,
            description=tool_description(declaration),
            inputSchema=declaration.input_schema,
        )
        for declaration in declared.values()
    ]
    server = Server(name, version=version)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return listed

    @server.call_tool(validate_input=False)
    async def call_tool(tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        declaration = declared.get(tool_name)
        if declaration is None:
            raise ValueError(f"Tool '{tool_name}' not found")
        if isinstance(declaration, UnscopedToolDeclaration):
            outcome = await invoke_unscoped_declaration(declaration, arguments, caller, services)
        else:
            outcome = await invoke_declaration(declaration, arguments, scope, caller, services)
        return embedded_result(declaration, outcome)

    return McpSdkServerConfig(type="sdk", name=name, instance=server)


__all__ = ["embedded_result", "embedded_server"]
