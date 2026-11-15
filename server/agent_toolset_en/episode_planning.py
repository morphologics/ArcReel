"""Episode planning tool declarations: plan the episodes of the next source window, and roll the ledger back to the unplanned state.

``plan_episodes`` is a long task: the ArcReel Agent waits for planning to finish and receives ``episode_plan``; an external Agent
immediately receives a ``generation_batch`` handle and polls it. Reset does not call the text model; both hosts return synchronously.
"""

from __future__ import annotations

from typing import Any

from lib.generation.generation_batch import GenerationBatchReadModel
from server.agent_toolset.declaration import BLOCKED, TEXT_OUTPUT_TRUNCATED_NOTE, ToolDeclaration
from server.agent_toolset.envelope import json_value
from server.tool_runtime import (
    PlanEpisodesRequest,
    PlanEpisodesResult,
    ResetEpisodePlanningRequest,
    plan_episodes,
    reset_episode_planning,
)


def _plan_structured(value: PlanEpisodesResult | GenerationBatchReadModel) -> dict[str, Any]:
    """Project a batch handle to ``generation_batch``, and the ledger summary of a finished plan to ``episode_plan``."""
    if isinstance(value, GenerationBatchReadModel):
        return {"generation_batch": json_value(value)}
    return {"episode_plan": json_value(value)}


PLAN_EPISODES = ToolDeclaration(
    name="plan_episodes",
    description=(
        "Episode planning: following the order of the whole-source file list registered for the project, read one source window starting "
        "from the end of the last split-off episode, and call the project's configured text model once to "
        "plan every episode in the window whose story arc is complete (title / hook / source range; drama additionally includes an episode outline); "
        "within the same project lock, write the ledger, derive "
        "source/episode_{episode ID}.txt and clean up leftover derived files. Newly split-off episodes are placed after the last split-off episode; "
        "episodes with their own source and episodes without source do not take part in "
        "splitting and keep their positions. Files in source/ that are not registered in the file list are not whole source and are not read. "
        "Returns the ledger summary (for each episode: title, episode ID and airing position, hook, size, plus the first and last sentence of "
        "its source, with overlong sentences truncated); the first and last sentences let the user check "
        "the episode boundaries. New episodes get IDs incremented from the project's historical highest episode ID; an episode ID does not "
        "indicate episode order, so refer to an episode to the user by its title and airing position. "
        "The source window is fixed at 50000 characters, and the maximum number of episodes planned per batch is derived from the maximum "
        "output length registered for the current text model; neither is configurable. "
        "If no split point with a complete story arc can be found in the window, this batch fails; ask the user to split this section off "
        "by hand first and then continue planning."
        + TEXT_OUTPUT_TRUNCATED_NOTE
        + " Only one episode planning can run per project at a time; calls are refused while the Web \"Plan episodes with AI\" "
        "is in progress. "
        "The target size per episode is episode_target_units; when it is unset, it is converted from episode_target_duration via the "
        "voice-over speech rate (the converted value states its source in the returned check material). "
        "When the user gives additional episode instructions (e.g. split aligned with chapters, end at a specific point), pass the "
        "original text via instructions; the planning prompt additionally includes "
        "the number of episodes already planned, the remaining unplanned amount and the size of this window, for working out the split "
        "pace. Planning runs in several batches by window (a long work calls this tool several times), and instructions "
        "are not persisted, so every batch call must repeat the same additional instructions until planning is complete. On the last "
        "batch, i.e. when all source has been planned or a further call finds no new content, "
        "the return additionally carries global size check material (cumulative episode count, the smallest few episodes by size, median "
        "size, target size): if the user gave a total episode count, chapter alignment or other "
        "structural preferences, you must check against them and tell the user clearly about any deviation; regular batches report only "
        "the cumulative number of planned episodes. On commit, a content fingerprint is recorded for each source file involved; if it "
        "detects that "
        "the content of a recorded source file has changed or disappeared (source replaced / edited / deleted, even if the ledger "
        "coordinates are still in range), planning is refused and the changed files are named; "
        "you must then call reset_episode_planning for a full reset before planning again. It is also refused when the Web "
        "\"Episodes\" view holds a pending \"New episode plan\" "
        "(a replan candidate); ask the user to adopt or discard it in the \"Episodes\" view first."
    ),
    request_model=PlanEpisodesRequest,
    migration=BLOCKED,
    domain_key="episode_plan",
    handler=plan_episodes,
    long_task=True,
    projection=_plan_structured,
)

RESET_EPISODE_PLANNING = ToolDeclaration(
    name="reset_episode_planning",
    description=(
        "Reset episode planning: the escape hatch that rolls the ledger back to the unplanned state; it does not call the text model "
        "and is not affected by provider configuration. "
        "It only touches episodes split from the whole source; episodes with their own source and episodes without source are kept as is. "
        "Omitting episode_id (or giving the episode ID of the first split-off episode in episode order) is a full reset: it removes "
        "all split-off episodes; use it to recover when the source was replaced or deleted and rebuilt, "
        "or when a corrupted ledger makes planning fail permanently with errors such as \"start out of range\" or \"invalid range\"; "
        "it has zero preconditions and succeeds from any corrupted state. "
        "Giving the episode_id of another split-off episode is a partial reset: it keeps the episodes before that episode in episode "
        "order and clears only that episode and the split-off episodes after it; the start of the next planning batch "
        "moves back accordingly to the end of the last split-off episode in the kept part; this path has preconditions (all recorded "
        "source fingerprints must match the current source, and the coordinates of the kept part must lie within the current source, "
        "advancing in order without overlap); if any is not met, it refuses and states the specific reason, in which case use a full "
        "reset instead. Episodes planned afterwards by plan_episodes get "
        "new episode IDs; cleared episode IDs are never reused. In both modes, when consumed episodes are affected (they already have a "
        "script_plan / script / media artifacts) it does not execute; "
        "it returns confirmation_required=true and the list of affected items; you must tell the user and, after confirmation, call "
        "again with confirm_consumed=true. "
        "After confirmation, consumed episodes become episodes without source, are marked stale (source has been replanned) and moved to "
        "the end of the episode order; the script, media and other artifacts and their artifact records all stay with them "
        "and are not attached to newly planned episodes; split-off episodes without artifacts are removed from the ledger. Within the "
        "reset range, derived episode files that the ledger can recreate are deleted; episode files without a recorded source range "
        "are renamed and kept (no content is lost); derived files of the kept part are not affected."
    ),
    request_model=ResetEpisodePlanningRequest,
    migration=BLOCKED,
    domain_key="episode_reset",
    handler=reset_episode_planning,
)

EPISODE_PLANNING_TOOLS = (PLAN_EPISODES, RESET_EPISODE_PLANNING)

__all__ = ["EPISODE_PLANNING_TOOLS", "PLAN_EPISODES", "RESET_EPISODE_PLANNING"]
