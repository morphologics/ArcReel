"""Orientation tool declarations: workflow plan, video capabilities and prompt preview, to read before deciding the next step."""

from __future__ import annotations

from lib.workflow.workflow_plan import WorkflowPlanRequest
from server.agent_toolset.declaration import READ_CHECK, Exempt, ToolDeclaration
from server.tool_runtime import (
    NoArguments,
    PromptPreviewRequest,
    get_prompt_preview,
    get_video_capabilities,
    get_workflow_plan,
)

GET_WORKFLOW_PLAN = ToolDeclaration(
    name="get_workflow_plan",
    description=(
        "Read the project's authoritative workflow plan: current content state, admission status.operations for each AI "
        "operation, ordered steps, blocking reasons, data problems, "
        "active task observations, video admission and the suggested next action next_action. Read it before acting on any step: "
        "if the operation the user named is admitted in "
        "operations, execute it, and when it is refused, relay the reason; when the user says continue, act on next_action; "
        "when next_alternatives is non-empty, let the user choose among the parallel options. confirmed_request_durations "
        "applies only to this plan and is not written to the project. Read-only, no side effects. "
        "When the project data upgrade has failed, the plan contains only the single project_migration_failed problem, and the "
        "next action points to the repair."
    ),
    request_model=WorkflowPlanRequest,
    migration=READ_CHECK,
    domain_key="workflow_plan",
    handler=get_workflow_plan,
)

GET_VIDEO_CAPABILITIES = ToolDeclaration(
    name="get_video_capabilities",
    description=(
        "Query the capabilities of the project's current video model (at model granularity: provider, model, supported durations, "
        "etc.) and the project preferences, and give the duration constraints duration_constraints, with axes fixed by the "
        "project's generation mode; "
        "the same basis applies to the whole project, no episode needs to be specified. Reference-to-video projects additionally "
        "include "
        "reference_unit_durations (tiers with and without images, the endpoint_fixed flag of each bucket and its cause; a problem "
        "is attached when the no-image case cannot be resolved). "
        "Read-only, sends no request to the provider. When the video model is not configured or its capabilities cannot be "
        "resolved, the corresponding problem is returned."
    ),
    request_model=NoArguments,
    migration=Exempt(
        "Read-only: queries model capabilities and project preferences, neither reads nor writes the artifact manifest and "
        "issues no write credential; it must stay usable when migration fails, for planning the repair."
    ),
    domain_key="video_capabilities",
    handler=get_video_capabilities,
)

GET_PROMPT_PREVIEW = ToolDeclaration(
    name="get_prompt_preview",
    description=(
        "Read the prompt text that one shot entry will finally send to the image / video model, through the same rendering exit as "
        "the execution path, verbatim equal to the prompt actually "
        "sent at generation time; when the prompt on one side is still pending generation or empty, that side carries a stable "
        "unavailable reason code instead of text. "
        "Read-only: sends no request to the provider and incurs no cost. When the project data upgrade has failed, a "
        "project_migration_failed problem is returned."
    ),
    request_model=PromptPreviewRequest,
    migration=READ_CHECK,
    domain_key="prompt_preview",
    handler=get_prompt_preview,
)

ORIENTATION_TOOLS = (GET_WORKFLOW_PLAN, GET_VIDEO_CAPABILITIES, GET_PROMPT_PREVIEW)

__all__ = [
    "GET_PROMPT_PREVIEW",
    "GET_VIDEO_CAPABILITIES",
    "GET_WORKFLOW_PLAN",
    "ORIENTATION_TOOLS",
]
