"""External Agent (remote MCP) adapter.

A required ``project`` is prepended to the schema, so every call locates the project explicitly; any location failure uniformly returns ``invalid_project``.
Unscoped declarations (listing and creating projects) get no ``project`` and do not locate a project.
Long-task declarations get a uniform batch-handle and polling note appended to the end of the description. Results are written to ``structuredContent``; content (text blocks and image blocks)
is the same as in the embedded host.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from dataclasses import replace
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp.tools import Tool as FastMCPTool
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from lib.db.base import DEFAULT_USER_ID
from lib.project.project_manager import ProjectManager
from lib.project.project_path_text import project_path_spellings, relativize_project_paths
from server.agent_toolset.declaration import (
    AgentToolDeclaration,
    ToolDeclaration,
    UnscopedToolDeclaration,
    invoke_declaration,
    invoke_unscoped_declaration,
    tool_description,
)
from server.agent_toolset.envelope import encode_outcome
from server.tool_runtime import CallerContext, ProjectScope, Services, ToolOutcome, ToolProblem

PROJECT_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": "Project name of the target project (the name returned by list_projects); must be specified explicitly on every call",
}

LONG_TASK_NOTE = (
    "\n\nExternal calls do not wait for generation to finish: submitting immediately returns the batch handle generation_batch; "
    "poll with get_generation_batch at its poll_after_seconds interval until done=true, then read the terminal result."
)


def authenticated_caller() -> CallerContext:
    token = get_access_token()
    if token is None:
        raise RuntimeError("authenticated MCP request has no access token")
    # API-key subjects identify credentials; the supported single-operator model persists queue ownership as default.
    return CallerContext(user_id=DEFAULT_USER_ID, source="mcp")


def resolve_project_scope(project: object, projects: ProjectManager) -> ProjectScope:
    """Normalize and locate ``project``; raise ``ValueError`` / ``FileNotFoundError`` when it is invalid, out of bounds, nonexistent or missing project.json."""
    if not isinstance(project, str):
        raise ValueError("project must be a project name string")
    project_name = projects.normalize_project_name(project)
    projects.get_project_path(project_name)
    if not projects.project_exists(project_name):
        raise FileNotFoundError(f"Project '{project_name}' is missing project.json")
    return ProjectScope(project_name=project_name, data_root=projects.data_root)


def _relativize_payload(value: Any, spellings: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return relativize_project_paths(value, spellings)
    if isinstance(value, dict):
        return {key: _relativize_payload(item, spellings) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_relativize_payload(item, spellings) for item in value)
    return value


def _project_relative_outcome(outcome: ToolOutcome[Any], spellings: tuple[str, ...]) -> ToolOutcome[Any]:
    """Remote error messages do not leak the server's filesystem layout: a problem's detail / action / params use only in-project relative paths."""
    problem = outcome.problem
    if problem is None:
        return outcome
    return ToolOutcome(
        problem=replace(
            problem,
            detail=relativize_project_paths(problem.detail, spellings),
            action=relativize_project_paths(problem.action, spellings) if problem.action is not None else None,
            params=_relativize_payload(problem.params, spellings),
        )
    )


def remote_input_schema(declaration: AgentToolDeclaration) -> dict[str, Any]:
    schema = deepcopy(declaration.input_schema)
    if isinstance(declaration, UnscopedToolDeclaration):
        return schema
    schema["properties"] = {"project": PROJECT_PROPERTY, **schema["properties"]}
    schema["required"] = ["project", *schema.get("required", [])]
    return schema


def remote_description(declaration: AgentToolDeclaration) -> str:
    description = tool_description(declaration)
    if isinstance(declaration, ToolDeclaration) and declaration.long_task:
        return description + LONG_TASK_NOTE
    return description


def remote_result(declaration: AgentToolDeclaration, outcome: ToolOutcome[Any]) -> CallToolResult:
    envelope = encode_outcome(declaration, outcome)
    return CallToolResult(
        content=[
            *(TextContent(type="text", text=text) for text in envelope.texts),
            *(
                ImageContent(type="image", data=image.base64_data, mimeType=image.mime_type)
                for image in envelope.images
            ),
        ],
        structuredContent=envelope.structured,
        isError=envelope.is_error,
    )


class _DeclaredRemoteTool(FastMCPTool):
    """Bypass FastMCP's signature inference: arguments are validated by the declared request model, and ``project`` is located by this adapter."""

    invoke: Callable[[dict[str, Any]], Awaitable[CallToolResult]] = Field(exclude=True)

    async def run(self, arguments: dict[str, Any], context: Any = None, convert_result: bool = False) -> Any:
        del context, convert_result
        return await self.invoke(arguments)


def _locate_project(project: object, projects: ProjectManager) -> tuple[ProjectScope, tuple[str, ...]]:
    scope = resolve_project_scope(project, projects)
    return scope, project_path_spellings(projects.get_project_path(scope.project_name))


def remote_tool(
    declaration: AgentToolDeclaration,
    *,
    projects: ProjectManager,
    services: Services,
    caller: Callable[[], CallerContext] = authenticated_caller,
) -> FastMCPTool:
    async def invoke(arguments: dict[str, Any]) -> CallToolResult:
        if isinstance(declaration, UnscopedToolDeclaration):
            return remote_result(
                declaration, await invoke_unscoped_declaration(declaration, arguments, caller(), services)
            )
        request_arguments = dict(arguments)
        project = request_arguments.pop("project", None)
        try:
            scope, spellings = await asyncio.to_thread(_locate_project, project, projects)
        except (FileNotFoundError, ValueError) as exc:
            return remote_result(declaration, ToolOutcome(problem=ToolProblem("invalid_project", str(exc))))
        outcome = await invoke_declaration(declaration, request_arguments, scope, caller(), services)
        return remote_result(declaration, _project_relative_outcome(outcome, spellings))

    async def unused() -> CallToolResult:
        raise RuntimeError("declared remote tools override run")

    metadata = FastMCPTool.from_function(unused, structured_output=False)
    return _DeclaredRemoteTool(
        fn=unused,
        name=declaration.name,
        title=None,
        description=remote_description(declaration),
        parameters=remote_input_schema(declaration),
        fn_metadata=metadata.fn_metadata,
        is_async=True,
        context_kwarg=None,
        annotations=None,
        invoke=invoke,
    )


def remote_tools(
    declarations: Iterable[AgentToolDeclaration],
    *,
    projects: ProjectManager,
    services: Services,
) -> list[FastMCPTool]:
    return [remote_tool(declaration, projects=projects, services=services) for declaration in declarations]


__all__ = [
    "LONG_TASK_NOTE",
    "PROJECT_PROPERTY",
    "authenticated_caller",
    "remote_description",
    "remote_input_schema",
    "remote_result",
    "remote_tool",
    "remote_tools",
    "resolve_project_scope",
]
