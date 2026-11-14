---
name: generate-storyboard
description: Generate storyboard images for shots. Use when the user says "generate storyboards", "preview the storyboard frames", wants to regenerate certain storyboard images, or when shots in the script are missing storyboard images. Automatically maintains character and visual continuity.
---

# Generate Storyboard Images

Create storyboard images through the generation queue; the aspect ratio is set automatically from content_mode.

> For generation mode specs, see `.claude/references/generation-modes.md`.

## Tool calls

**Important: generating storyboard images MUST be done by calling the MCP tools below to enqueue. This skill provides no Python/Shell scripts; NEVER use BASH to call `python .../scripts/*.py`.**

Enqueue via MCP tools:

| Operation | Tool |
|------|------|
| Submit all missing storyboard images | `mcp__arcreel__generate_storyboards({"script": "episode_1.json"})` |
| Regenerate a specified ID | `mcp__arcreel__generate_storyboards({"script": "episode_1.json", "segment_ids": ["E1S05"]})` |
| Regenerate several IDs | `mcp__arcreel__generate_storyboards({"script": "episode_1.json", "segment_ids": ["E1S01", "E1S02"]})` |

> **Selection rule**: `segment_ids` accepts both narration segment_id and drama scene_id; if omitted, all missing items are submitted.
>
> **Dependency**: the generation worker must be online (two independent channels, image/video); the worker handles actual generation and rate control.

## Workflow

1. **Load the project and script** — confirm every character has a `character_sheet` image
2. **Generate storyboard images** — the MCP tool detects content_mode automatically and chains dependent tasks by adjacency
3. **Review checkpoint** — following "Review checkpoint: review images", dispatch the review-images subagent to look at the images, and hand the report and regeneration suggestions to the user for confirmation; the user may approve, request regeneration, or request edits
4. **Update the script** — update the `storyboard_image` path and scene status

## Review checkpoint: review images

After storyboard images are generated, first have the read-only review-images subagent `review-images` look at them, then hand its conclusions to the user for confirmation. Image review never triggers generation or editing and incurs no generation cost; the subagent's model calls for viewing images count toward Agent session usage as usual. Grid projects are reviewed after grid split, once storyboard images are written.

1. **Dispatch**: dispatched by the main Agent (subagents cannot dispatch subagents: when this skill runs inside a generation subagent, the generation results go back to the main Agent, which then dispatches). Dispatch only the shots generated successfully in this round, grouped by adjacent shots, at most 20 images per group; with multiple groups, issue one `review-images` call per group in the same message so the groups run in parallel. The dispatch message states the image type `storyboard`, the script file (e.g. `scripts/episode_1.json`), this group's shot IDs and the user's concerns.
2. **Hand to the user**: summarize the groups' reports for the user: which are usable, which are suggested for regeneration or editing and why, which are doubtful. Refer to a shot by its position in this episode (e.g. "shot 5"), not by shot ID. When any shot is suggested for regeneration, editing, or is doubtful, use AskUserQuestion multi-select (`multiSelect: true`) to have the user tick the shots to handle; each option states the position, the reason and the suggested handling. Do not regenerate or edit before the user confirms.
3. **Execute**: handle the ticked shots per the next section "Edit vs regenerate", using shot IDs when calling tools. When done, you may dispatch `review-images` again on the new versions for acceptance.

When the user asks follow-up questions about specific storyboard images, use `mcp__arcreel__inspect_images` to look at the images directly and answer, without dispatching a subagent.

## Review checkpoint: edit vs regenerate

- **Only wants a local change** (e.g. a deformed hand in some shot, background clutter, lighting off), and is happy with composition/character consistency →
  use `mcp__arcreel__edit_images({"resource_type": "storyboard", "script_file": "episode_1.json", "edits": [{"id": "E1S05", "instruction": "去掉背景里多余的路人"}]})`
  to fine-tune on top of the existing image, without affecting `image_prompt`; multiple shots can be sent in one batch
- **Wants to overturn the composition or the character/reference-image relationships**, or `image_prompt` itself must change → use
  `patch_episode_script` to change `image_prompt`, then immediately call `generate_storyboards` to regenerate
- Editing does not update `image_prompt` — triggering `generate_storyboards` after an edit still redraws from
  the original `image_prompt`, and the edited result can only be recovered from version history

## Character consistency mechanism

The MCP tool automatically handles passing in the following reference images; no manual specification needed:
- **character_sheet**: asset sheets of the characters appearing in the scene, keeping appearance consistent
- **scene_sheet / prop_sheet**: scene / prop asset sheets appearing in the scene
- **Product reference (ad/short film projects)**: when a shot's `products_in_shot` is non-empty, product references are injected automatically and placed before all other references (with a product sheet: sheet + original images; without a sheet: original images injected directly); the declaration line marks these positions as product reference images and requires the product in frame to match them exactly — image_prompt need not restate the product's appearance
- **Previous storyboard image**: referenced by default for adjacent segments, placed last in the reference image list, used only for composition and color tone
- When a segment is marked `segment_break=true`, the previous storyboard image reference is skipped

To the model, reference images are just an ordered array of images; the prompt refers to them as 「图N」 ("image N") (N = the reference image's position in the array: products first,
then character / scene / prop asset sheets, the previous storyboard image last):
- The generation side inserts a `Reference_Images` type-declaration line between `Style` and `Scene`, e.g.
  `图1、图2为角色参考图；图3为场景参考图；图4为上一分镜图，只参考构图与色调。`; for product shots it is
  `图1为商品参考图，画面中的商品须与之完全一致；……`
- In `image_prompt.scene`, refer to assets with `@[登记名]` ("registered name") (derivative looks written as `@[角色/衍生]` ("character/derivative")); the generation side replaces it with the corresponding 「图N」 per the numbering above;
  that name must also be written into the entry's reference fields (`characters_in_*` / `scenes` / `props` / `products_in_shot`); reference images are determined only by the reference fields, and `@[]` does not bring an extra image
- An `@[名称]` ("name") that is unregistered or not in the reference fields is sent as the name verbatim; the `warnings` in the `generate_episode_script` / `patch_episode_script` receipt list such references; follow the hints to register them or add the reference field, then regenerate

## Prompt template

The generation side renders the final prompt from the project style, the reference image list and the entry's `image_prompt` (`get_prompt_preview` shows the verbatim text):

```
Style: [项目 style]
Reference_Images: 图1、图2为角色参考图；图3为场景参考图；图4为上一分镜图，只参考构图与色调。
Scene: [image_prompt.scene，其中 @[登记名] 已换成对应的图N]
Composition:
  shot_type: [image_prompt.composition.shot_type]
  lighting: [image_prompt.composition.lighting]
  ambiance: [image_prompt.composition.ambiance]
Avoid: 水印、多余文字、Logo
```

> The aspect ratio is set via API parameters and is not written into the prompt.

## Pre-generation checks

- [ ] Every character has an approved character_sheet image
- [ ] Scene visual description is complete
- [ ] Character actions are specified

## Error handling

See `.claude/references/generation-results.md` for the result structure and per-ID problem codes.

- A single scene failing does not affect the batch; the tool returns per-ID results under `requested / succeeded / failed / blocked`
- Decide whether to retry or fix the input first by each item's own `problem.code` and `problem.action`; do not guess from the text
- Omitting `segment_ids` only fills gaps; stale but usable old storyboard images are reused, not regenerated automatically
- Redo retryable scenes by name with `mcp__arcreel__generate_storyboards({"script": "...", "segment_ids": [...]})`
