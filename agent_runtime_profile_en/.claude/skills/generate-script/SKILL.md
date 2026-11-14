---
name: generate-script
description: Prompt authoring - calls the project's configured text model to fill in image_prompt and video_prompt for the shots / video units in the script that are pending authoring (for reference-to-video, rewrites the unit body text); for ad projects without a script yet, generates the whole thing. Called by the create-episode-script subagent; the inputs are the script's own content and project.json.
user-invocable: false
---

# generate-script

Calls the project's configured text generation model (Gemini / Ark / OpenAI / custom provider, decided by project.json)
to fill in the visual layer for the entries pending authoring in the script. The `image_prompt` / `video_prompt` in the script
are the "seeds" of later image / video generation; **prompt quality largely determines visual quality** — so this skill is
the link in ArcReel's whole pipeline most worth optimizing.

## Prerequisites

1. `project.json` exists in the project directory (with style / overview / characters / scenes / props)
2. **The script `scripts/episode_N.json` already exists** (N is the target episode's episode ID, taken from the plan's `target.episode`, not the episode's ordinal position) (drama / narration / reference_video): the review gate converts the whole episode's script plan into the script, with all entries carrying the pending-authoring marker. There are two equivalent confirmation paths: the user clicks confirm in the Web UI, or after the user explicitly agrees in the conversation the main Agent calls `mcp__arcreel__confirm_script_review({"episode_id": N})`. This tool reads only the script, not the script plan: a missing script plan, or one rerun but not yet confirmed, does not affect authoring.
   - **ad (ad/short film)**: when there is no script yet, this tool generates the whole thing from `project.json`'s `brief` + `products` (including selling_points) + `target_duration` (the backend builds the prompt from the approved eight-section sales-video framework allocation table; with empty `products` it automatically routes to a generic short film); at least one of `brief` and `products` is required. The result becomes the script directly, without the review gate; new characters / scenes / props in references are listed by the model in `new_assets` with a handling decision, and are registered as pending assets along with the script; ones with the same name as an already-registered asset of the same kind are merged automatically, and the receipt lists the assets newly registered this time. When a script already exists, it only authors pending entries like the other routes; for a whole redo see below.
3. **Output that fails constraints is kept as an invalid draft, not discarded and re-rolled**: when reference-to-video prompt-authoring output violates content constraints, the official file is not written, and the output together with a per-item violation report lands in `*.invalid.json`. Use `open_draft` to read the draft and revision, fix the full `content` per `violations[]`, then submit with `patch_draft`; then call `promote_draft` with the same `episode_id` and `doc_type: reference_prompt_authoring`; if it still violates, keep going open → patch → promote, with no round limit.

## Usage

Call via MCP tools (the project name is bound by the session; no need to pass it):

```text
mcp__arcreel__generate_episode_script({"episode_id": N})
mcp__arcreel__generate_episode_script({"episode_id": N, "instructions": "<附加指令原文，可选，无则省略>"})
mcp__arcreel__generate_episode_script({"episode_id": N, "dry_run": true})   # preview the prompt only
```

The output path is fixed inside the tool as `{project}/scripts/episode_{N}.json`; customization is not supported;
to rename or archive, do it in the Web UI.

### Filling in prompts (`author_prompts`)

Entries in the script carrying the pending-authoring marker (all entries converted at the review gate, manually added shots / units) do not yet have a visual layer.
When the workflow plan's `next_action.type` is `author_prompts`, `requested_ids` are exactly these pending entries.
Calling without `entry_ids` authors all pending entries, and the marker is cleared after write-back:

```text
mcp__arcreel__generate_episode_script({"episode_id": N})
```

- **Default fills gaps**: the image prompt and the video prompt are each judged as a whole; existing ones are kept and only the missing one is filled; reference-to-video expands the unit body
  per the pending-authoring marker. Entries already authored (including ones handwritten by the user) are left as they are.
- **`entry_ids` only sets the scope**: named entries still just fill gaps; those with a complete visual layer are skipped, and the receipt lists skipped items.
- **`rewrite: true` is an explicit rewrite**: re-authors the entire visual layer of entries in scope; pass it only when the user explicitly asks to overwrite these entries; content fields
  (dialogue, narration text, source text), notes and generated artifacts are preserved as usual. When it would overwrite existing content, the tool first returns
  `prompt_overwrite_required`; the receipt body is a server-generated loss list, and `params.prompt_overwrite.revision` is the approval token:
  relay the list verbatim to the user, and only after their consent call again with that revision as `overwrite_revision`.
- When there are no entries to author, the tool does not call the model or change the script, and the receipt says so. A whole-episode redo goes through rerunning the script plan and re-confirming.
- Reference-to-video re-checks the body text and dialogue volume of pending units against the script before calling the model. If that fails, the task fails with problem code
  `prompt_authoring_dialogue_overload` (dialogue can't be spoken in time) or `prompt_authoring_unit_invalid`, with `action` `fix_input`;
  `params.position` is that unit's position in this episode, which the UI calls 「视频单元 N」 ("video unit N"). Use this name when relaying the reason to the user, and ask the user to change that unit's body text or duration
  on the timeline; do not retry as-is.

### Whole ad/short film redo (`regenerate`)

When the user asks to regenerate the whole ad script, pass `regenerate: true`; no need to delete the script first:

```text
mcp__arcreel__generate_episode_script({"episode_id": N, "regenerate": true, "instructions": "<可选>"})
```

- When a script already exists, the tool first returns `script_overwrite_required`; the receipt body is a loss list (the entries to be removed and their storyboard images, videos, etc.),
  and `params.script_overwrite.revision` is the approval token: relay the list verbatim to the user, and after their consent call again with
  `regenerate: true` + `overwrite_revision`.
- Assets registered last time are kept after the redo, even if the new script no longer references them.
- When whole-script output violates constraints (references a name neither registered nor listed in `new_assets`, a handling decision can't be parsed, structure doesn't fit), the task fails
  with problem code `ad_script_rejected`; the script, assets and drafts are all unchanged. Relay the failure reason to the user, and per the reason add one line of additional instructions and generate again.

**Important: generating the script MUST be done by calling the MCP tools above. This skill provides no Python/Shell scripts; NEVER use BASH to call `python .../scripts/*.py`.**

## Generation flow

Inside the MCP tool, `ScriptGenerator` performs the following steps:

1. **Load project.json** — read content_mode, characters, scenes, props, overview, style
2. **Load the script** — within scope (all pending entries, or the entries named by `entry_ids`), select the entries and fields to author this time by fill-gaps or explicit rewrite; for ad without a script yet, or with `regenerate`, generate the whole thing
3. **Build the prompt** — generated by `lib.prompts.prompt_builders_script`, `lib.prompts.prompt_builders_reference` or `lib.prompts.prompt_builders_ad`, with these entries' content fields as input
4. **Call TextBackend** — `TextGenerator` selects the text model per project configuration, passing a Pydantic schema as `response_schema` to strictly constrain the JSON structure
5. **Pydantic validation and write-back** — the LLM only produces the visual layer; the backend writes it back to the script by entry id; content fields are not in the LLM output, which structurally prevents them drifting through Structured Outputs:
   - narration → `NarrationVisualEpisodeScript` (`segment_id` + image_prompt + video_prompt; video_prompt contains no dialogue)
   - drama (storyboard, incl. grid_storyboard) → `DramaVisualScript` (`scene_id` + image_prompt + video_prompt)
   - ad shots → `AdVisualScript` (`shot_id` + image_prompt + video_prompt); ad whole-script generation → `AdEpisodeScript` plus `new_assets` (storyboard) or `AdReferenceFlatScript` (reference_video); new assets have their references rewritten per the handling decision, then are registered in the same write as the script
   - reference_video → `ReferencePromptAuthoringFlatScript`: one rewritten body per pending unit, in order, with dialogue kept verbatim
6. **Add metadata** — `episode` (episode ID), `content_mode`, `novel` (project title + this episode's title; without a title, 「第 {播出位置} 集」 ("Episode {airing position}")), timestamps. These fields are hidden from the LLM (SkipJsonSchema) and injected by the backend from `project.json`, preventing LLM hallucinations from polluting downstream consumers (e.g. JianYing drafts).
   - Note: **no script of any skeleton writes a top-level `generation_mode`**. Generation mode is a project-level fact (`generation_mode` in `project.json`, locked at creation), and the script's skeleton kind itself embodies the generation mode; consumers always dispatch by reading `project.json` and MUST NOT look for that field on the script.

## Output format

The generated JSON file is saved to `scripts/episode_N.json`, with core structure:

- `title`: the episode title written by the LLM
- `episode` / `content_mode` / `novel` (with title, chapter): injected by the backend's `_add_metadata`, not dependent on LLM output
- Narration: `segments[]` (each shot has novel_text, duration_seconds, segment_break, appearing characters / scenes / props — carried over from the script plan at the review gate and modified on the script afterward; image_prompt, video_prompt — generated by prompt_authoring)
- Drama: `scenes[]` (each shot has image_prompt, video_prompt, duration_seconds, plus utterances, source_text, characters_in_scene etc. carried over from the script plan at the review gate)
- Ad/short film: `shots[]` (each shot has section, voiceover_text, products_in_shot, image_prompt, video_prompt, duration_seconds etc.); total duration deviating from `target_duration` beyond the threshold only logs a reminder and does not block saving
- Reference-to-video: `video_units[]` (each video unit has `text`, `duration_seconds` etc.)
- `metadata`: created_at, updated_at, generator

Entry count and whole-episode total duration are not persisted: they are obtained by reading the script, computed at read time by the project summary; persisting a copy would only drift from the body.

## `--dry-run` output

Prints the full prompt text that would be sent to the text model, without calling the API or writing files. Used to check prompt quality and length.

> For the data paths, script-plan subagents and schema selection of the two generation modes (storyboard / reference_video) under narration / drama, see `.claude/references/generation-modes.md`; for ad's path see `CLAUDE.ad.md`.
