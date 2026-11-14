---
name: edit-video
description: Edit an episode on the edit timeline and, on request, render the final cut or export a JianYing draft. Use when the episode's videos are all in and a cut is needed; also use this skill when the user asks for changes by clip number (e.g. "c3 drags"), or asks to add BGM, render the final cut, export a JianYing draft, duplicate or rename an edit timeline, or go back to an old revision.
---

# Edit Video

The final-cut form of an episode is recorded only on the edit timeline: the preview is stitched from it, and both the final cut and the JianYing draft are generated from it. All editing and rendering is done through `mcp__arcreel__*` tools. For each tool's parameters, value ranges and write semantics, the tool description is authoritative.

## One edit round

Each time the user makes an edit request, run one round. Every round ends with one delivery.

1. **Determine the edit timeline**: use `mcp__arcreel__list_timelines` to list the episode's edit timelines.
   - The episode has no edit timeline yet: use `mcp__arcreel__create_timeline` to mechanically create one from the script, with the display name 「完整版」 ("Full cut").
   - If the user named one, use that one; if not, use the one with the latest modification time in `list_timelines`. When the user's wording matches several and it is unclear which one is meant, ask the user first.

   Done when: you have exactly one edit timeline ID.
2. **Read**: use `mcp__arcreel__read_timeline` to read the selected edit timeline and map the user's requests to clip numbers (e.g. c3). If you cannot find the clip the user means, ask before changing anything. If the user only asked to render, go straight to "Render" after reading. In a first-round edit (a newly created timeline, or the user asks you to cut a version), after reading, if `issues` contains `narration_missing`, first fill in the missing narration audio per `generate-narration-audio` and re-read the timeline (narration overrun and collision can only be computed with measured audio durations), then review all footage per the "First-round full review" section, then go to step 3.

   Done when: every request is located to a clip, or the information needed from the user is clearly identified; for a first-round edit, additionally every video unit has a review-footage report, and for TTS narration projects the narration audio is filled in or the units that could not be filled are noted.
3. **Edit decisions (L1)**: based on the user's requests and each clip's duration, status, edit rationale and `issues`, decide this round's operations:
   - **Structure**: decide which clips to keep, reorder and reuse around narrative order. For large restructures (reordering most clips, changing the overall pacing) duplicate first: use `mcp__arcreel__create_timeline` (`from: "timeline"`, `timeline` set to the currently selected ID) to make a copy, with a display name describing the cut in terms the creator will recognize, such as 「快节奏版」 ("Fast-paced cut"); then edit the copy, keep the original as a fallback, and tell the user the new timeline's name.
   - **Look at the footage**: in the first round, rely on the review-footage reports. When the user later asks about the footage of certain segments in conversation (whether a segment is broken, where it picks up or trails off, which candidate version is better), use `mcp__arcreel__inspect_video_units` to view the contact sheets of the relevant video units and judge from the images yourself. Contact sheets also mark machine-check signals: BLACK black segments and FREEZE frozen segments are suspected defects; confirm by looking before deciding whether to trim them; CUT shot-change points are structural information, not defects. Signals are only hints; decide based on the footage.
   - **Pacing**: decide trims or end freeze-frames from the ranges the user gave, or from moments you settled on after looking at the footage. The times marked on the contact sheet share the same time axis as in/out points; place in/out points right on the CUT frame or the frame before it, never in the middle of a transition; when trimming black or frozen frames at the head or tail, the range's end is a candidate in point and its start is a candidate out point.
   - **Transitions**: transitions can be set between adjacent clips. The script's `segment_break` (scene change point) can serve as a hint for where to place a transition.
   - **Sound**: adjust the original audio volume as the user asks. When the user wants to add, replace or adjust BGM, follow the "Add BGM" section.
   - **Narration**: when `issues` contains `narration_overrun` or `narration_source_collision`, follow the "When the narration does not fit" section.
   - **Version**: when the user specifies another video version, or when the review-footage report or looking at the footage shows another candidate version is clearly better, switch directly with `mcp__arcreel__select_video_version` (no need to ask for consent), then re-read the timeline and redecide the trims.
   - **Pending creator decisions**: note video units whose footage is unusable and needs regeneration, and voice-over that needs rewriting; put them in the checklist in step 5.
   - **Rationale**: for each kept or newly added changed clip, write a one-sentence edit rationale addressed to the creator; write the rationale for deleted clips into this round's summary.

   Done when: every request in this round maps to specific clips and operations; for requests that cannot be done, note the reason to explain in step 5.
4. **Submit**: collect this round's timeline changes into a single `mcp__arcreel__edit_timeline`, submitted against the most recent read result, with a one-sentence change summary written for the creator. If there is no executable change, go straight to step 5. If an operation is rejected, fix it using the returned location info and resubmit the whole batch; on `revision_conflict`, re-read first, then redecide this round's changes based on the latest content.

   Done when: the changes were submitted successfully, or it is clear there is nothing to submit this round.
5. **Deliver**: per the "End-of-round delivery" section, deliver in one go the change summary, "Jump here" links, and, when there are pending decisions, the checklist.

## First-round full review

In the first round, look at every video unit used by the edit timeline (the current version, comparing candidate versions where they exist), handing this to the read-only review-footage subagent `review-footage` in parallel. Contact sheets go only into the subagents' context; you read only their text reports.

1. **Group**: collect the video unit IDs used by the clips in `read_timeline`, deduplicate, and group by scene or adjacent units, about 6-10 video units per group. A unit together with its candidate versions goes into only one group, compared by the same subagent.
2. **Dispatch**: in a single message, launch one `review-footage` subagent call per group so the groups run in parallel. The dispatch message states the script file, the group's video unit IDs and the report requirements. The report must at least answer: whether it is usable, the usable range and why, problems and their timestamps, the ranking of candidate versions, whether regeneration is recommended (and whether the prompt needs changing), and one sentence that can go straight into the edit rationale.
3. **Consolidate**: following the reports, decide step 3's operations unit by unit: pick the version, trim to the usable range, write the edit rationale; record units recommended for regeneration as pending creator decisions.

Done when: every video unit used by the timeline has a review-footage report. Dispatch once more for units missing a report.

## End-of-round delivery

An edit round ends with one delivery, with three things in the same message:

1. **Change summary**: list by clip number what changed this round and why, including which video version was picked. Then list the blocking- and warning-level problems still present in the result's `issues`, and explain requests that could not be done and why.
2. **"Jump here" links**: attach an in-app link to every problem, written as a Markdown link, with a path starting with `/app/`, no domain, and the project name URL-encoded as a path segment:
   - Problem on a clip of the edit timeline: `/app/projects/{项目名}/episodes/{集 ID}?view=edit&tl={剪辑时间线ID}&t={秒}`. `t` is the global time on the edit timeline: the clip's `start` plus "problem timestamp - the clip's in point".
   - Problem on a video unit not in the timeline: `/app/projects/{项目名}/episodes/{集 ID}?unit={单元ID}&t={秒}`. `t` is the time within that video unit's own video.
3. **Checklist**: when there are pending creator decisions, after sending the delivery message ask them all at once with an AskUserQuestion multi-select (`multiSelect: true`), one question per category:
   - **Regenerate**: first preflight with `mcp__arcreel__generate_videos` (`preview: true`, `target` set to `selected` listing all candidate units, `force: true`). Each option states the video unit, the problem and timestamp, whether the prompt will change, and the estimated cost and duration tier (when the tier changes, state the arranged duration and requested tier); also add a "Regenerate all" option stating the total cost. Units that are `blocked` in the preflight do not become options; explain the gap in the change summary.
   - **Rewrite voice-over**: each option states original text -> revised text, and the estimated seconds saved.

   When a question's options cannot hold all candidates, merge by scene into one option stating which video units it includes and the total cost. Send no checklist when there is nothing pending.

Done when: every problem in the delivery message has a link; every pending decision is in the checklist with all the information above.

## Executing checked items

Paid or script-changing actions are confirmed only once per round: what the creator checks in the checklist is executed directly, without asking again.

- **Regenerate**: for units whose prompt needs changing, first fix the prompt with `mcp__arcreel__patch_episode_script`. Then submit all checked items with a single `generate_videos`: `target` set to `selected` listing the checked units, `force: true`, and `confirmed_request_durations` carrying the same-named field from the preflight result unchanged, so the tier will not need confirming again. If asked to reconfirm, the tier or cost has changed; tell the creator the new quote and let them decide whether to continue.

  Acceptance after completion: note each unit's current version number before regeneration, and dispatch `review-footage` to compare the old and new versions. Make the better one current (use `select_video_version` to switch back when the old version is better), reset in/out points per the report, write them into the edit timeline with `edit_timeline`, then tell the creator the result per "End-of-round delivery". If the new version still has problems, only report them in the delivery; do not automatically regenerate a second time.
- **Rewrite voice-over**: rewrite the checked voice-over with `mcp__arcreel__patch_episode_script`, then use `mcp__arcreel__generate_narration_audio` with `segment_ids` naming those video units to regenerate the narration audio.

## When the narration does not fit

Narration starts at the start of its carrying clip and plays for its measured audio duration; when it is longer than the footage it extends over the following clips, running into the next narration or past the end (`narration_overrun`), and it may also cover dialogue or clips whose original audio is not ducked (`narration_source_collision`). Handle it in three tiers by authorization boundary:

- **Only touch the edit timeline: just do it.** Widening trims, switching video versions, inserting extra clips from the same video unit, extending with an end freeze-frame, re-attaching narration with `place_narration`, and lowering the original audio volume of covered clips are all submitted in this round together with other changes, with the reason written into the clip's edit rationale.
- **Rewrite voice-over: confirm with the creator first.** Put it in the checklist and handle per "Executing checked items".
- **Regenerate a longer video: change the arranged duration first, then get consent.** This incurs cost: first state which video unit's arranged duration you would change to what, and the estimated cost; only after the creator agrees, change the arranged duration and regenerate.

Both issues are warnings and do not block rendering. If it still does not fit after edit adjustments, state the remaining overruns and collisions in the summary and let the creator decide whether to render with the warnings or approve a rewrite or regeneration.

## Add BGM

BGM is uploaded by the creator on the BGM track of the edit view in the Web UI, belongs to the whole project, and its loudness is normalized at upload. BGM operations are submitted in the same `edit_timeline` as this round's other changes.

1. **Pick BGM**: use `mcp__arcreel__list_bgm` to list the project's BGM. If the user named a track, use it; otherwise pick by name the one that best fits the episode's mood and say in the summary which one you picked. When the project has no BGM, or none fits, ask the creator to upload one on the BGM track of the edit view, then place it after upload.
2. **Place**: use `insert_bgm` to place it at an absolute start on the timeline. Use default volume and fades; adjust only when the user asks.
3. **Chain segments**: when the BGM is shorter than the range to cover, place the next segment right after: the next segment's `start` equals the previous segment's end time (previous `start` + `source_out` - `source_in`), end to end with no overlap. The next segment can take another passage of the BGM to avoid repeating the same phrase. Shorten the fade-out/fade-in on both sides of the join to about 0.5 seconds so it sounds smoother.
4. **Ending**: anything past the end of the timeline is truncated automatically with a fade-out at the cut, so you need not compute the last segment's out point exactly.

Done when: BGM clips cover the range the user asked for without overlapping each other; `issues` has no `bgm_missing`. If `bgm_missing` appears, switch to another BGM already in the project, or ask the creator to upload again.

## Going back to old revisions and tidying timelines

Every submit appends a revision; history is never rewritten.

- **Go back to an old revision**: when the user wants to undo changes or return to an earlier state, use `mcp__arcreel__list_revisions` to find the target revision. When the user says "undo the last one" or names a revision, roll back directly with `mcp__arcreel__restore_revision`; when the target is unclear, first list the revision summaries for the user to choose from. A rollback replaces the current content wholesale; after rolling back, read the timeline once and tell the user what it looks like now in the change-summary format of "End-of-round delivery".
- **Rename**: use `mcp__arcreel__rename_timeline`; it changes only the display name and creates no revision.
- **Delete**: the Agent has no tool for deleting an edit timeline (`delete_project_file` deletes only unreferenced files and old versions; timelines will be rejected). When the user wants one deleted, tell them to open the menu next to that timeline's tab in the edit view of the Web UI and choose 「删除」 ("Delete").

## Render

Rendering has two deliverables: the final cut (`mcp__arcreel__render_final_cut`) and the JianYing draft (`mcp__arcreel__export_jianying_draft`). Produce whichever the user asks for; when they just say "render", produce the final cut. Export a JianYing draft only when the user asks.

1. **Report problems**: go by the `issues` in the most recent read or submit result.
   - Blocking-level problems: list them for the user, explain which clips block rendering and how to resolve them; do not submit until they are resolved. Problems with `applies_to` of `with_narration` block only the narrated version. Paid fixes (e.g. generating missing videos) are the user's decision.
   - Warning-level problems: list them for the user, explain that rendering will proceed with these problems, then submit. If the user asks to fix first, go back to "One edit round".
2. **Submit**: submit the selected timeline with the matching tool and wait for the terminal result. The narration version (both deliverables have it) and whether to burn in subtitles (final cut only) follow the user's request; if the user did not mention them, omit them to take the defaults: TTS narration projects include narration, and the final cut burns in subtitles. If rejected, tell the user the returned problem codes and the clips involved, and offer fixes within what the tools support. After the user confirms, run another edit round.
3. **Deliver**:
   - Final cut: give the download link and the acceptance result. If the measured audio/video duration clearly differs from the edit timeline duration, point it out explicitly.
   - JianYing draft: the result has no download link. Tell the user the draft has been generated and to download it in the Web UI, entering their local JianYing draft directory when downloading.
