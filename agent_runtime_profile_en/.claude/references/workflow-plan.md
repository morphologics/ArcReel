# Workflow plan contract

`mcp__arcreel__get_workflow_plan` is the **single authoritative entry point** for orchestration. It returns a read-only plan: the state of the content,
whether each AI operation can run right now (admission), ordered steps, active tasks, the video whole-batch admission verdict, and the suggested next step.

**Do not build a separate step table in the profile broken down by content mode or generation mode.** Across the six mode combinations (narration /
drama / ad × storyboard / reference_video), which steps apply, their order and where things currently stand are all expressed by
the plan's `steps[]`; the Agent is only responsible for executing the controlled actions the plan hands back.

## Query

```text
mcp__arcreel__get_workflow_plan({
  "episode_id": X,                               // optional: pass the episode ID when the user names a specific episode
  "confirmed_request_durations": {"E1U1": 8}    // optional: per-video-unit requested duration options the user has confirmed (keys are unit IDs)
})
```

Both fields belong only to **this one query**; the server does not persist them. So every re-query must carry the choices that still hold,
unchanged; leaving one out is the same as withdrawing it. Narration delivery is a project setting; the plan reads it directly from the project, and the query has no such field.

When to call: on entering the workflow, when the user says "continue / next / show progress", and **after every tool or subagent completes**.
`Read` / `Glob` are only for fetching content needed to execute an already chosen action, not for building a second state machine. You MUST NOT override the server's conclusions based on empty asset buckets,
file names, the existence of old files or conversation memory.

## Naming an operation vs continuing

Admission belongs to the AI operation, not to a position in the pipeline: whether an operation can run depends only on whether its own inputs hold, regardless of whether the suggested next step
is that operation. Accordingly there are two kinds of request:

- **The user names an operation** (e.g. "generate the script plan for episode 2", "author the prompts"): query the plan and look at
  `plan.status.operations[operation]`. If `state == "admitted"`, execute it, even if `next_action` points elsewhere;
  if `refused`, relay the `reason` to the user (what is missing and how to supply it) and do not execute; if `not_applicable`, explain that this project
  has no such operation. `operations` only states the listed operations; admission of generation operations stays with each entry point's own verdict
  (ADR 0073); when refused, relay the reason the entry point returns.
  Tool entry points use the same verdict: when refused they return `operation_not_admitted`, with `params.reason` equal to the reason code in the plan.
- **The user says "continue / next"**: execute `next_action`. A non-empty `next_alternatives` means this is a **fork**
  (e.g. with no episode source, "write the script by hand" and "add the episode source, then AI planning" side by side): present `next_action` and each alternative to the user,
  let the user choose and then execute; do not choose for the user.

## Reading the plan

| Field | Meaning |
|---|---|
| `steps[]` | Ordered steps. `id` is the stable step name, `state` ∈ `completed` / `ready` / `active` / `blocked` / `pending` / `skipped`; `required=false` means the step does not apply under this project's mode combination |
| `steps[].action` | The step's own controlled action (may be null) |
| `steps[].artifacts` | Snapshot of the step's artifact currency: three ID buckets `current_ids` / `stale_ids` / `missing_ids`, plus a set-level `state` (`current` / `stale` / `partial` / `missing` / `blocked` / `not_applicable`). `blocked` is a set-level state; there is **no** per-ID blocked bucket |
| `steps[].tasks[]` | The step's queued and running tasks, each with `unit_id`, `task_id`, `status` and `provider_checkpoint` (only `submitted`). In-progress tasks are progress, not problems |
| `steps[].admission` | Whole-batch admission verdict for the video step (see below) |
| `steps[].problems[]` | The step's individual problems, with stable problem code `code`, closed-set `action`, owning unit `unit_id` and message params `params`; each appears only once, under the step it belongs to. Per-unit problems of the video whole-batch admission appear only in `steps[].admission.units[]` |
| `problems[]` | Project-level problems that belong to no step (currently only data upgrade failure); does not repeat problems from the steps |
| `blockers[]` | The project as a whole is unusable (data upgrade failed, project.json or the artifact manifest unreadable, invalid content mode or generation mode); contains `code` / `path` / `reason` |
| `status.issues[]` | Data problems in the content itself (script unreadable, invalid field, corrupt asset definition, etc.), also with `code` / `path` / `reason`; only blocks operations that depend on that content |
| `status.content` | State of the content: episode count, whether the whole source and episode source exist, the target episode's draft, the final script (`present` / `absent` / `invalid`) and its entry count, entries pending authoring, video units needing replan, assets that are referenced but have no sheet; `episode_plan_stale` true means that episode's episode plan is stale and the script plan needs rebuilding |
| `status.operations` | Structural admission of the listed AI operations; keys are action names, values are `state` (`admitted` / `refused` / `not_applicable`) and a stable reason code `reason` (see "Naming an operation vs continuing") |
| `next_action` | The suggested next step. When the user says "continue", route by it; do not pick an earlier action from `steps[]` yourself and jump ahead |
| `next_alternatives[]` | Alternatives alongside `next_action` at a fork; when non-empty the user chooses |

When `blockers` is non-empty: show the blockers to the user and **stop all changes**. `next_action.type == "none"` with step
`edit` at `completed` in `steps` means the workflow is finished: every episode in the queried scope has at least one edit timeline. In the other cases where
`next_action.type == "none"` and `blockers` is empty, explain `status.issues` and `status.content` to the user
(e.g. that episode's script is unreadable, the episode plan is stale and needs rebuilding, the edit timeline is unreadable); operations the user names that are admitted can still run as usual.

## Data upgrade failure: fix → retry

When the project's data upgrade (including artifact backfill) did not finish, the whole project is blocked. In that state the plan hands back only one thing:

- `blockers[]` contains only one entry with `code == "project_migration_failed"`; `reason` is the verbatim upgrade failure;
- `problems[]` contains only one problem with the same code, `action == "retry_project_migration"`;
  `params.details[]` gives `episode` (episode ID) / `file` / `violation` per entry — which episode, which file, what constraint was violated;
- `next_action.type == "retry_project_migration"`, with `args.details` as above.

In this state all generation tools and final write tools return that same problem and do nothing, and nothing is charged. The project itself
is still readable: `Read` scripts, viewing existing images and videos on the canvas work as usual.

Order of handling:

1. Walk the user through `details[]` entry by entry: which file of which episode (translated to title and episode position via the project details' `episodes[]`) violated which constraint;
   do not collapse it into "the upgrade failed".
2. The only write tools still usable while blocked are `mcp__arcreel__patch_project`, `mcp__arcreel__patch_episode_meta`,
   `mcp__arcreel__rename_asset`, `mcp__arcreel__merge_asset`; `mcp__arcreel__patch_episode_script` and all generation
   tools are refused. First fix what these tools can fix according to the details; for what they cannot reach (e.g. violations in the script body) tell the user truthfully per step 4.
   **There is no raw file write path**, and do not use `Edit` to change the final script directly either.
3. Call `mcp__arcreel__retry_project_migration` to rerun the upgrade chain. It is idempotent; repeated calls do no harm.
4. On success the tool returns a new workflow plan, the project is unblocked, and you continue by `next_action` as usual; on failure it returns new structured
   details — go back to step 1. When you cannot fix it, tell the user truthfully where it is stuck; do not keep retrying to no effect.

When the user clicks "Retry migration" on the Web project page, the request text is filled into the chat input for the user to send themselves — when you receive it, take the path
above.

## Controlled action table

Route by `next_action.type`, carrying `target.episode`, `next_action.args` and `requested_ids` into the corresponding action.
The plan model always serialises `requested_ids`: a non-empty array means explicitly named; `[]` means the plan named nothing. When mapping to a tool's optional
ID parameter, pass the former; for the latter you MUST **omit the parameter** and MUST NOT pass `[]` through to the tool (an explicit empty array is invalid as tool input).
`plan.status.target` provides `episode` (episode ID of the target episode), `script`, `script_filename`, `source`. A tool's
`episode_id` parameter always comes from `next_action.args.episode_id` or `target.episode`. The two script fields are not interchangeable:
`script` is the script path relative to the project root (`scripts/episode_{集 ID}.json`, `{集 ID}` = episode ID), used when reading script content with Read;
`script_filename` is the bare file name with the `scripts/` prefix stripped, used for the `script` parameter of all `mcp__arcreel__*` tools.

| `next_action.type` | Execution entry point |
|---|---|
| `collect_project_input` | Guide the user to complete the project inputs on the Web (upload the whole source; for ad, fill in the brief or register products) |
| `create_episode` | Guide the user to create a new episode on the Web |
| `draft_selling_points` | When the user asks, draft selling points and write them back via `mcp__arcreel__patch_project` (ad) |
| `reset_episode_planning` | `mcp__arcreel__reset_episode_planning`, passing `next_action.args` unchanged (no `episode_id` means a full reset) |
| `plan_episodes` | `mcp__arcreel__plan_episodes` |
| `resolve_draft` | The target episode has a draft (`args.draft_kind`): when `args.needs_repair` is true it is an invalid draft left by a failed write — explain to the user and rerun its corresponding operation; when false it is the Agent's editable draft — finish it with the corresponding skill |
| `prepare_script_plan` | dispatch the subagent named by `next_action.args.preprocessor` |
| `start_blank_script` | Guide the user to click "Start from blank" on the Web episode page and hand-write this episode's final script entry by entry on the timeline |
| `provide_episode_source` | Guide the user to add the episode source for this episode (`source/episode_{集 ID}.txt`); after that AI script planning is available |
| `confirm_script_plan` | `mcp__arcreel__confirm_script_review` |
| `generate_script` | dispatch the `create-episode-script` subagent (for ad, call `mcp__arcreel__generate_episode_script` directly) |
| `add_script_items` | The final script is empty: guide the user to add entries on the Web, or insert them via `mcp__arcreel__patch_episode_script` |
| `author_prompts` | The final script has entries pending authoring, listed in `requested_ids`: call `mcp__arcreel__generate_episode_script` without `entry_ids`, which authors all pending entries (see the generate-script skill) |
| `generate_asset_sheets` | dispatch the `generate-assets` subagent, calling `mcp__arcreel__generate_assets({"episode_id": <args.episode_id>})` once to generate the assets this episode references that still lack a sheet. For assets recorded as `blocked` for a missing description (`fix_input`): write the description from the source text, add it via `mcp__arcreel__patch_project`, then rerun the same call; when the source gives no basis, confirm the description with the user, or ask the user to upload a sheet on the Web. When a derivative reports `generation_dependency_failed` / `derivative_owner_sheet_missing`, resolve the base look's sheet first |
| `generate_storyboards` | dispatch the `generate-assets` subagent, calling `mcp__arcreel__generate_storyboards` with `segment_ids` |
| `generate_grid` | dispatch the `generate-assets` subagent, calling `mcp__arcreel__generate_grid` without `scene_ids` (generate what is missing); once the grid composite is ready and the user has reviewed and approved it, call `mcp__arcreel__split_grids` |
| `repair_video_units` | `mcp__arcreel__get_episode_script` + `mcp__arcreel__patch_episode_script` to fix everything in one go, then name them for a redo |
| `patch_episode_script` | Plan-injected: `next_action.args` already gives `base_revision` and the individual `problems`; fix them all in one batch |
| `confirm_request_duration` | Plan-injected: see "Whole-batch admission verdict" |
| `generate_videos` | Video generation tool (see the `generate-video` skill) |
| `wait_for_task` | Plan-injected: there are active tasks; do not enqueue new ones; wait and re-check the plan |
| `create_edit_timeline` | This episode's videos are complete and there is no edit timeline yet: `mcp__arcreel__create_timeline` (`episode` from `next_action.args.episode_id`, `from: "script"`) mechanically creates one from the script; with at least one edit timeline, the "edit" step is complete; then edit per the `edit-video` skill |
| `retry_project_migration` | The project data upgrade is unfinished: fix per the details, then `mcp__arcreel__retry_project_migration` (see "Data upgrade failure") |
| `none` | When `blockers` is non-empty, show them and stop changes; when `edit` in `steps` is `completed`, the workflow is finished; otherwise explain `status.issues` and `status.content` |

`next_action.args.preprocessor` is the authoritative script plan subagent name; **do not derive it yourself from content mode ×
`generation_mode`**: the server derives it from a single rule table, and deriving it again on the profile side only creates a second source of truth.

### Per-problem actions handed back when the whole batch is refused

When the video whole-batch admission verdict is a refusal, the plan hands back **the first problem's `action`** directly as `next_action.type`,
with the full admission conclusion in `next_action.args.admission`. So besides the table above you may also receive the actions below — they come from the same closed set as
`problems[].action`; handle each video unit by reading its own `problems[].action`, do not guess from
`code`:

| `next_action.type` | Execution entry point |
|---|---|
| `fix_input` | The script/declaration itself is invalid: locate it via `admission.units[].problems[].detail`, correct it via `mcp__arcreel__patch_episode_script`, then re-query |
| `replan_unit` | The video unit needs replanning: use the approach in the `repair_video_units` row |
| `generate_dependency` | An upstream artifact is missing (reference images such as sheets): fill the dependency first, then re-query |
| `configure_provider` | The current provider or duration option does not support this request: tell the user which setting to change; **retrying the same request will only be refused the same way** |
| `repair_artifact_state` | Artifact status is unreadable: report it as a separate gap; NEVER treat it as missing and regenerate |
| `retry` | The same request can safely be resent |
| `retry_artifact_download` | The artifact was generated on the provider side but not fetched: call `POST /tasks/{id}/retry-download` to resume fetching; **do not resend the generation request** — that would create another paid task |

For `retry` and `configure_provider`, explain the reason for the action to the user before enqueueing a new batch;
for any action that incurs new cost, get the user's explicit consent before executing.

## Narration delivery

Narration delivery (`post_production` post-production voiceover / `use_tts` TTS voiceover) is a project setting, changed in project settings; the workflow
has no delivery choice step. Video requests are unaffected by it: under both options the video's requested duration options, admission and cost are exactly the same.

Narration audio is not a prerequisite for video generation. `plan.status.artifacts.audio` only reports, in TTS voiced projects, which narration
audio is missing or stale; in post-production voiceover projects it is `not_applicable`. TTS projects fill missing narration audio during the first automatic edit pass;
choosing TTS for the project is already the authorisation, no separate confirmation needed; the user can also ask at any time to generate it with `generate-narration-audio`.
Stale narration audio is regenerated only when the user asks for re-synthesis.

## Whole-batch admission verdict

A video whole-batch request is **all or nothing**: when `steps[].admission.decision` is `admitted` the whole batch is enqueued; when it is
`blocked` or `confirmation_required`, **not a single task is enqueued**. Web and Agent go through the same admission and the same
request selection semantics (naming a video needs `force: true` as well to force a redo / passing nothing means fill-missing only / empty array is invalid); there is no lenient Agent-only lane.

When `decision != "admitted"`:

- Report `admission.units[]` per video unit: `unit_id`, whether `admitted`, `problems[].code`,
  `problems[].action` (next action). Video units that passed carry `generation_batch_admission_withheld`,
  whose `blocked_unit_ids` says who blocked them — explain this causality to the user truthfully; do not report it as a problem of their own.
- When `decision == "confirmation_required"`, `admission.confirmation.tiers[]` gives the video units and costs grouped by requested duration option.
  After the user confirms, put the confirmed options into `confirmed_request_durations` and re-query the plan; when resending `generate_videos`, carry the same `confirmed_request_durations`.
- **Do not split the whole batch into smaller batches to "run the passing half first".** That both bypasses all-or-nothing and, once the rest is filled, resubmits
  video units that have already been paid for. Fix the refused video units and redo the whole batch.

## Report the four status axes separately

In the plan these four axes are the fields `steps[].state` (step progress), `steps[].tasks[].status` (queue task),
`steps[].tasks[].provider_checkpoint` (whether the provider has submitted) and `steps[].artifacts`
(`current_ids` / `stale_ids` / `missing_ids` and the set-level `state`). The four axes are independent; state them separately,
do not translate one into another — for how to read them and what each axis means see
[generation-results.md](generation-results.md).

## Stale and history

- **Stale artifacts can still be previewed, exported and used in the final cut as usual**; the server reuses them and does not regenerate them automatically.
- Whether to redo is **the user's explicit decision**. The Agent MUST NOT automatically delete, overwrite or regenerate any paid artifact, nor name something for a redo
  just because it "looks old" — a video must be named and passed with `force: true` to force a redo, and that always incurs cost.
- Units whose artifact status is unreadable (`blocked`) are reported as a separate gap and are NEVER treated as missing and regenerated: that would turn one corruption
  into one duplicate charge.
- Resuming an interrupted task is done by the server picking up the original request; requests already settled on the provider side are not resubmitted.

For the per-ID result structure, selection semantics and the problem code list see [generation-results.md](generation-results.md).
