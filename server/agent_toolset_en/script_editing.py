"""Declaration of the formal script editing tool."""

from __future__ import annotations

from lib.i18n import _ as translate
from lib.script.storyboard_mentions import render_storyboard_mention_warnings
from server.agent_toolset.declaration import BLOCKED, ToolDeclaration
from server.tool_runtime import PatchEpisodeScriptRequest, ScriptPatchResult, patch_episode_script


def _script_patch_summary(result: ScriptPatchResult) -> str:
    if result.success:
        lines = [
            f"✅ patch_episode_script committed atomically; revision={result.revision}; "
            f"affected_ids={', '.join(result.affected_ids) or 'none'}",
            *(f"⚠️ {line}" for line in render_storyboard_mention_warnings(result.warnings, translate)),
        ]
        if result.regeneration_required_ids:
            lines.append(
                f"⚠️ Shots whose prompt changed ({', '.join(result.regeneration_required_ids)}) must have their images/videos regenerated right away."
            )
        return "\n".join(lines)
    problem = result.problems[0]
    locations = ", ".join(
        ".".join(str(part) for part in location.path) for location in problem.locations if location.path
    )
    return (
        f"patch_episode_script failed: code={problem.code}, operation_index={problem.operation_index}, "
        f"unit_id={problem.unit_id}, location={locations or None}, next_action={problem.next_action}"
    )


PATCH_EPISODE_SCRIPT = ToolDeclaration(
    name="patch_episode_script",
    description=(
        "Atomically execute ordered script operations (update / insert / move / remove / split) against the canonical revision. "
        "First build the complete candidate in memory, then validate schema, project references, the Artifact Manifest and "
        "SpeechComposition together; "
        "any problem means zero writes for the whole batch, the result is success=false, and problems gives a stable code, "
        "operation_index, unit/field location and "
        "next_action; speech composition problems additionally carry speech_admission. New entry ids for insert and split are "
        "allocated by the server: the number after the highest sequence number used in this episode; "
        "gaps in the numbering are normal, and numbers of deleted entries are never issued again; see the new ids in the result's "
        "affected_ids or re-read the script. "
        "When after_id is omitted, the entry is inserted at the very front; the first entry of an empty script is inserted this way. "
        "Entries removed by remove go to the project trash together with their media and version history, and can be restored. "
        "move changes the order: moves id to after after_id, or to the very front when after_id is omitted; the entry moves together "
        "with its already generated media. "
        "After a move, storyboard images whose preceding shot changed (except those at a segment_break) become stale; they are not "
        "listed in regeneration_required_ids, and whether to regenerate is up to the user. "
        "Paid media of existing entries in the script is never deleted or cleared; entries whose image_prompt / "
        "video_prompt changed are listed in regeneration_required_ids and must be explicitly regenerated right away; unbound "
        "@[名称] (@[name]) mentions in the visual description "
        "are listed in warnings. When writing source_text, if the project has source it must be a verbatim excerpt of this "
        "episode's source, otherwise it is refused with "
        "source_text_not_verbatim. When the script does not exist, a script_not_found problem is returned."
    ),
    request_model=PatchEpisodeScriptRequest,
    migration=BLOCKED,
    domain_key="script_patch",
    handler=patch_episode_script,
    summary=_script_patch_summary,
    is_error=lambda result: not result.success,
)

SCRIPT_EDITING_TOOLS = (PATCH_EPISODE_SCRIPT,)

__all__ = ["PATCH_EPISODE_SCRIPT", "SCRIPT_EDITING_TOOLS"]
