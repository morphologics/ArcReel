---
name: create-episode-script
description: "Single-episode JSON script generation subagent. Use when: (1) after content review the final script scripts/episode_N.json exists and prompts need to be authored for entries pending authoring, (2) the user asks to generate the JSON script of an episode, (3) video-workflow orchestration enters the JSON script generation stage. Receives the project name and the episode ID of the target episode, calls the mcp__arcreel__generate_episode_script tool to generate the JSON, validates the output, and returns a summary of the result."
skills:
  - generate-script
---

Your task is to call the `mcp__arcreel__generate_episode_script` tool to generate the final script in JSON format.

## Task Definition

**Input**: the main Agent provides in the prompt:
- the project name (e.g. `my_project`)
- the episode ID of the target episode (written N below, e.g. `7`; taken from the plan's `target.episode`; it is an internal identifier, not the episode's number in the order)
- the title and episode-order position of the target episode (only for the returned summary)

**Output**: after generating `scripts/episode_{N}.json`, return a summary of the result

## Core Principles

1. **Call the tool directly**: call `mcp__arcreel__generate_episode_script` as instructed by the generate-script skill
2. **Validate the output**: confirm the JSON file was generated and is well-formed
3. **Return when done**: complete all the work on your own and return; do not wait for user confirmation

## Workflow

### Step 1: Confirm prerequisites

Use the Read tool to read `project.json` (relative to the session cwd) and confirm:
- the content_mode field (narration or drama)
- the generation_mode field (determined solely at project top level, cannot be changed after creation, no per-episode override)
- characters, scenes, props already have data

Use the Glob tool to confirm the final script `scripts/episode_{N}.json` exists. This step reads only the final script, not the script plan: content review converts the whole episode's script plan into the final script, with every entry flagged pending authoring. If the final script does not exist, this episode has not yet passed content review; report back to the main Agent per "No final script yet" in Step 2; if the script plan itself is also missing, name the script plan subagent that must run first (reference_video → `split-reference-video-units`; storyboard + narration → `split-narration-segments`; storyboard + drama → `normalize-drama-script`).

> Reference-to-video is also two-stage: what the finalized script_plan holds is the content contract (video unit boundaries / durations / dialogue lines / core asset identification); `generate_episode_script` only does prompt authoring — the tool mechanically preserves the structure of the video unit count, video unit durations and dialogue-line canonical lines, and if the model changes any of them the whole output is rejected.
>
> drama is two-stage (see ADR 0041): script_plan finalizes the content (shot boundaries / appearing assets / verbatim spoken utterances / source anchor source_text / visual adaptation description); `generate_episode_script` only generates the visual layer (image_prompt / video_prompt); its input is the finalized content fields in the final script, and it does not re-identify spoken lines.

### Step 2: Call the tool to generate the JSON script

```text
mcp__arcreel__generate_episode_script({"episode_id": N, "instructions": "<the instructions verbatim; optional, omit if none>"})
```

Wait for the return. When it returns `is_error: true`, read the error message and try to fix it or report the problem.

If the error is **draft awaiting disposition**, call `open_draft` with the `doc_type` from the error report to get the full `content`, `violations` and `revision`. Keep the edits already in the draft; if the main Agent passed in user feedback this round, apply that feedback first; when `violations[]` is non-empty, fix per the report on top of those edits. After fixing, call `patch_draft` with the same `episode_id` / `doc_type`, using the `revision` returned by `open_draft` as `base_revision`, then call `promote_draft` with the new `revision` returned by `patch_draft` as `base_revision`. If a violation report is returned, keep going open → patch → promote, with no round limit. Do not operate on the draft file directly with Read/Edit, and do not re-run the generation tool to re-roll.

If the error is **no final script yet** (the script plan of this episode for drama / narration / reference_video has not passed content review; only confirmation produces the final script; ad has no script plan and never hits this error), this is not a data error: do not retry repeatedly, and do not rewrite the script plan. Confirmation must be user-driven — report back to the main Agent, which calls `mcp__arcreel__confirm_script_review({"episode_id": N})` after the user reviews and confirms in the Web UI or explicitly agrees in conversation; retry this step after confirmation.

### Step 3: Validate the result

Use the Read tool to read the generated `scripts/episode_{N}.json`,
and confirm:
- the file exists and is valid JSON
- it contains the episode (equal to episode ID N) and content_mode fields
- reference-to-video: the video_units array is not empty
- storyboard + narration: the segments array is not empty
- storyboard + drama: the scenes array is not empty

### Step 4: Return a summary

```
## JSON script generation complete

**Status**: DONE
**Project**: {project name}  **Episode**: "{title}" (episode {episode-order position}, episode ID N)

| Metric | Value |
|--------|------|
| Content mode | narration or drama |
| Total shots | XX |
| Total duration | X min X s |
| Generation model | {model name actually used in the script output} |

**File saved**: `scripts/episode_{N}.json`

✅ Data validation passed

Next: the main Agent can go on to dispatch asset generation subagents (character sheets, storyboard images, etc.).
```

When `generation_mode == reference_video`, write the metric row as `| Total video units | XX |`; for other generation methods write `| Total shots | XX |`. The summary outputs only the one row that applies to the current generation method.

If generation fails:
```
## JSON script generation failed

**Status**: {PARTIAL / BLOCKED}

**Error**: {error description}

**Suggestions**:
- {fix suggestions based on the error type}
```
