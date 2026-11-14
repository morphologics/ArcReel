---
name: generate-video
description: Generate videos for shots or self-contained video units. Use when the user asks to generate or redo videos; supports a whole episode, a single item, and a hand-picked batch.
---

# Generate Videos

## Routing

Let the MCP tool read `project.json`, dispatch by `generation_mode` × `content_mode`, and validate the script skeleton:

| Generation mode × content mode | Expected skeleton | Dispatch | Output directory |
|---|---|---|---|
| `reference_video` × narration / drama / ad | `video_units[]` | `task_type="reference_video"` → `execute_reference_video_task` | `reference_videos/{unit_id}.mp4` |
| `storyboard` × narration | `segments[]` | `task_type="video"` → `execute_video_task` | `videos/scene_{segment_id}.mp4` |
| `storyboard` × drama | `scenes[]` | Same as above | `videos/scene_{scene_id}.mp4` |
| `storyboard` × ad | `shots[]` | Same as above | `videos/scene_{shot_id}.mp4` |

On a skeleton mismatch, stop enqueuing and regenerate the script for the project's generation mode. Reference-to-video consumes the self-contained `video_units[]` directly and skips storyboard images.

### Reference-to-video

Treat each `video_units[]` entry as one independent generation call:

- Build a unified reference-syntax prompt from the video unit's body text (`text`).
- Reference images are resolved at execution time from the body's `@[名称]` ("name") in order of first mention, with no special ordering; use the asset sheet if there is one, otherwise all of that asset's original images.
- Let the generation preflight project the video unit's planned duration onto the provider's requestable duration tiers.
- On `needs_replan` or a speaker-attribution problem, stop that video unit and fix the planned content first.
- Whole-episode generation only reuses the current clip that `generated_assets.video_clip` explicitly points to; an orphan file with the same name does not mean that video unit is done.

Let the project configuration, script model and video capabilities decide aspect ratio, duration and reference-image limit; do not write a separate set of values in the call parameters.

## Tool calls

Enqueue with MCP tools; this skill provides no Python or Shell generation scripts.

| Operation | Tool |
|------|------|
| Whole-episode generation (default operation) | `mcp__arcreel__generate_videos({"script": "episode_1.json", "target": {"scope": "episode", "episode_id": 1}})` |
| Single shot | `mcp__arcreel__generate_videos({"script": "episode_1.json", "target": {"scope": "scene", "ids": ["E1S01"]}})` |
| Hand-picked batch | `mcp__arcreel__generate_videos({"script": "episode_1.json", "target": {"scope": "selected", "ids": ["E1S01", "E1S05", "E1S10"]}})` |
| All pending | `mcp__arcreel__generate_videos({"script": "episode_1.json", "target": {"scope": "all"}})` |

Interpret `target.ids` as shot IDs in storyboard-to-video and as `unit_id` in reference-to-video. For whole-episode generation, `target.episode_id` is the episode ID of the episode the script belongs to (the same number as in the file name `episode_{episode ID}.json`, taken from the plan's `target.episode`), not the episode's ordinal position.

### Regenerating named video units

In reference-to-video, pass `video_units[].unit_id`:

| Operation | Tool |
|------|------|
| Regenerate a single video unit | `mcp__arcreel__generate_videos({"script": "episode_1.json", "target": {"scope": "scene", "ids": ["E1U2"]}, "force": true})` |
| Regenerate several video units | `mcp__arcreel__generate_videos({"script": "episode_1.json", "target": {"scope": "selected", "ids": ["E1U2", "E1U3"]}, "force": true})` |

One call enqueues and returns a durable batch; call `get_generation_batch` per the returned `poll_after_seconds` until `done: true`, then process the results:

- Treat naming as a forced redo that overwrites the existing clip.
- If a task is already in flight, do not automatically force a redo; wait for and read its batch result, to avoid paying again for a target that just completed.
- Only generate the self-contained video units named in the script; IDs that do not match are recorded as `blocked` with `generation_unit_not_found`.
- After an interrupted call, query the durable batch; resend only the unsuccessful IDs using `selected` and `force: false`; completed items go to `skipped`.
- Results are returned per ID under `requested / succeeded / failed / blocked`;
  see `.claude/references/generation-results.md` for the structure and problem codes.

### Switching the current version

A video unit's current version decides which version is used for preview, edit and export. When picking a better version, call
`mcp__arcreel__select_video_version({"unit_id": "E1S01", "version": 2})`; it takes effect immediately, needs no user confirmation, and is free:

- After the first review round, another candidate version of a unit is better: switch to it.
- On acceptance after regeneration, the new version is worse than the old one: switch back to the old version.
- If the version number does not exist, re-pick from the returned `params.available_versions`.

### Video and narration

Video requests look only at the script: always request duration tiers by the video unit's planned duration; admission, quoting and resume are all independent of the project's narration delivery method,
and projects without TTS configured generate and resume videos as usual. Narration audio is generated separately in the edit stage (`generate-narration-audio`).
`generate_videos` has no `narration_delivery` parameter; including it will be rejected.

### Batch admission and duration-tier confirmation

A video batch request is **all or nothing**: when admission is `admitted` the whole batch is enqueued; when `blocked` or `confirmation_required`,
**not a single task is enqueued**. Web and Agent share the same admission and the same request-selection semantics; there is no Agent-only lenient lane.

Reference-to-video picks the effective tier from the video unit's reference state, projecting the planned duration onto a requestable tier the model supports. When the requested tier differs from the planned duration,
the preflight returns `reference_duration_confirmation_required`; explain to the user tier by tier the video units involved, the planned seconds, the requested seconds
and whether it gets longer/shorter; after confirmation, via `confirmed_request_durations` (tiers keyed by unit_id), **resend the original target set as one batch**:

```text
mcp__arcreel__generate_videos({"script": "episode_1.json", "target": {"scope": "episode", "episode_id": 1},
                               "confirmed_request_durations": {"E1U1": 8}})
```

When the cost should be confirmed by the user before submitting (e.g. the checklist in `edit-video`), preflight with `"preview": true`:
the same admission, nothing enqueued, returning per-video-unit estimated cost and tier changes. When formally submitting after user confirmation, include the
`confirmed_request_durations` given by the preflight unchanged, and no tier confirmation will be requested again.

When rejected, report per video unit the `unit_id`, `problem.code`, reason and `problem.action`; video units that passed carry
`generation_batch_admission_withheld`, whose `blocked_unit_ids` point to what blocked them; explain this causality truthfully.
**Do not split the batch to run the passing half first** — that both bypasses all-or-nothing and resubmits video units already paid for.
When capabilities cannot be resolved, treat the tool error as a blocker and fix the model capability declaration first.

### How to read and report results

`task_state` (queue task), `provider_checkpoint` (whether submitted to the provider), `artifact_status` (artifact
current / stale / missing / blocked) and the workflow step status are independent of each other; **state them separately**: "task succeeded" does not mean
"current artifact is valid". `provider_checkpoint.submitted` being true means the provider has very likely billed; a task being
`interrupted` means there is no provider verdict; always decide by `problem.action`; that case usually hands back
`wait_for_task` (the task may still be running and land normally); do not change it to `retry` on your own.

Stale artifacts can still be previewed, exported and used in the final cut; the server reuses them and does not regenerate automatically; whether to redo is the user's explicit decision.
Never automatically delete, overwrite or regenerate any paid artifact or historical version.

## Workflow

1. Load the project and script, confirm the skeleton matches the generation mode.
2. In storyboard-to-video confirm storyboard images are available; in reference-to-video confirm the video unit body is non-empty and the planned duration valid.
3. Call the MCP tool to enqueue; handle admission rejections and tier confirmations.
4. Show the results; per the user's choice, redo the unsatisfactory shots or video units by name.
5. Use the `generated_assets.video_clip` written back by the tool as clip ownership.

## Prompt construction

Let the MCP tool build the prompt per generation mode:

- Storyboard-to-video reads `image_prompt`, `video_prompt` and the storyboard image.
- Reference-to-video reads the video unit body (`text`) and planned duration.
- Narration storyboard-to-video does not put `novel_text` into the video prompt; narration is handled by a separate audio flow.
- The audio switch, character speaker attribution and negative-prompt rules are applied automatically.

## Pre-generation checks

Check by the project's generation mode:

- storyboard: every target shot has a usable storyboard image, and the action and spoken content are executable.
- Reference-to-video: every target video unit has non-empty body text, a valid planned duration, a single speaker attribution, and is not marked `needs_replan`.
- reference: reference images are resolved server-side at execution time from the body's `@[名称]` in order of first mention; unregistered mentions only produce warnings and do not block enqueuing; let the server trim to `max_reference_images`.
- reference: the output path is `reference_videos/{unit_id}.mp4`.
