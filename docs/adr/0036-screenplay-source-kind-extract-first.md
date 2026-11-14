---
status: accepted
---

# Screenplay sources: the source kind is recorded with the source file, extract-first reuses the whole pipeline, and verbatim fidelity anchors only audible content

Drama mode treats the source text as a novel by default, and episode planning, script planning, and prompt authoring **create** a script from the prose. But some users upload a finished screenplay that already has its own episodes, scenes, dialogue, voiceover, and characters, and the "adaptation" pipeline would **rewrite it a second time** — re-cutting the author's episodes, adapting the dialogue, and dropping the voiceover. We decided to introduce a source kind (`source_kind`: `novel` by default / `screenplay`) as a third axis orthogonal to both the creation type and the generation mode; under `screenplay` the pipeline flips from "create" to "extract-first", and verbatim fidelity is anchored only on "audible content".

## The source kind is recorded with the source file

A project can have several source texts at once (multiple files of a whole-book source, episodes that carry their own source text); "a novel main story + a screenplay side story" is a normal shape, so the source kind is recorded with the source file: each file of the whole-book source records its own kind, an episode with its own source text records it on its episode-ledger entry, an episode cut from the whole-book source takes the kind of the file in which its source range starts and does not store it separately, and an episode without source text has no kind. The kind is chosen together with "whole-book source / per-episode source" when the source text is uploaded or entered, defaults to novel, and can be changed at any time afterwards.

- Only drama mode has a source kind; both generation modes, storyboard image-to-video and reference-to-video, switch between the "adapt / extract" approach according to it. In narration/commentary mode the source text already goes verbatim into the narration, and a finished commentary script is simply uploaded as the source; ads/short films have no source text, and a finished script is written by hand via "start from blank".
- The kind is not source content: changing the kind does not change the source fingerprint, does not trigger remapping of cut episodes, and leaves the episode ledger untouched; episodes already cut keep the split planned under the old kind, and re-cutting under the new kind is initiated by the creator through re-planning. A script plan produced under the old kind is judged stale by artifact currency; the formal script is not based on the script plan (see `docs/adr/0080`) and is unaffected. Whether to re-plan the script under the new kind is the creator's decision.
- A single AI episode-planning window is truncated when it hits a kind change, so a window contains only one kind.
- The project overview uses the kind when all sources share the same kind, and the novel approach when they are mixed.

## Extract-first; verbatim fidelity anchors only audible content

Extract-first **does not build a separate screenplay parser**; instead it is spread across every existing stage — episode planning, script planning, and the project overview. The LLM first uses what the author has already written (in any form, with no hard-coded marker regexes) and generates only what is missing, which fits the "lazy per-episode consumption" architecture (scripts can reach 100,000 characters), and each stage degrades independently (character list present but no episode markers → characters extracted verbatim + semantic episode planning). **Episodes are never cut mechanically**: episode planning is still semantic planning; if the screenplay has its own episodes (in any form), their boundaries, titles, hooks, and outlines are used as-is, otherwise split points are chosen along complete story arcs — cutting mechanically by length would shred story arcs. **Verbatim fidelity anchors only "audible content"**: the strict no-rewrite, no-drop, no-polish rule applies only to two things, character dialogue text and voiceover text; layout and labels, camera directions and stage directions, visual descriptions, and generic extras are transcribed or stripped at the LLM's discretion (hard verbatim would force stage directions, extras, and layout symbols into structured fields).

Verbatim-extracted dialogue and voiceover land in `utterances` (dialogue `kind=dialogue` with a speaker, voiceover `kind=voiceover` without a speaker, in order of appearance; for the model see `docs/adr/0040`). Generic speakers (`老人甲` (Old Man A) / `年轻人乙` (Young Man B)) are filled with the source's wording but are not registered as character assets and do not enter the appearing characters — generic vs. named characters is decided by the LLM at the prompt level; "the speaker must be an appearing character" is only a prompt instruction with no mechanical check, and is relaxed for screenplays. The extracted skeleton (episodes + characters) relies on the candidate review in episode planning (adjustments go through re-planning, see `docs/adr/0032`) and the content confirmation in script planning to catch the LLM's misjudgements on wild formats; no separate mechanism is added.

## Considered Options

- **One big dedicated parser**: it would have to stuff 100,000 characters into a single call and front-load all work to upload time, working against the "lazy per-episode consumption" architecture, and hard-coding structural assumptions for wildly varied formats is brittle; extract-first spreads across existing stages, each stage uses the LLM for semantic recognition on its own slice and fills in whatever is missing.
- **Project-level, fixed at creation**: cannot express mixed sources; on the Web the project is created first and the source uploaded later, so the kind would have to be chosen at creation before the source has been seen, and a wrong choice could only be fixed by recreating the project.
- **Project-level but changeable**: fixes wrong choices but still cannot express mixed sources; and downstream already reads the kind per planning window and per episode — only the project overview needs a whole-project view.

## Consequences

- Prompt templates are split into named variants along the source-kind axis (see `docs/adr/0077`): episode planning, script planning for both drama-mode generation modes, and the project overview each have `novel` / `screenplay` variants.
- Data validation accepts only the two values `novel` / `screenplay`, rejecting invalid values such as `screen_play`; the project top level no longer has `source_kind`.
- "Voiceover/dialogue voice-over" (drama-TTS) belongs to the voice-over and compositing stage and is out of scope for this decision.
