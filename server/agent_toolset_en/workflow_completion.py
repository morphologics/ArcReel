"""Declaration for the pending-completion tool: atomically records into the project a completion fact the workflow plan requires, so the plan can keep converging.

A completion fact cannot be inferred from file content (a rebuild may be identical, character for character, to the old file); it can only be submitted explicitly by the party that produced it.
"""

from __future__ import annotations

from server.agent_toolset.declaration import BLOCKED, ToolDeclaration
from server.tool_runtime import CompleteScriptPlanRebuildRequest, complete_script_plan_rebuild

COMPLETE_SCRIPT_PLAN_REBUILD = ToolDeclaration(
    name="complete_script_plan_rebuild",
    description=(
        "Call after a stale episode's script_plan has been rebuilt successfully, to atomically record the fact that the rebuild is complete; even if the rebuilt content is identical, character for character, to the old script_plan, "
        "the workflow plan can then keep converging. Use only when the workflow plan's next_action.args carries expected_stale_script_plan_revision, "
        "and pass that value as is. Refused, with nothing written, when the episode is not awaiting a rebuild (not_stale), the rebuild baseline is missing (missing_baseline), the baseline has changed (baseline_conflict) "
        "or the formal script_plan file is missing (script_plan_missing); on a conflict, refresh the workflow plan and do not retry with the old arguments. "
        "On success it returns the revision of the episode and of the rebuilt script_plan."
    ),
    request_model=CompleteScriptPlanRebuildRequest,
    migration=BLOCKED,
    domain_key="script_plan_rebuild",
    handler=complete_script_plan_rebuild,
)

WORKFLOW_COMPLETION_TOOLS = (COMPLETE_SCRIPT_PLAN_REBUILD,)

__all__ = ["COMPLETE_SCRIPT_PLAN_REBUILD", "WORKFLOW_COMPLETION_TOOLS"]
