"""Declarations for the grid storyboard tools: generate grid composites, and after user approval, grid split.

``generate_grid`` is a long generation task; the ``list_only`` preview is not a generation result, it returns immediately and the grid's own result hook
places it under the tool name; other values have the same shape as the generation tools. ``split_grids`` is an ordinary write tool.
"""

from __future__ import annotations

from server.agent_toolset.declaration import BLOCKED, ToolDeclaration
from server.agent_toolset.media_generation import generation_tool
from server.media_tools.grid import (
    GenerateGridRequest,
    SplitGridsRequest,
    generate_grid,
    grid_is_error,
    grid_structured,
    grid_summary,
    split_grids,
)

GENERATE_GRID = generation_tool(
    name="generate_grid",
    description=(
        "For a storyboard project with grid assembly enabled (generation_mode=storyboard and grid_storyboard=true), "
        "generate grid composites (paid generation; grouped by segment_break, and a group exceeding the per-image cell limit is split into several images). "
        "This tool only produces composites and does not write any storyboard image: a successful shot reports the composite of the grid it belongs to "
        "(artifact_path is grids/<grid_id>.png), with the status \"composite ready, not split\"; "
        "in the terminal result, the grid_ids of unsplit grids are also listed in grid_ids_awaiting_split. "
        "The batch handle and the batch terminal-state query do not carry that field: for grids that are ready but unsplit, see the artifact_path of each skipped item, "
        "and for those newly produced this time, see the artifact_path of the successful shots. "
        "Before grid split, first ask the user to review the composites in the grid panel, and call split_grids only after the user explicitly agrees. "
        "list_only=true only previews the plan without enqueuing, and the result is placed under the generate_grid key. "
        "Without scene_ids, stale but usable old images are reused as usual, and grids whose composite is ready but not split are not regenerated; "
        "when a grid for the same group of shots is already being generated, the in-flight task is reused and not billed again. "
        "Admission is per whole batch: if any shot is blocked (reference gap, prompts pending generation, partial overlap with an in-flight grid, etc.) no task is created for the whole batch, "
        "shots that are themselves healthy carry generation_batch_admission_withheld; grids already being generated run to completion as usual, and their shots carry "
        "generation_active_task_conflict (action=wait_for_task). "
        "The terminal result's generation_result gives the outcome per shot ID as requested / succeeded / failed / blocked."
    ),
    request_model=GenerateGridRequest,
    handler=generate_grid,
    summary=grid_summary,
    projection=grid_structured,
    is_error=grid_is_error,
)

SPLIT_GRIDS = ToolDeclaration(
    name="split_grids",
    description=(
        "Grid-split the composites of one or more grids: write them out as the storyboard images of all the shots they cover; old storyboard images stay in the version history and can be rolled back. "
        "Call only when the user explicitly agrees: after generate_grid completes, first ask the user to review the composites in the grid panel (they can regenerate, upload a replacement or roll back), "
        "and pass these grid_ids only after the user confirms the split; do not call it on your own after generation completes. "
        "Take the grid_id from grid_ids_awaiting_split in the generate_grid result, from the artifact_path of successful shots (grids/<grid_id>.png), "
        "or from the generate_grid list_only preview. "
        "Returns results per grid: split grids list the shots written; grids still generating, without a composite, or nonexistent are skipped with the reason; "
        "when not a single one was split it returns a problem, with per-grid reasons in params.results."
    ),
    request_model=SplitGridsRequest,
    migration=BLOCKED,
    domain_key="split_grids",
    handler=split_grids,
)

GRID_STORYBOARD_TOOLS = (GENERATE_GRID, SPLIT_GRIDS)

__all__ = ["GENERATE_GRID", "GRID_STORYBOARD_TOOLS", "SPLIT_GRIDS"]
