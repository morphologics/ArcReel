# Generation mode reference

ArcReel splits "what content to make" and "how to generate the video" into two independent dimensions. `content_mode` strictly expresses the **content type** (narration / drama); `generation_mode` expresses the **video source / generation path** (storyboard / reference_video). Both are determined solely by top-level fields in `project.json`, cannot be changed after the project is created, and have no episode-level override. The combinations are enumerated below; on the reference-to-video path the content type only drives secondary decisions such as aspect ratio / default duration.

Grid is not a separate generation mode: `grid_storyboard` is an independent boolean switch that only takes effect under `generation_mode="storyboard"` (toggled by the user on the settings page; the Agent has no write access to it). It decides whether the storyboard image step produces single images or grid images, and does not affect how the other steps are dispatched.

## Mode matrix

| generation_mode | content_mode | Main data structure | Script plan subagent | script_plan intermediate file | Script schema | Visual reference source |
|---|---|---|---|---|---|---|
| `storyboard` | `narration` | `segments[]` | split-narration-segments | `script_plan_segments.json` | NarrationEpisodeScript | One storyboard image per shot as the start frame (cut from the grid image when `grid_storyboard=true`) |
| `storyboard` | `drama` | `scenes[]` | normalize-drama-script | `script_plan_normalized_script.json` | DramaNormalizedScript (script_plan) → DramaVisualScript (prompt_authoring) → DramaEpisodeScript (merged) | One storyboard image per shot as the start frame (cut from the grid image when `grid_storyboard=true`) |
| `reference_video` | `narration` / `drama` | `video_units[]` | split-reference-video-units | `script_plan_reference_units.json` | ReferenceVideoScript | Character / scene / prop sheets used directly as `reference_images` |

> drama runs in two stages (see ADR 0041): script_plan (normalize-drama-script) produces **structured content** `script_plan_normalized_script.json` (shot boundaries / assets on screen / verbatim spoken utterances / source anchor source_text / visual adaptation description); in prompt_authoring (create-episode-script) the LLM only outputs the visual layer `DramaVisualScript` (scene_id + image_prompt + video_prompt), and the backend writes it back by scene_id into the final script `DramaEpisodeScript`. At content review the whole episode's script plan becomes the final script; after that, content changes are made on the final script via `patch_episode_script`.
>
> script_plan intermediate files all live in `drafts/episode_{N}/`. Status detection and content review **only recognise the one file that matches the current project's generation_mode**: `script_plan_*` files of other modes in the directory are historical leftovers; they are neither evidence that the script plan is done nor a substitute input for content review. A leftover `script_plan_normalized_script.md` in an old drama project (the free-text draft from before structuring) is not a valid script_plan; normalize must be rerun to produce the `.json`.

## Step applicability is expressed by the plan

**This document no longer repeats a step table broken down by content mode or generation mode.** Which steps apply, their order, where things currently stand,
and which subagent the script plan should dispatch — always read these from the `steps[]` and
`next_action` of `mcp__arcreel__get_workflow_plan` (`next_action.args.preprocessor` of `prepare_script_plan` is the authoritative script plan subagent name).
See [workflow-plan.md](workflow-plan.md) for how to read it.

The table above only explains **differences in data structure and schema**: for the same step under different combinations, which main structure it operates on, which intermediate file,
which schema, and where the visual references come from. The difference between the two generation modes lies here, not in "which step is skipped".

A few facts the plan does not express and that need to be stated here:

- `reference_video` **only skips the storyboard image** step. TTS voiced projects can generate narration audio under both generation modes;
  reference-to-video just has no per-segment batch TTS entry point (no `segments[]`).
- Video enqueueing takes the generation mode from the project's `generation_mode`; the script skeleton is only used for validation. On a mismatch (e.g. a leftover
  `video_units[]` old script in a storyboard project) enqueueing is rejected outright; the right fix is to rerun the script plan and script generation for the project's current generation mode, not to expect the old script to be executed.
- The final script does not follow changes to the script plan: content changes are made on the final script via `patch_episode_script`; only redoing the whole episode reruns the script plan, after which it must be reconfirmed, and confirming overwrites the existing final script wholesale (after an overwrite confirmation).

## Video specs

- **Resolution**: images 1K, video 1080p
- **Per-shot duration** (storyboard, including grid_storyboard): the value must be within the model's `supported_durations`; when the project's `default_duration` is non-null it is the default (written into project.json by content_mode at project creation); when null, the script plan picks the value itself according to the content's pacing
- **Episode target duration** (non-ad projects): when the project's `episode_target_duration` is non-null, the script plan uses it to decide how many shots / video units to split this episode into; when `episode_target_units` is not explicitly set, episode planning also converts it via the spoken speech rate into how much source text each episode holds. Both are soft targets: when content is thin, prefer fewer splits; when content truly needs it, exceeding is allowed — exceeding only warns, never blocks
- **Per video unit duration** (reference-to-video): a video unit is the unit of one generation call, one video unit has one duration — the value must be within the effective duration options **matching that video unit's reference state** (`reference_unit_durations.with_references` / `.without_references` returned by `get_video_capabilities`; some models have separate duration limits for generation with reference images); when the content does not fit the chosen option, re-split the video unit rather than violate the duration. The concrete values are looked up by the subagent at run time via the `mcp__arcreel__get_video_capabilities` tool and are **not fixed in this document**. For existing final video units, the bucket and options they actually land in are governed by the server's verdict in `reference_unit_durations.units[unit_id]` of the same response: it picks the bucket by the reference images available right now; a reference to an asset that is registered but has no sheet does not count as having an image, and is named in `problems` / `unavailable_references`
- **Editing and final cut**: all modes join clips on the edit timeline and produce the final cut via the `edit-video` skill; Veo extend is only for **extending a single clip**, never for chaining different shots
- **BGM**: the generation side already auto-appends `Avoid: BGM、文字字幕、水印` (BGM, on-screen subtitles, watermarks) to the end of the video prompt; no need to append it manually, and do not describe BGM / score in the prompt either

## Prompt language

- Image/video generation prompts use **Chinese**
- Use narrative description, not keyword lists
- Extra rules for reference-to-video: reference assets with `@[角色]/@[场景]/@[道具]` (`@[character]/@[scene]/@[prop]`); **NEVER** describe appearance, clothing or scene details (the reference images provide them)

## Directory differences

> The directory tree below only illustrates the project structure; the session cwd is already at `projects/{name}/`, so **use paths relative to cwd when calling tools** (e.g. `videos/`, `reference_videos/`), never with the `projects/{name}/` prefix.

```text
projects/{name}/          # ← session cwd is already here
├── storyboards/          # storyboard-mode storyboard images (with grid_storyboard=true, holds the start storyboard images cut from the grid)
├── grids/                # storyboard mode with grid_storyboard=true (full grid images)
├── reference_videos/     # reference-to-video outputs
├── videos/               # storyboard-mode video outputs
└── audio/                # narration audio (narration only, created on first generation)
```

> See the prompting strategies in the [official Gemini image generation guide](https://ai.google.dev/gemini-api/docs/image-generation).
