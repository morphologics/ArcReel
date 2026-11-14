---
name: video-workflow
description: Workflow entry point for ad/short film projects. You MUST use this skill whenever the user mentions making a video, continuing a project, or checking progress. Trigger scenarios include but are not limited to: "make me a product promo video", "continue", "next step", "show me the project progress", etc. Even if the user only says a short "continue" or "next step", trigger it as long as the current context involves a video project. Do not use it for single-asset generation (such as only redrawing one storyboard image or only regenerating one character asset sheet - those have dedicated skills).
---
<!-- mode: ad -->

# Ad/Short Film Workflow

This project is an **ad/short film** (ad): a single video, always a single episode (the screenplay is `scripts/episode_1.json`), with shots planned by `target_duration`. **There is no concept of episodes** - do not do episode planning, splitting, or novel source file processing.

## Workflow steps

First call `mcp__arcreel__get_workflow_plan({})` to fetch the authoritative plan. Treat `steps[]`, `blockers`, `status.operations`,
and `next_action` as the single source of truth for stage decisions (`plan.status` embeds the `project` / `target` / `content` / `issues` /
`gates` / `artifacts` snapshot); Read only supplements creative input and product soft gate information. Refresh the plan after every action completes.
When `blockers` is non-empty, show them and stop making changes; `next_action.type == "none"` with `edit` being `completed` in `steps` means the workflow
is finished; in other `none` cases without blockers, explain `status.issues` to the user.

When the user says "continue", advance per `next_action`, and when `next_alternatives` is non-empty, list the options for the user to choose; when the user names an operation,
execute it if it is `admitted` (or not listed) in `status.operations`, and when it is `refused`, relay the `reason` to the user
(such as `ad_brief_and_products_missing`: neither the creative brief nor products are filled in).

For the meaning of the plan fields, the full controlled action table, narration delivery, batch admission, the four status axes, and the stale / history discipline, see
[.claude/references/workflow-plan.md](../../references/workflow-plan.md). **This skill does not repeat a step table expanded by generation mode**:
which steps apply is expressed by `plan.steps[].required`.

Go directly to the corresponding step by `next_action.type`:

- `next_action.type == "collect_project_input"` → step 2
- `next_action.type == "generate_script"` → step 5
- `next_action.type == "start_blank_script"` / `"add_script_items"` → guide the user to write by hand or add entries on the Web side (see the workflow-plan reference)
- `next_action.type == "generate_asset_sheets"` → step 4
- `next_action.type == "repair_video_units"` → the video unit repair in step 7
- `next_action.type == "generate_storyboards"` → the storyboard single-image path in step 7
- `next_action.type == "generate_grid"` → the storyboard grid path in step 7
- `next_action.type == "generate_videos"` → the video generation in step 7
- `next_action.type == "create_edit_timeline"` → step 8

When calling tools or dispatching subagents, carry in `target.episode`, `next_action.args`, and `requested_ids`; do not re-check `generation_mode` or `grid_storyboard` to change the chosen stage. The product reference image and sheet review rules within the steps are soft gates before executing an action.

1. **Confirm project state**: confirm `content_mode=ad` and the project-level `generation_mode` per the plan; Read `project.json` to supplement `title`, `target_duration`, `brief`, and `products`. The generation mode cannot be changed after creation.
2. **Creative input**: when a product promo project has no registered products or is missing original images, guide the user to upload them in the WebUI; the original image is the fidelity anchor. Use `mcp__arcreel__patch_project` to write product descriptions, brand, and `brief`. A general short film does not ask for products.
3. **Draft selling points**: selling points do not block script generation. When `status.content.products_without_selling_points` is non-empty, you may offer to draft them; when the user agrees or asks on their own, draft them from the brief, descriptions, and original images, confirm with the user, then write back with `patch_project`.
4. **One-click screenplay generation**: once there is at least one of `brief` and products, call `mcp__arcreel__generate_episode_script({"episode_id": target.episode})`. Ads do not go through script_plan; the result directly becomes the formal script; storyboard-to-video directly produces `shots[]`, and reference-to-video directly produces self-contained `video_units[]`. New characters, scenes, and props referenced by the screenplay are registered as pending assets along with it, and the receipt lists them. When the total duration deviates from `target_duration`, remind the user without blocking the save. When the user wants the whole thing redone, pass `regenerate: true`; for confirming the loss list and handling violation failures, see the generate-script skill.
5. **Asset sheets**: call `mcp__arcreel__generate_assets({"episode_id": <next_action.args.episode_id>})`: the server generates all assets referenced by this episode that still lack asset sheets (including products, derivatives, and assets newly registered during screenplay generation), the same list as at the Web episode level. After generation completes, per "Review checkpoint: review images" in [generate-assets](../generate-assets/SKILL.md), dispatch `review-images` and hand the review report to the user for confirmation.
6. **Sheet review (soft gate)**: when a product has a `product_sheet`, ask the user to confirm it matches the real product before the first storyboard or reference-to-video generation; when there is only an original image, continue directly.
7. **Orchestration and generation**:

   - `repair_video_units`: call `mcp__arcreel__get_episode_script({"script": target.script_filename})` to read the body and revision, and only handle the video units corresponding to `requested_ids`; then use a single `mcp__arcreel__patch_episode_script({"script": target.script_filename, "base_revision": revision, "operations": [{"op": "update", "id": unit_id, "fields": {"text": "...", "duration_seconds": ...}}]})` to write back the complete plan for all video units (one ordered update per video unit); `needs_replan` is recomputed by the tool, do not edit the marker directly. Each video unit keeps a single speaker attribution, and products / characters / scenes / props all use `@[名称]` (`@[name]`). After the fix, immediately name these video units for a redo with `generate_videos`' `selected` scope and `force: true`, then refresh the status.
   - `next_action.type == "generate_storyboards"` → call
     `mcp__arcreel__generate_storyboards({"script": target.script_filename, "segment_ids": requested_ids})`
   - `next_action.type == "generate_grid"` → call
     `mcp__arcreel__generate_grid({"script": target.script_filename})` (do not pass `scene_ids`: generate-if-missing, without redoing grids whose grid composite is ready but not split)
   - After storyboard images are written (for grids, after grid split), per "Review checkpoint: review images" in [generate-storyboard](../generate-storyboard/SKILL.md), dispatch `review-images` and hand the review report to the user for confirmation
   - `next_action.type == "confirm_request_duration"` → per `admission.confirmation.tiers[]`, show tier by tier
     the video units and cost involved, and after confirmation carry them back via `confirmed_request_durations` into the next `mcp__arcreel__get_workflow_plan`
   - `next_action.type == "generate_videos"` → first look at `plan.steps[].admission.decision`: only enqueue when it is `admitted`;
     when it is `blocked` / `confirmation_required`, **enqueue no tasks at all**, and report per video unit the `unit_id`,
     `problems[].code`, the reason, and `problems[].action` (video units dragged down by `blocked_unit_ids` carry
     `generation_batch_admission_withheld`; state truthfully that it is not a problem of their own); after fixing the rejected video units, redo the whole batch;
     do not split the batch to run the passing half first. When enqueueing, if `requested_ids` is non-empty call
     `mcp__arcreel__generate_videos({"script": target.script_filename, "target": {"scope": "selected", "ids": requested_ids}, "force": true})`;
     only when `requested_ids` is empty call `mcp__arcreel__generate_videos({"script": target.script_filename, "target": {"scope": "episode", "episode_id": target.episode}})`.
     After returning, state the results itemized per ID (`succeeded` / `failed` / `blocked` / `skipped`), and state separately the four axes of workflow step
     status, queue tasks, provider checkpoint, and artifact currency - "task succeeded" does not equal "the current artifact is valid";
     stale artifacts can still be used as usual; whether to redo them is for the user to decide; never automatically delete or regenerate paid artifacts
   - When a product promo project uses storyboard-to-video, first review the fidelity of the product shots, then incur video costs; a general short film has no product shots and has no such review.
   - Reference-to-video generates by self-contained video units and skips storyboards; reference images are resolved at execution time in the order of first mention of `@[名称]` (`@[name]`) in the body, with products following the same rule as characters, scenes, and props. When the user is unhappy, name the `unit_id` for a redo.

8. **Edit**: for TTS voiceover projects (`plan.status.artifacts.audio.missing_ids` non-empty), first per the `generate-narration-audio` skill
   call `mcp__arcreel__generate_narration_audio({"script": target.script_filename})` to fill in the missing narration audio, without
   `segment_ids`; choosing TTS for the project is already authorization. This step is complete as soon as the edit timeline is created, and the workflow will not come back here afterward to fill in voiceover.
   Then call `mcp__arcreel__create_timeline({"from": "script", "episode": target.episode, "name": "完整版"})` ("完整版" = "Full cut")
   to mechanically create an edit timeline from the script; once at least one edit timeline exists, this step is complete. Afterward, per the `edit-video` skill,
   edit on it, and only render the final cut or export a JianYing draft when the user asks. Sound attribution and subtitle timing are decided by the server's presentation result -
   **do not estimate the subtitle timeline yourself, and do not judge on the user's behalf whether TTS is required**. Stale artifacts can still be used as usual; rendering neither clears nor
   overwrites old paid media.

## General short film (no products)

An empty `products` means a general short film. Skip product upload, selling points, and the product sheet; flesh out `brief` enough to express theme, mood, visual style, and pacing. Character, scene, and prop assets remain available as usual.

## Shot duration constraints

Storyboard-to-video shot durations take members of the video model's `supported_durations`, which can be queried with `mcp__arcreel__get_video_capabilities`. Reference-to-video video units use positive-integer orchestration durations allowed by the screenplay model, and generation preflight then projects them onto provider tiers. When an illegal value is found, first fix it with `patch_episode_script`.

## Boundaries

- Storyboard ads use `shots[]` as the single source of truth; reference ads use self-contained `video_units[]` as the single source of truth.
- Reference-to-video video units own their reference-syntax body, orchestration duration, generated assets, and planning state; refresh the plan after editing these fields.
- Use `patch_episode_script` for video unit reordering and field repair, and the `generate-video` skill for video generation.
