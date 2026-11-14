---
name: generate-grid
description: Generate grid storyboard images. Use when the user says "generate grids", "grid image generation", "generate storyboards with grid assembly". Automatically groups by segment_break, picks the optimal grid size, and generates chained transition-frame grid composites; after the user reviews and approves the composites, grid split turns them into each shot's starting storyboard image.
---

# Generate Grid Storyboard Images

Generate grid storyboard images for projects with grid assembly enabled. Groups automatically by segment_break, generating one grid composite per group (a group whose shot count exceeds a single grid's cell limit is split into several; if the last one falls short of a tier, it drops to a smaller tier and is padded with placeholder cells).

Grids work in two stages, with user review between them:

1. `generate_grid` only generates the composite and records a version; it does not write any storyboard image.
2. The user reviews the composite in the Web grid panel (may regenerate, upload a replacement, or roll back). **Only after the user explicitly approves the split** call `split_grids` for grid split: it overwrites all storyboard images covered by the grid, with old storyboard images kept in version history for rollback. Do not split on your own after generation finishes.

Storyboard images after the split still go through i2v, with the same input contract as storyboard images generated one by one.

## Prerequisites

- The project's `generation_mode` is `"storyboard"` and `grid_storyboard` is `true` (grid assembly is toggled by the user in the Web settings page and cannot be changed by the Agent after project creation)
- The script has been generated (scripts/episode_N.json exists)
- Character/scene/prop asset sheets (generated ones are brought in as reference images; with none at all it degrades to pure text-to-image, and visual consistency gets noticeably worse)

## Tool calls

| Operation | Tool |
|------|------|
| Fill gaps for the whole episode | `mcp__arcreel__generate_grid({"script": "episode_1.json"})` |
| Redo the grids containing specified shots | `mcp__arcreel__generate_grid({"script": "episode_1.json", "scene_ids": ["E1S01", "E1S02", "E1S03"]})` |
| Preview the plan and existing grid status | `mcp__arcreel__generate_grid({"script": "episode_1.json", "list_only": true})` |
| Grid split after user approval | `mcp__arcreel__split_grids({"grid_ids": ["grid_a1b2c3d4e5f6"]})` |

- Without `scene_ids` it only fills gaps: groups whose storyboard images are complete are reused, grids whose composite is ready but not yet split are not regenerated, grids being generated reuse the in-flight task; nothing is billed twice.
- Passing `scene_ids` is a redo request: the grids containing those shots are regenerated and billed; use only when the user explicitly asks for a redo.
- Admission is batch-wide: if any shot is blocked (reference gap, prompt pending, partial overlap with an in-flight grid, etc.), no task is created for the whole batch; shots that are themselves healthy carry `generation_batch_admission_withheld`, grids already generating run to completion with their shots carrying `generation_active_task_conflict` (`wait_for_task`); fix all gaps and retry to submit in one go.

Results are returned per **shot** ID under `requested / succeeded / failed / blocked`: shots in the same group share one grid, and that grid's enqueue and task result are projected onto every shot it covers. A successful shot's `artifact_path` is the composite of its grid `grids/<grid_id>.png`; grids not yet split are also listed in `grid_ids_awaiting_split`. See `.claude/references/generation-results.md` for the structure.

`split_grids` returns results per grid: split grids list the shots written; grids still generating, without a composite, or nonexistent are skipped with the reason stated.

## Output

- Grid composites are saved to `grids/{grid_id}.png` (`grid_id` itself carries the `grid_` prefix, e.g. `grids/grid_a1b2c3d4e5f6.png`); each generation/upload is recorded as a grids version
- Frame-chain metadata is saved to `grids/{grid_id}.json` (`split_at` records the time of the most recent grid split from the current composite)
- Cells after grid split are written to disk per `next_scene_id`, with file names aligned to ordinary storyboard images as `storyboards/scene_{id}.png` (no first/last suffix); each cell is entered into version history both before and after overwriting
