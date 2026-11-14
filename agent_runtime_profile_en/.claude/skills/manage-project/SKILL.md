---
name: manage-project
description: Project management toolset. Use for: adding/modifying characters/scenes/props in project.json (via the patch_project tool, upsert by table+name), cascading asset rename (rename_asset tool), merging assets registered twice for the same identity (merge_asset tool), writing top-level settings fields, editing the project overview, and querying video model capabilities (get_video_capabilities). Episode planning is not in this skill: use the mcp__arcreel__plan_episodes / reset_episode_planning server-side tools.
user-invocable: false
---

# Project Management Toolset

Provides batch writes of characters/scenes/props to project.json, editing of project-level settings and the project overview, and video model capability queries.

## Tool overview

| Tool | Function | Caller |
|------|------|--------|
| `mcp__arcreel__patch_project` (SDK tool) | Add/modify characters/scenes/props in project.json (upsert by table+name), top-level settings fields, or the project overview (overview branch) | Subagent / main Agent |
| `mcp__arcreel__rename_asset` (SDK tool) | Cascading asset rename: in one go updates the asset table key, name references in all episode scripts and script_plan drafts (reference arrays / speaker / `@[名称]` mentions), and associated files and version history | Subagent / main Agent |
| `mcp__arcreel__merge_asset` (SDK tool) | Merge two same-table assets registered for the same identity into one: references are repointed once to the kept one, the merged-away name is recorded as an alias, or it is merged as a derivative of the kept character | Main Agent |
| `mcp__arcreel__get_video_capabilities` (SDK tool) | Query video model capabilities (model granularity, resolved by the project's single generation_mode, same basis for the whole project, no episode needed) | **Subagent** (queries on its own while executing tasks) |

> Episode planning (split into episodes / adjust) is done by the server-side tools `mcp__arcreel__plan_episodes` / `mcp__arcreel__reset_episode_planning`; adjusting already-planned content goes through "reset + replan", see video-workflow stage 2 for the flow.

## Writing characters/scenes/props

Write via the `mcp__arcreel__patch_project` tool (the project name is bound by the session, no parameter needed). Call separately per table;
each entry is upserted keyed by name: if the name does not exist it is added, if it exists the fields are merged. **Revising existing asset descriptions must be driven by explicit user
intent** (to avoid silently overwriting manually edited fields). Assets newly appearing in this episode are brought out by the script plan and registered at the review gate; no need to pre-write them here.
`aliases` is a list of strings, used only by the script plan to recognize who is who; it must not be written into references.

```text
mcp__arcreel__patch_project({"table": "characters", "entries": {"角色名": {"description": "...", "voice_style": "..."}}})
mcp__arcreel__patch_project({"table": "characters", "entries": {"角色名": {"derivatives": {"战斗装": {"description": "换上黑色重甲，其余外观保持不变"}}}}})
mcp__arcreel__patch_project({"table": "scenes", "entries": {"场景名": {"description": "..."}}})
mcp__arcreel__patch_project({"table": "props", "entries": {"道具名": {"description": "..."}}})
mcp__arcreel__patch_project({"settings": {"episode_target_units": 1000}})
mcp__arcreel__patch_project({"settings": {"episode_target_duration": 90}})
mcp__arcreel__patch_project({"settings": {"source_language": "en"}})
mcp__arcreel__patch_project({"settings": {"narration_voice": "Ethan", "narration_speed": 1.2}})
mcp__arcreel__patch_project({"overview": {"genre": "悬疑", "theme": "复仇与救赎"}})
```

A character entry's `derivatives` table registers another look of the same character besides the base look (persisting across scenes, expressible by image generation);
the script references it as `@[角色/衍生]`. It merges by derivative name: same name changes the description, a new name is added, unmentioned derivatives are kept as is (along with their asset sheets).
A derivative only writes `description` -- the change relative to the base look, in the tone of an image editing instruction; the asset sheet is written back by the generation pipeline.
Renaming a derivative goes through the derivative sub-resource's rename endpoint (the 「衍生」 ("Derivatives") popover on the character card in the Web UI); writing a new name with `patch_project` only creates a new one.

**Pick exactly one of three call forms**: pass `{"table", "entries"}` for asset upsert, pass `{"settings"}` for top-level field writes,
pass `{"overview"}` for project overview editing; passing several or none is rejected. `settings` whitelisted fields:

- `episode_target_units`: `int >= 1` to set / `null` to clear. Target size per episode (read as reading units per `source_language`); the episode planning tool uses it to gauge how much to cut per episode; when unset, the tool converts `episode_target_duration` into an equivalent size via the spoken-delivery rate, so there is no need to ask the user for a word count just to control size
- `episode_target_duration`: integer seconds `10`-`600` to set / `null` to clear. Target final-cut duration per episode; the script plan uses it to decide how many shots / video units to split this episode into; when `episode_target_units` is unset, episode planning also converts it into how much source text to fit per episode (when `episode_target_units` is set explicitly, the latter wins). A soft target (can be overridden by content needs; exceeding it only warns, does not block); writable only for non-ad/short-film projects; writes in ad projects are rejected (whole-episode size is already expressed by the `target_duration` budget)
- `source_language`: `"zh" / "en" / "vi"` to set / `null` to clear. Priority: **explicit user configuration > automatic inference** -- write it as soon as the user explicitly specifies a language (not limited to cases where overview was skipped or failed); without explicit user confirmation do not guess and write it yourself; on the normal path it is persisted automatically by overview generation. When you find the explicit configuration disagrees with the automatic inference / actual language of the source, remind the user (WARN) and continue with the explicit configuration, without blocking the flow
- `brief`: string to set / `null` to clear. Short creative brief text, writable only for ad/short-film projects (`content_mode=ad`); writes in other project types are rejected
- `narration_voice`: non-empty string (voice id per provider docs) to set / `null` to clear. The narration voice in the project's TTS snapshot, affects only the current project; TTS narration projects must have a voice, so clearing is rejected
- `narration_speed`: positive finite number (e.g. `1.2`) to set / `null` to clear. The speech rate in the project's TTS snapshot, affects only the current project; clearing means no speech rate is sent to the provider
- `character_voice_binding`: `"prompt" / "reference_audio"` to set / `null` to clear back to default (`prompt`). What constrains character voices: `prompt` writes the character's `voice_style` into the prompt as a soft constraint; only `reference_audio` attaches the character's configured reference audio to the request to the video model for native voice consistency. Native consistency requires both: this field set to `reference_audio` and the character having reference audio configured

`generation_mode` and `grid_storyboard` are not whitelisted and `patch_project` rejects writing them: `generation_mode` cannot be changed after project creation; when the user asks to change the generation mode (storyboard <-> reference_video), state clearly that it cannot be changed and there is no workaround; `grid_storyboard` is toggled by the user on the Web settings page; when the user asks to change grid composition, direct them to the settings page, and tell them the toggle only affects later generations -- already generated storyboard images are not automatically invalidated; to produce images with the new composition, the corresponding shots must be explicitly regenerated.

`overview` whitelisted fields: `synopsis` / `genre` / `theme` / `world_setting`, **merge semantics** (only passed fields change;
the overview is created if it does not exist). **Revising the overview must be driven by explicit user intent** (to avoid silently overwriting manually edited fields).

The tool return distinguishes **N added / N merged with field changes**, and explicitly lists ignored fields (system-managed fields such as ``reference_image`` /
``character_sheet``, deprecated fields such as ``type`` / ``importance``). On structurally invalid input (e.g. missing
description) nothing is persisted and `is_error: true` is returned.
**Strictly forbidden**: editing `project.json` directly with Write/Edit/Bash -- change fields via the patch_project tool, rename assets via the rename_asset tool,
merge two assets into one via the merge_asset tool. `patch_project` upserts by name; using it to rename only produces "new name created + old name left behind" and updates no references.

## Merging assets registered twice

When the same person, place or object is registered as two same-table assets (e.g. "Old Wang" and "Wang Jianguo"), use `mcp__arcreel__merge_asset` to merge
`source` into `target`. A merge cannot be undone; proceed in this order:

1. Confirm with the user which one to keep as `target`, and how to merge: the same look merges as the base look (default); when `source` is another look of `target`
   (e.g. "the man in black" is the protagonist in disguise), pass `as_derivative: true`; this applies only to characters.
2. Call with `dry_run: true`, and relay to the user the receipt's per-episode count of rewritten references, the number of storyboard images and videos that will go stale, and what `source` will not keep
   (description, asset sheet and version history, voice settings, original images, reference audio). When `source`'s description has information `target` needs,
   also ask the user whether to first add it to `target` via `patch_project`.
3. After the user agrees, call again without `dry_run` to execute.

## Querying video model capabilities

Query via the MCP tool (the project name is bound by the session, no parameter needed):

```text
mcp__arcreel__get_video_capabilities({})
```

The generation mode is determined solely by the project, with no episode-level override; capability queries use the same basis for the whole project and do not accept / need an `episode_id` parameter.

**Returns**: JSON text containing `provider_id` / `model` / `supported_durations[]` / `max_duration` / `max_reference_images` / `source` / `default_duration` / `episode_target_duration` / `content_mode` / `generation_mode`; reference-to-video projects in narration / drama additionally include `reference_unit_durations` (two sets of effective tiers, `with_references` / `without_references`, applied according to whether the video unit has `@` references; `units` gives, per `unit_id`, for each existing formal video unit the bucket `hydrated_capability` determined server-side from available reference images, that bucket's effective tiers `allowed_durations`, the factual failure `problem`, and `problems` / `unavailable_references` when declared references and available reference images diverge -- for existing units, bucket and tiers follow it); **ad projects do not return this field** -- ad's machine field `unit` is a lightweight index derived from `shots[]`, shot durations are not governed by the tier enumeration (rules in `video-workflow/SKILL.ad.md`); do not wait for this field, and do not rearrange ad shot durations by tiers.

**Use**: script plan subagents in every generation_mode (storyboard / reference_video) check this themselves while executing, to decide per-shot / per-video-unit durations. **Decision priority** (high to low): hard constraints (storyboard shot durations must come from `supported_durations`; narration / drama reference_video unit durations must come from the `reference_unit_durations` tiers matching that video unit's reference state; ad shot durations follow the free-integer rule in `SKILL.ad.md`) > `episode_target_duration` / `default_duration` preferences (the former is a soft target for the sum of unit durations in this episode, used to decide how many to split into; the latter, when non-null, is the default for a single unit) > content needs (reference_video takes the tier the video unit's content actually needs; narration / drama long sentences and complex visuals may take longer values). When it does not fit, re-split the video units; do not violate durations.

**Errors**: returns `is_error: true` when the project is not found or model capabilities cannot be resolved, with the reason in the text.
