"""Host-agnostic declarations of the Agent tools and the unified invocation entry point.

One declaration gives the whole contract of a tool shared by both hosts: name, full description (Chinese in the live set), request model
(the schema is derived from it; parameter docs are written on the field descriptions), migration-blocking policy,
long-task flag, domain key, handler and optional summary.
Both adapters call the handler through :func:`invoke_declaration` (unscoped declarations through
:func:`invoke_unscoped_declaration`), so request validation and migration blocking have a single implementation.

There are two kinds of declaration: :class:`ToolDeclaration`, which acts on an existing project, and
:class:`UnscopedToolDeclaration`, which does not (listing and creating projects). Adapters dispatch on the declaration
type.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema

from server.tool_runtime import (
    CallerContext,
    ProjectScope,
    Services,
    ToolOutcome,
    ToolProblem,
    ToolRequest,
    migration_gate,
)


@dataclass(frozen=True, slots=True)
class Blocked:
    """Refused at the entry point when the project migration verdict is failure; the handler is not called."""


@dataclass(frozen=True, slots=True)
class Exempt:
    """Let through as usual when migration failed; ``reason`` states why this entry point need not be blocked."""

    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("An exemption from migration blocking must state a reason")


@dataclass(frozen=True, slots=True)
class ReadCheck:
    """Read-only tools report migration status themselves: the entry point lets them through and the handler returns the migration problem in the result."""


BLOCKED = Blocked()
READ_CHECK = ReadCheck()

type MigrationPolicy = Blocked | Exempt | ReadCheck

type ScopedHandler[RequestT, ResultT] = Callable[
    [ToolRequest[RequestT], ProjectScope, CallerContext, Services], Awaitable[ToolOutcome[ResultT]]
]

type UnscopedHandler[RequestT, ResultT] = Callable[
    [ToolRequest[RequestT], CallerContext, Services], Awaitable[ToolOutcome[ResultT]]
]


class _UntitledJsonSchema(GenerateJsonSchema):
    """Drops the titles pydantic auto-generates from class and field names; parameter semantics are expressed only by the field descriptions."""

    def field_title_should_be_set(self, schema: Any) -> bool:
        del schema
        return False

    def model_schema(self, schema: core_schema.ModelSchema) -> JsonSchemaValue:
        json_schema = super().model_schema(schema)
        json_schema.pop("title", None)
        return json_schema


def request_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Derive the tool's parameter schema from the request model (the top level always has ``type`` and ``properties``).

    The tool's purpose is written only in the declaration's description; the request model's docstring does not go
    into the schema.
    """
    schema = model.model_json_schema(schema_generator=_UntitledJsonSchema)
    schema.pop("description", None)
    schema.setdefault("properties", {})
    return schema


@dataclass(frozen=True, slots=True)
class ToolImage:
    """One image in a result: both hosts hand it to the Agent as an MCP image content block, not as a file path."""

    data: bytes
    mime_type: str

    @property
    def base64_data(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolDeclaration[RequestT: BaseModel, ResultT]:
    """An Agent tool that acts on an existing project.

    The three optional result-encoding hooks all act on the handler's success value and are shared by both hosts:

    - ``summary``: human-readable summary text; when it returns an empty value no summary block is emitted;
    - ``projection``: the structured result, by default ``{domain_key: JSON projection of the value}``;
    - ``is_error``: returns True when the value itself means failure (e.g. the whole batch was blocked); defaults to
      always False;
    - ``images``: images handed to the Agent to look at along with the result, placed after the text blocks; none by
      default.
    """

    name: str
    description: str
    request_model: type[RequestT]
    migration: MigrationPolicy
    domain_key: str
    handler: ScopedHandler[RequestT, ResultT]
    long_task: bool = False
    summary: Callable[[ResultT], str | None] | None = None
    projection: Callable[[ResultT], dict[str, Any]] | None = None
    is_error: Callable[[ResultT], bool] | None = None
    images: Callable[[ResultT], Sequence[ToolImage]] | None = None

    @property
    def input_schema(self) -> dict[str, Any]:
        return request_json_schema(self.request_model)


@dataclass(frozen=True, slots=True, kw_only=True)
class UnscopedToolDeclaration[RequestT: BaseModel, ResultT]:
    """An Agent tool that does not act on an existing project.

    The handler takes no ``ProjectScope``: the embedded host does not inject the session project, and the remote host
    does not append ``project`` to the schema. With no target project there is no migration verdict to check, so there
    is no migration-blocking policy.
    """

    name: str
    description: str
    request_model: type[RequestT]
    domain_key: str
    handler: UnscopedHandler[RequestT, ResultT]
    summary: Callable[[ResultT], str] | None = None

    @property
    def input_schema(self) -> dict[str, Any]:
        return request_json_schema(self.request_model)


type AgentToolDeclaration = ToolDeclaration[Any, Any] | UnscopedToolDeclaration[Any, Any]

MIGRATION_REFUSAL_NOTE = " Refuses to run when the project data upgrade failed, returning a project_migration_failed problem."

#: Shared by tools that call text models: the issue code and way out when output is truncated.
TEXT_OUTPUT_TRUNCATED_NOTE = (
    " When the output is truncated by the text model's maximum output length, returns a text_output_truncated "
    "problem with params.model naming the model: if params.custom_model is true, ask the user to register a maximum "
    "output length for it in settings; otherwise ask the user to switch to another text model. Retrying unchanged "
    "will truncate again."
)


def tool_description(declaration: AgentToolDeclaration) -> str:
    """Tool description shared by both hosts: declarations whose migration policy is ``BLOCKED`` get the migration refusal note appended."""
    if isinstance(declaration, ToolDeclaration) and isinstance(declaration.migration, Blocked):
        return declaration.description + MIGRATION_REFUSAL_NOTE
    return declaration.description


def invalid_request_problem(exc: ValidationError) -> ToolProblem:
    errors = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]
    detail = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}" for error in errors
    )
    return ToolProblem("invalid_request", detail, params={"errors": errors})


def _validated_request[RequestT: BaseModel](
    request_model: type[RequestT], arguments: Mapping[str, Any]
) -> RequestT | ToolProblem:
    try:
        return request_model.model_validate(dict(arguments))
    except ValidationError as exc:
        return invalid_request_problem(exc)


async def invoke_declaration(
    declaration: ToolDeclaration[Any, Any],
    arguments: Mapping[str, Any],
    scope: ProjectScope,
    caller: CallerContext,
    services: Services,
) -> ToolOutcome[Any]:
    """Validate the request per the declaration, apply the migration-blocking policy and call the handler; both hosts share this one entry point."""
    request = _validated_request(declaration.request_model, arguments)
    if isinstance(request, ToolProblem):
        return ToolOutcome(problem=request)
    if isinstance(declaration.migration, Blocked) and (problem := await migration_gate(scope, services)) is not None:
        return ToolOutcome(problem=problem)
    return await declaration.handler(ToolRequest(request), scope, caller, services)


async def invoke_unscoped_declaration(
    declaration: UnscopedToolDeclaration[Any, Any],
    arguments: Mapping[str, Any],
    caller: CallerContext,
    services: Services,
) -> ToolOutcome[Any]:
    """Validate the request per the unscoped declaration and call the handler; both hosts share this one entry point."""
    request = _validated_request(declaration.request_model, arguments)
    if isinstance(request, ToolProblem):
        return ToolOutcome(problem=request)
    return await declaration.handler(ToolRequest(request), caller, services)


__all__ = [
    "BLOCKED",
    "MIGRATION_REFUSAL_NOTE",
    "READ_CHECK",
    "TEXT_OUTPUT_TRUNCATED_NOTE",
    "AgentToolDeclaration",
    "Blocked",
    "Exempt",
    "MigrationPolicy",
    "ReadCheck",
    "ScopedHandler",
    "ToolDeclaration",
    "ToolImage",
    "UnscopedHandler",
    "UnscopedToolDeclaration",
    "invalid_request_problem",
    "invoke_declaration",
    "invoke_unscoped_declaration",
    "request_json_schema",
    "tool_description",
]
