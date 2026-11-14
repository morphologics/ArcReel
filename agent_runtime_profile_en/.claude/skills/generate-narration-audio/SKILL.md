---
name: generate-narration-audio
description: Generate narration audio (TTS) shot by shot for narration scripts. Use when a TTS project's first-round automatic edit needs missing audio filled in, when the user asks to generate or regenerate narration audio for a shot or an episode, or when an interrupted batch narration run needs completing.
---

# Generate Narration Audio

For each shot of a script in the narration content mode, synthesize one narration audio clip from that shot's `novel_text` source text,
written back to the shot's `generated_assets.narration_audio` (output `audio/segment_{segment_id}.wav`).
Depends only on the script, not on storyboard images/videos -- it can proceed as soon as the script is generated.

## Tool calls

**Important: generating narration audio MUST enqueue via the MCP tools below. This skill provides no Python/Shell scripts; NEVER use BASH to call `python .../scripts/*.py`.**

Enqueue via MCP tools:

| Operation | Tool |
|------|------|
| Fill in the whole episode (default, all shots missing narration audio) | `mcp__arcreel__generate_narration_audio({"script": "episode_1.json"})` |
| Specific batch range | `mcp__arcreel__generate_narration_audio({"script": "episode_1.json", "segment_ids": ["E1S01", "E1S02"]})` |
| Regenerate a single shot | `mcp__arcreel__generate_narration_audio({"script": "episode_1.json", "segment_ids": ["E1S05"]})` |

> **Selection rule**: without `segment_ids`, only shots missing `narration_audio` are enqueued -- stale but still usable old audio is reused, not automatically regenerated;
> explicitly passed shots are re-synthesized even if they already have narration audio (used to regenerate after changing voice/speech rate).
>
> **When to call**: in a TTS project's first-round automatic edit, fill in what is missing; choosing TTS for the project is already authorization, no separate confirmation needed;
> the user may also ask for generation at any time. Stale audio is re-synthesized only at the user's request; post-production voice-over projects need no TTS.
> Video generation does not depend on narration audio; neither waits for the other.
> After new narration audio is done, ask the user to listen and confirm.
>
> **Dependencies**: the generation worker must be online (separate audio channel); the project's narration delivery mode is "TTS narration". The TTS model, voice and speech rate
> all come from the TTS snapshot in the project settings, independent of global defaults.
>
> **Change voice/speech rate**: when the user asks "use voice X / speech rate 1.2 for this project's narration", call
> `mcp__arcreel__patch_project({"settings": {"narration_voice": "X", "narration_speed": 1.2}})`
> to change the project's TTS snapshot (affects only the current project; pass `null` for `narration_speed` to send no speech rate to the provider). After the change, narration audio generated with the old model, voice or speech rate
> reads as stale: it can still be previewed and used in the edit, and whole-episode fill-in will not regenerate it; to switch to the new settings, name those shots for re-synthesis.

## Workflow

1. **Status check** -- read the script, check each shot's `generated_assets.narration_audio`, count the missing shots and tell the user
2. **Enqueue generation** -- call the MCP tool; tasks go through the generation queue and are handled by the worker; the tool waits for all to finish and returns per-shot results
3. **Report** -- summarize the success/failure details for the user

## Resume after interruption

After an interruption (service restart, task failure, session disconnect), just call again with **whole-episode fill-in without `segment_ids`**:
shots that already have narration audio are skipped automatically, only missing shots are filled in, with no double charging.

## Error handling

- A single shot failing does not affect the batch: the tool returns per-ID results of `requested / succeeded / failed / blocked`
- Decide whether to retry or fix inputs first from the problem code and next action carried by each item in `failed` / `blocked`; do not guess from the text
- Retry retryable shots precisely with `segment_ids`
- When the tool returns `narration_delivery_post_production`, tell the user this project's narration is delivered via post-production voice-over and the videos are unaffected;
  if the user actively wants in-app narration, ask them to change the delivery mode to TTS narration in the Web project settings and retry
- When the tool returns a `narration_tts_*` project configuration problem, likewise explain that the post-production voice-over route still works; **continuing with the videos does not need TTS**.
  Only when the user actively wants in-app narration, guide them to fill in the TTS model or voice in the Web project settings and retry
