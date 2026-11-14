---
name: video-workflow
description: End-to-end workflow orchestrator for turning a novel into short videos. You MUST use this skill whenever the user mentions making a video, creating a project, continuing a project, or checking progress. Trigger scenarios include but are not limited to: "turn my novel into a video", "start a new project", "continue", "next step", "show me the project progress", "start from scratch", "split into episodes", "run the whole workflow automatically", etc. Even if the user only says a short "continue" or "next step", trigger it as long as the current context involves a video project. Do not use it for single-asset generation (such as only redrawing one storyboard image or only regenerating one character asset sheet - those have dedicated skills).
---
<!-- mode: narration -->

# Video Workflow Orchestration

You (the main Agent) are the orchestration hub. You **do not directly** process the novel text or generate the screenplay; instead you:
1. Detect project state → 2. Read the plan's `next_action` → 3. Dispatch the appropriate subagent → 4. Show the result → 5. Get user confirmation → 6. Loop

**Core constraints**:
- The novel text is **never loaded into the main Agent context**; subagents read it themselves
- Each dispatch passes only **file paths and key parameters**, never large blocks of content
- Each subagent returns after completing one focused goal; the main Agent handles the hand-off between actions

> For the data structure and schema differences between the two generation modes (storyboard-to-video storyboard, with the grid_storyboard grid switch / reference-to-video reference_video), see `.claude/references/generation-modes.md`; step applicability is expressed by the plan and is not repeated in the reference doc.

---

## `collect_project_input`: Project setup

**Important**: The project directory is created by the Web side's `POST /api/v1/projects`, which triggers `ProjectManager.create_project()` (including all subdirectories and `project.json`, and materializing the Agent profile matching the content_mode). **The main Agent does not create directories and does not write initial project.json fields** - when the session starts, cwd is already bound to the existing project root.

### New project

1. Prompt the user to first create the project on the Web side, **specifying content_mode (narration / drama) and generation_mode (storyboard / reference_video) at creation**; neither can be changed after creation, and the Agent has no write permission for them. After the session starts, cwd is bound to the corresponding project root
2. Use the Read tool to read `project.json` and confirm the `title`, `content_mode`, and `generation_mode` fields (this session's current content_mode is `narration`, which cannot be changed after creation)
3. Ask the user to upload the novel text (the whole-book source file) on the Web side, or write it via `mcp__arcreel__upload_source`; a file placed directly into `source/` without being registered is not a source file
4. **After upload, automatically generate the project overview** (synopsis, genre, theme, world_setting)

> Standard project subdirectories are created automatically by `create_project()`: `source/`, `scripts/`, `drafts/`, `characters/`, `scenes/`, `props/`, `storyboards/`, `grids/`, `videos/`, `reference_videos/`, `thumbnails/`, `output/`.

### Existing project

1. The session cwd is already bound to the target project root
2. Call `mcp__arcreel__get_workflow_plan({})` to get the server's authoritative plan
3. Continue according to the returned `next_action` (alternatives at a fork are in `next_alternatives`)

---

## Plan query

When entering the workflow, when the user says "continue / next step / check progress", and after every tool or subagent completes, call
`mcp__arcreel__get_workflow_plan({})` to fetch the authoritative plan (when the user names a specific episode, find that episode in the project's `episodes[]` by the airing position or title they gave, and pass its episode ID `{"episode_id": X}`),
then route by `next_action.type` to the same-named section below.

For the meaning of the plan fields, the full controlled action table, narration delivery, batch admission, the four status axes, and the stale / history discipline, see
[.claude/references/workflow-plan.md](../../references/workflow-plan.md). **This skill does not repeat a step table expanded by content mode
or generation mode**: which steps apply, the suggested next step, and which operations can run right now are always read from `plan.steps[]`,
`plan.next_action`, and `plan.status.operations`; they are the single source of truth for stage decisions. `plan.status` embeds the full status snapshot
(`project` / `target` / `content` / `operations` / `blockers` / `issues` / `gates` / `artifacts`), so there is no need to query status separately.

After the call, take `plan.status.target.episode` (the target episode's episode ID; N in the paths and parameters below is this) as the target episode, and carry `next_action.args` and `requested_ids` unchanged into
the corresponding action. Read / Glob are only for content needed to execute the already-chosen action, not for building a separate state machine; never override the server's conclusion based on empty asset buckets,
file names, the existence of old files, or conversation memory.

The sections below are titled by `next_action.type`; actions without their own section (such as `resolve_draft`, `start_blank_script`,
`add_script_items`) are executed per the controlled action table in the workflow-plan reference. For `none`: if there are `blockers`, show them and stop making changes;
`edit` being `completed` in `steps` means the workflow is finished; otherwise explain `status.issues` and `status.content` to the user.
For the difference between the user naming an operation and saying "continue", see "Flexible entry".

> Batch narration audio is not driven by `next_action`; for when it is triggered, see the "Batch narration audio" section.

---

## Inter-action confirmation protocol

**After each subagent returns**, the main Agent does:

1. **Show the summary**: show the summary returned by the subagent to the user
2. **Get confirmation**: use AskUserQuestion to offer options:
   - **Continue to the next action** (recommended)
   - **Redo this action** (re-dispatch with additional change requests)
   - **Skip this action**
3. **Act according to the user's choice**

---

## `plan_episodes` / `reset_episode_planning`: Episode planning

**Recovery trigger**: when `next_action.type` is `"reset_episode_planning"`, first call
`mcp__arcreel__reset_episode_planning` with `next_action.args`. If the tool returns a consumed-episode confirmation requirement, show the scope of impact and get the user's explicit confirmation,
then retry with `confirm_consumed: true` added; after a successful reset, refresh the plan and continue with the new authoritative action.

**Trigger**: `next_action.type == "plan_episodes"`

**Skip this section when the user has split episodes themselves**: register each episode's source text the user split, one by one, via `mcp__arcreel__upload_source` (`role=episode`) as episodes with their own source text, or fill them in directly on the episode page; these episodes do not take part in episode planning, and the plan goes straight to script planning for each. Register each episode as-is as one episode, keeping the user's split and order; merging into a whole book and re-splitting with `plan_episodes` is only done when the user explicitly asks for re-splitting. Episode planning and reset only touch episodes cut from the whole-book source; episodes with their own source text and episodes without source text are kept as-is.

Episode planning is done by a server tool: internally, following the order of the whole-book source file list (`whole_source_files`), the tool reads a source window starting from the end of the last cut episode, calls the project's configured text model to plan in one go all episodes within the window whose story arcs are complete (title / hook / source range), and within the same project lock writes the ledger, derives `source/episode_{N}.txt`, and cleans up leftover derived files. **The main Agent calls the tool only once and only receives the summary** - it does not read the novel text and does not choose break points itself:

1. Before planning, quickly check `project.json`:
   - Whether `source_language` matches the actual language of the source. Priority: **user's explicit configuration > automatic inference** (in the normal path it is persisted automatically by overview generation); when a mismatch is found, **remind the user (WARN), explain the consequences, and suggest a fix** (a wrong configuration distorts the planner's size measurement and language assumptions); if the user does not fix it, continue with the explicit configuration without blocking the flow. When the field is missing or the user confirms it is wrong, write it via `mcp__arcreel__patch_project({"settings": {"source_language": "en"|"vi"|"zh"}})`
   - `episode_target_units` (target size per episode, interpreted as reading units according to `source_language`): if set, use it as-is; if missing and the user explicitly gave a word count in the conversation → write it via `mcp__arcreel__patch_project({"settings": {"episode_target_units": N}})`; if missing but the project has `episode_target_duration` set, the tool converts that duration into a per-episode size via the speaking rate, **so there is no need to ask the user for a word count**; if neither exists you can still plan directly (the tool sizes episodes itself for short-video pacing), no need to force a question
2. Call `mcp__arcreel__plan_episodes({})`. The window size and number of episodes per batch are determined by the tool from the text model's maximum output length and are not configurable; when this batch reports "no complete break point found", ask the user to manually cut out this section and then continue; for the way out of output truncation, see the tool description. **When the user gives additional episode-splitting instructions before planning** (such as "split strictly by chapter, one chapter per episode", "end each episode at a certain point"), pass the original instruction text via `instructions`: `mcp__arcreel__plan_episodes({"instructions": "附加指令原文"})`; the additional instructions are injected verbatim into the "Additional instructions" section of the planning prompt, and the strength of adherence is expressed by the text itself - when the user explicitly asks for strict adherence, write the strength wording into the text as well (such as "must all be carried out: one chapter per episode"). Long works are planned in multiple batches (one tool call per batch); the additional instructions are **not persisted**, so **every batch call must repeat the same `instructions`** until planning is complete
3. **Batch-level review**: show the ledger summary returned by the tool to the user and ask for feedback; it only counts as done when the opening and closing source text of every episode has been shown to the user - the user relies on it to check whether the episode boundaries are cut correctly
4. The user gives feedback (one sentence can contain any number of points, including global preferences) → go through "reset + replan": first call `mcp__arcreel__reset_episode_planning({"episode_id": X})`, where `episode_id` is the episode ID of the earliest affected episode in airing order per the feedback, keeping the episodes before it in airing order unaffected; when the earliest affected one is the first cut episode, omit `episode_id`, i.e. a full reset
5. **Consumed-episode warning confirmation**: when the reset would affect consumed episodes (that already have script_plan / screenplay / media artifacts), the tool returns the list of affected episodes without executing - inform the user of the scope of impact, and after getting explicit confirmation, call again with `"confirm_consumed": true` added; once confirmed and executed, these episodes become episodes without source text, are marked stale, and are moved to the end of the airing order, with artifacts and artifact registrations all kept
6. After the reset completes, global feedback (such as per-episode size) is first written explicitly via `mcp__arcreel__patch_project({"settings": {"episode_target_units": N}})`, then call `mcp__arcreel__plan_episodes` again with the adjusted `instructions` to plan in batches from after the kept section, and show the results for review again; replanned episodes are always assigned new episode IDs, and cleared episode IDs are never reused; the old episodes' screenplays and media artifacts are not attached to the new episodes, and the new episodes' downstream artifacts must be produced again. **After planning finishes, the return includes global check material** (cumulative episode count, the few smallest episodes by size, median size, target size - when the target size was converted from `episode_target_duration` its source is marked, and it should be treated as a soft target during the check): if the user gave structural preferences such as a total episode count or chapter alignment, you must check against them, and any deviation must be explained clearly to the user (you can guide the user to go through "reset + replan" again to fix it)
7. Once the user is satisfied with this batch's planning, refresh the plan and continue. **When the user explicitly authorizes full autonomy** (such as "just run the whole workflow without step-by-step confirmation"), you may skip batch-level review and continue directly

---

## `prepare_script_plan`: Single-episode script planning

**Trigger**: `next_action.type == "prepare_script_plan"`

Dispatch the subagent named by `next_action.args.preprocessor` to produce the corresponding script_plan
intermediate file under `drafts/episode_{N}/`. **Do not infer who to pick yourself from `generation_mode` × `content_mode`**: the server derives
`preprocessor` from the same rule table, and deriving it again on the profile side only creates a second source of truth. For the correspondence between each script_plan file and its schema, see
`.claude/references/generation-modes.md`.

Common dispatch prompt parameters: project name, project path, the target episode's episode ID (`target.episode`) and its title and airing position, this episode's novel file path (`target.source`); optional additional instructions (any temporary context that needs to go to the subagent, such as the user's requirements for this generation, passed through verbatim).

If `next_action.args` contains `expected_stale_script_plan_revision`, after the subagent successfully produces the formal script_plan it must call
`mcp__arcreel__complete_script_plan_rebuild({"episode_id": N, "expected_stale_script_plan_revision": next_action.args.expected_stale_script_plan_revision})`.
This completion fact must not be inferred from "whether the file content changed": a deterministic rebuild may produce exactly the same JSON. When the tool reports a conflict, refresh the plan;
never retry with the old parameters.

(The two script planning subagents read project.json themselves + call
`mcp__arcreel__get_video_capabilities({})`
to get model capabilities and user preferences; the main Agent does not need to pre-inject the character / scene / prop lists or
data such as `supported_durations` / `max_duration` / `max_reference_images` / `default_duration` / `episode_target_duration`.)

**This episode's new assets are produced with the plan**: asset identification is done in per-episode script planning. The subagent recognizes entities by the names, aliases, and descriptions of registered assets; characters / scenes / props in this episode that are not registered are written into the top-level `new_assets` of the script_plan, each with a handling decision (`register` register as a new asset / `merge` fold into an existing asset / `derivative` register as a character derivative / `skip` do not register) and a one-sentence rationale. When the asset table is empty, dispatch script planning as usual.

**Content changes after the review gate are made on the formal script**: the review gate converts the whole episode's script plan into the formal script `scripts/episode_{N}.json`; from then on the formal script is the single source of truth for that episode's content, and the script plan is read-only (Web-side saves, and the Agent fetching, modifying, or promoting an edit copy, all return `script_plan_confirmed`).

- **Changing content**: when the user wants to change shot narration body `novel_text`, or a reference-to-video unit's body `text` and source text `source_text`, first get the body and revision with `mcp__arcreel__get_episode_script`, then write back with a single `update` via `mcp__arcreel__patch_episode_script`, without changing the script plan or re-running authoring. Non-empty source text written must be a verbatim substring of this episode's source `source/episode_{N}.txt` (compared after whitespace normalization; you may trim the start and end, but must not delete or change anything in the middle; when this episode's source is missing, compare against the project source); when the project has source files the server validates this and rejects mismatches with `source_text_not_verbatim`; clearing the source text is not validated. If, after changing a shot's narration body, the prompts should follow, the user decides whether to explicitly rewrite those entries with `generate_episode_script` using `entry_ids` and `rewrite: true`; reference-to-video authoring rewrites the unit body, so an explicit rewrite of a unit whose body was just changed would overwrite that change - explain this to the user first
- **Redoing the whole episode**: only when the user wants to tear down the whole episode do you re-dispatch the script planning subagent (operation type "redo whole episode") to re-run script planning. After the new script plan is written, this episode's review gate returns to pending (the plan's `gates.script_plan_review.state` is `pending`), but the old formal script is still in use, so the plan does not stop at `confirm_script_plan` and `next_action` points as usual to the downstream action of the old formal script; when redoing the whole episode do not follow it, and do not call `generate_episode_script` - it would author the old formal script that is about to be overwritten; call `confirm_script_review` directly per the flow below. When the episode already has a formal script, confirming means overwriting: `confirm_script_review` returns `script_overwrite_required`, and the receipt body is a server-generated loss list (the same text as the Web confirmation dialog, including voiceover, end frames, and grid membership). First relay the list verbatim to the user, and only after getting explicit agreement re-confirm with `overwrite_revision=params.script_overwrite.revision`; after overwriting, all entries are pending authoring
- **Prompt authoring**: `generate_episode_script` fills gaps by default, and existing image / video prompts (including ones the user wrote by hand) are kept as-is; `entry_ids` only defines the scope, and only add `rewrite: true` when the user explicitly asks to overwrite certain entries, then get agreement per the loss list in the receipt and pass back the token

---

## `confirm_script_plan` / `generate_script`: JSON screenplay generation

**Trigger**:

- `next_action.type == "confirm_script_plan"` → first complete the review gate below, refresh the plan, then route
- `next_action.type == "generate_script"` → dispatch screenplay generation

**script_plan→prompt_authoring review gate (blocking)**: the script plan produced by `prepare_script_plan` must be **explicitly confirmed** before prompt authoring is allowed (this applies to all three structured script_plan variants - drama / narration / reference_video; `reference_video`'s `script_plan_reference_units.json` must also be confirmed, do not skip it. ad has no script_plan and requires no review gate). There are two equivalent confirmation paths - the user reviews / edits and confirms on the Web side, or the user clearly agrees in the conversation to move on to visual generation and you call `mcp__arcreel__confirm_script_review({"episode_id": N})` (in full-autonomy mode, confirm under the user's overall authorization). When the episode has no formal script yet, an unconfirmed plan stops at `confirm_script_plan` and does not route to prompt authoring (when a formal script already exists it does not block downstream, see "Redoing the whole episode" above); when there is no formal script yet, `generate_episode_script` directly reports "no formal script yet". Confirming converts the whole episode's script plan into the formal script, with all entries pending authoring; when the episode already has a formal script, confirming overwrites it, and you must first get the user's agreement per "Redoing the whole episode" above.

On confirmation, `new_assets` are registered together with the formal script: per the handling decision, new assets are created, aliases are recorded on existing assets, or derivatives are registered, and references are rewritten accordingly; a new item with the same name as an already-registered asset of the same type is automatically folded into that asset. Before confirming in the conversation, tell the user each new asset and its handling item by item; when the user wants to change the handling, ask them to change it in the "New assets in this episode" area of the Web review gate page, or fetch the script plan draft, change `new_assets`, and promote it. When the handling cannot be resolved (the asset to fold into or the base look of the derivative does not exist), confirmation returns `invalid_new_assets`; fix it per the receipt and re-confirm.

**Dispatch the `create-episode-script` subagent**: pass the project name, project path, the target episode's episode ID (`target.episode`) and its title and airing position; optional additional instructions (any temporary context that needs to go to the subagent, such as the user's requirements for this generation, passed through verbatim).

---

## `generate_asset_sheets`: This episode's asset sheets

**Trigger**: `next_action.type == "generate_asset_sheets"`; `next_action.args.episode_id` is the target episode's episode ID.
Assets are registered with the review gate; an empty asset bucket is a legal state, and you must not fall back because of it.

"Which assets this episode references" is computed by the server: call `generate_assets` once with the episode ID, and the server generates all assets referenced by this episode that still lack asset sheets
(characters / scenes / props / products and derivatives), the same list as the Web episode-level "Generate pending assets"; derivatives are in the same batch as their base look, and derivatives are only submitted after the base look sheet is generated successfully.

```text
dispatch `generate-assets` subagent:
  Task type: asset_sheets
  Project name: {project_name}
  Tool call:
    mcp__arcreel__generate_assets({"episode_id": <next_action.args.episode_id>})
  Verification: report per ID by the returned requested / succeeded / failed / blocked
```

After the subagent returns, per "Review checkpoint: review images" in [generate-assets](../generate-assets/SKILL.md), dispatch
`review-images` for the successfully generated assets, show the generation summary together with the review report to the user, then proceed to inter-action confirmation; assets the user wants handled are processed per that section.
For handling `blocked` items, see the `generate_asset_sheets` row in [workflow-plan](../../references/workflow-plan.md).

---

## `generate_storyboards` / `generate_grid`: Storyboard image generation

**Trigger**: `next_action.type` is `"generate_storyboards"` or `"generate_grid"`; the server never returns these two actions for
reference-to-video.

Choose the tool directly by action, without re-checking `generation_mode` or `grid_storyboard`:

- `next_action.type == "generate_storyboards"` → dispatch `generate-assets`, calling
  `mcp__arcreel__generate_storyboards({"script": target.script_filename, "segment_ids": requested_ids})`
- `next_action.type == "generate_grid"` → dispatch `generate-assets`, calling
  `mcp__arcreel__generate_grid({"script": target.script_filename})` - **do not pass `scene_ids`**:
  generate-if-missing selects exactly the groups containing `requested_ids`, while skipping grids whose grid composite is ready but not split and reusing in-flight grids;
  naming `scene_ids` treats them as a redo request and charges again

For `generate_storyboards`, pass `next_action.args` and `requested_ids` unchanged to the subagent; for `generate_grid`, pass only the screenplay file name.

After the subagent returns, per "Review checkpoint: review images" in [generate-storyboard](../generate-storyboard/SKILL.md), dispatch
`review-images` for the shots that had storyboard images written (for grids, after grid split), show the generation summary together with the review report to the user, then proceed to inter-action confirmation.

> **Grids are two-stage**: `generate_grid` only produces the grid composite; storyboard images are only written after grid split, so until the split the plan keeps
> returning `generate_grid`. `grid_ids_awaiting_split` in the result (or grids marked "grid composite ready, not split" in the summary)
> lists the unsplit grids: ask the user to review the grid composite in the grid panel (they can regenerate, upload a replacement, or roll back), and **only after the user explicitly agrees to split**,
> call `mcp__arcreel__split_grids({"grid_ids": [...]})`. Do not split on your own after generation completes; splitting overwrites all storyboard images
> covered by the grid, and the old images remain in version history and can be rolled back.

> **Redoing after switching `grid_storyboard`**: this action's normal trigger condition is "missing storyboard images", but the user switching that toggle on the settings page does not invalidate existing storyboard images, and the screenplay does not record which assembly method produced a storyboard image - looking only at missing images would judge the whole episode as complete. When the user, on a project that already has storyboard images, switches the toggle and asks for images in the new way, confirm with them the range of shots to redo, then regenerate explicitly with IDs: for switching to grid use `mcp__arcreel__generate_grid({"script": target.script_filename, "scene_ids": [...]})`, for switching back to single images use `mcp__arcreel__generate_storyboards({"script": target.script_filename, "segment_ids": [...]})` (`script` is required; omitting the ID list only fills missing images and does not achieve a redo). Already-generated videos are likewise not automatically invalidated; after re-generating storyboard images, re-run `generate_videos` for the corresponding shots based on the new images.

## `generate_videos`: Video generation

**Trigger**: `next_action.type == "generate_videos"`

Before enqueueing, the plan may first hand back `confirm_request_duration`: batch admission requires confirming the requested duration tiers. Per
`admission.confirmation.tiers[]`, show the video units and cost involved tier by tier, and after getting confirmation, carry them back via
`confirmed_request_durations` into the next `mcp__arcreel__get_workflow_plan` (see
[workflow-plan](../../references/workflow-plan.md)).

Only enqueue when `plan.steps[].admission.decision == "admitted"`; when it is `blocked` or `confirmation_required`,
**enqueue no tasks at all**. In that case report, per video unit, `admission.units[]`'s `unit_id`, `problems[].code`, the reason, and
`problems[].action`; video units held back by others carry `generation_batch_admission_withheld` - state truthfully that they are dragged down by
`blocked_unit_ids` rather than having problems of their own. After fixing the rejected video units, **redo the whole batch**; do not split the batch to run the passing half first,
or you will resubmit video units that have already been paid for.

**Dispatch the `generate-assets` subagent**: request selection semantics are exactly the same as on the Web. The plan's `requested_ids` is always an array:
non-empty means **a named forced redo (always charged)**; `[]` means the plan named nothing, and the tool call should **omit the ID parameter** to only fill gaps.
An explicit empty array as tool input is invalid; never pass the plan's `[]` to the tool unchanged. Pick one of these two by the plan value; do not try both tools:

```text
dispatch `generate-assets` subagent:
  Task type: video
  Project name: {project_name}
  Tool call:
    requested_ids non-empty →
      mcp__arcreel__generate_videos({"script": target.script_filename, "target": {"scope": "selected", "ids": requested_ids},
                                             "force": true})
    requested_ids == [] (plan named nothing; the tool call does not pass scene_ids) →
      mcp__arcreel__generate_videos({"script": target.script_filename, "target": {"scope": "episode", "episode_id": target.episode}})
  Verification: re-read target.script and check each shot's video_clip field
```

After returning, state the results itemized per ID (`succeeded` / `failed` / `blocked` / `skipped`), and **state separately** the four axes of workflow step status,
queue tasks, provider checkpoint, and artifact currency - "task succeeded" does not equal "the current artifact is valid".
Stale artifacts can still be previewed, exported, and used in the final cut as usual; whether to redo them is for the user to decide explicitly; never automatically delete, overwrite, or regenerate paid artifacts.

---

## `create_edit_timeline`: Edit

**Trigger**: `next_action.type == "create_edit_timeline"`; this episode's videos are all done and there is no edit timeline yet.

For TTS voiceover projects (`plan.status.artifacts.audio.missing_ids` non-empty), first fill in the missing voiceover per the "Batch narration audio" section. As soon as the edit timeline is created
this step is complete, and the workflow will not come back here afterward to fill in voiceover.

Then call `mcp__arcreel__create_timeline({"from": "script", "episode": target.episode, "name": "完整版"})` ("完整版" = "Full cut") to mechanically
create an edit timeline from the script; once at least one edit timeline exists, this step is complete. Afterward, edit on it per the `edit-video` skill,
and only render the final cut or export a JianYing draft when the user asks.

---

## Batch narration audio

**Trigger**: filling in missing voiceover during the first automatic edit of a TTS project, or when the user explicitly asks to generate narration audio; choosing TTS for the project is already authorization
for the first fill-in, no separate confirmation is needed, and stale voiceover is only re-synthesized at the user's request. For post-production voiceover projects that want narration audio generated, first ask the user to switch to TTS voiceover in
the project settings. It is not driven by the plan's `next_action` and does not block video generation;
`plan.status.artifacts.audio` reports missing or stale in TTS projects, and is `not_applicable` in post-production voiceover projects.

Narration audio synthesizes speech segment by segment from each segment's `novel_text` source text; it depends only on the screenplay and is independent of storyboard images / videos:
when the user asks, it can be run at any time after `generate_script` produces the screenplay.

`generation_mode == "reference_video"` **only skips storyboard images**: reference-to-video has no per-segment batch TTS
entry point (no `segments[]`); narration audio is generated video unit by video unit.

**Dispatch the `generate-assets` subagent**:

```text
dispatch `generate-assets` subagent:
  Task type: narration_audio
  Project name: {project_name}
  Tool call:
    Fill in the whole episode:
      mcp__arcreel__generate_narration_audio({"script": target.script_filename})
    Segments the user named for re-synthesis (after changing voice / speech rate):
      mcp__arcreel__generate_narration_audio({"script": target.script_filename,
                                              "segment_ids": [named segment IDs]})
  Verification: re-read target.script and check each segment's generated_assets.narration_audio field
```

**Re-synthesis must pass `segment_ids`.** Omitting the parameter means "only fill missing", and stale audio counts as reusable and is
skipped - re-synthesizing without IDs does nothing at all.

After an interruption, re-dispatching the full-episode fill-in call resumes from the breakpoint - segments that already have audio are skipped automatically, and only missing segments are filled.

---

## `repair_video_units` / `patch_episode_script`: Fix the screenplay, then redo

**Trigger**: `next_action.type` is `"repair_video_units"` or `"patch_episode_script"`.

Read `target.script` and **only handle the entries corresponding to `requested_ids`**. Get the revision according to the action:
for `patch_episode_script`, `next_action.args` already gives `base_revision` and per-entry `problems` directly,
so use them directly without querying again; for `repair_video_units` the args do not contain them, so first call
`mcp__arcreel__get_episode_script({"script": target.script_filename})` to read the body and get the revision.
Then use **a single** `mcp__arcreel__patch_episode_script({"script": target.script_filename,
"base_revision": <the revision obtained above>, "operations": [...]})` to finish all entries - one ordered `update` per entry.
Markers such as `needs_replan` are recomputed by the tool; do not write them by hand. When the tool reports a revision conflict, refresh the plan and start over; never retry with the old
revision. After the fix, name these IDs for a redo per the request selection semantics above, then refresh the plan.

---

## `wait_for_task`: A task is running

**Trigger**: `next_action.type == "wait_for_task"`.

A task is already executing in the queue or on the provider side. **Do not enqueue any new tasks**; tell the user truthfully the `task_id`, `status`,
and `provider_checkpoint` from `steps[].tasks[]`, wait, then call `mcp__arcreel__get_workflow_plan` again to re-check.
`provider_checkpoint.submitted == true` means it has been submitted on the provider side and has very likely been charged; resubmitting now means paying twice.

---

## Flexible entry

The workflow **does not force starting from the beginning**. Based on the plan result, it automatically starts from the correct action:

- "continue" → execute the plan's `next_action`; when `next_alternatives` is non-empty it is a fork - list the options for the user to choose
- Naming a specific operation (such as "analyze the novel's characters", "plan the script for episode 2", "generate storyboard images") → check the plan first; if the operation is `admitted` in `plan.status.operations`
  (or not listed), execute it, even if `next_action` points elsewhere; when it is `refused`, relay the `reason`
  to the user, explaining what is missing and how to fill it in. When there are `blockers`, never execute
- When the user says "episode N" it means the Nth in airing order: take that episode's episode ID by the order of `episodes[]`, then query its plan

---

## Data layering

- Full definitions of characters / scenes / props are **stored only in project.json**; the screenplay only references names
- Derived fields of the project summary `episodes[]` (item_count, status, progress) are **computed on read**, not stored
- Episode metadata (episode ID / title / script_file) is **synced on write** when the screenplay is saved
