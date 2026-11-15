"""Declarations of the edit timeline tools: create (from the script or by copying), list, read, batch edit, rename,
revision history and rollback, and listing the BGM that can be placed on a timeline."""

from __future__ import annotations

from server.agent_toolset.declaration import BLOCKED, ToolDeclaration
from server.media_tools.bgm import ListBgmRequest, bgm_list_summary, list_bgm
from server.media_tools.edit_timelines import (
    CreateTimelineRequest,
    EditTimelineRequest,
    ListRevisionsRequest,
    ListTimelinesRequest,
    ReadTimelineRequest,
    RenameTimelineRequest,
    RestoreRevisionRequest,
    create_timeline,
    edit_timeline,
    list_revisions,
    list_timelines,
    read_timeline,
    rename_timeline,
    restore_revision,
    revision_history_summary,
    timeline_list_summary,
    timeline_readout_summary,
    timeline_renamed_summary,
    timeline_write_summary,
)

CREATE_TIMELINE = ToolDeclaration(
    name="create_timeline",
    description=(
        "Create a named edit timeline; one episode can have several, and display names cannot repeat "
        "(timeline_name_conflict). "
        "from=script: lay out every video unit of episode in the current script order, as a starting point for "
        "editing: "
        "each used whole, all hard cuts, source volume 0.3 for voice-over units and 1.0 for dialogue and voiceless "
        "units, narration hung on the unit's first clip; "
        "returns episode_not_found when the episode does not exist. "
        "from=timeline: copy revision `revision` of timeline (latest revision when omitted) into a new timeline of "
        "the same episode; "
        "content is carried over as is (order, trims, source volume, holds, transitions, narration placement, BGM), "
        "clip IDs are unchanged, and the new timeline starts from revision 1; "
        "suitable for keeping a fallback before a large restructuring, or for making an alternative cut side by "
        "side; the source timeline is unaffected, "
        "and a nonexistent revision returns revision_not_found. "
        "A clip's picture is always the video unit's current video version; edit timelines do not change "
        "automatically with the script — after script changes, differences show up in issues. "
        "The result has the same shape as read_timeline."
    ),
    request_model=CreateTimelineRequest,
    migration=BLOCKED,
    domain_key="edit_timeline",
    handler=create_timeline,
    summary=timeline_readout_summary,
)

LIST_TIMELINES = ToolDeclaration(
    name="list_timelines",
    description=(
        "List the edit timelines in the project: id, episode ID, display name, latest revision number, clip count "
        "and last modified time, ordered by episode and creation order. "
        "Read-only, no side effects."
    ),
    request_model=ListTimelinesRequest,
    migration=BLOCKED,
    domain_key="edit_timelines",
    handler=list_timelines,
    summary=timeline_list_summary,
)

READ_TIMELINE = ToolDeclaration(
    name="read_timeline",
    description=(
        "Read the full content of an edit timeline's latest revision; revision is the current revision number. "
        "Read-only, no side effects; "
        "returns timeline_not_found when the ID does not exist. All times are in seconds with at most three "
        "decimals. "
        "clips are in playback order; each clip carries id (e.g. c3, stable within this timeline — use it to refer "
        "to clips when talking with the creator), "
        "unit_id, start and duration (absolute start and duration computed by the server), source_volume (source "
        "audio volume 0–1), "
        "trim (in/out points and the video version they are based on, basis_version), source_duration (full length "
        "of the current video, null when there is no usable video), "
        "hold (freeze-frame extension at the tail), transition_to_next (transition to the next clip, null is a hard "
        "cut), narration (narration start/end; end null means there is no narration audio yet) and status: "
        "ready usable; video_missing — the duration is a placeholder using the arranged duration for now; "
        "unit_deleted — skipped at render time, duration counted as 0. "
        "Each issue carries code, severity (blocking blocks rendering; warning and info do not), "
        "applies_to (all, or with_narration: only affects the version with narration) and the related clip_ids / "
        "unit_id: "
        "video_missing — the video unit has no usable video yet; unit_deleted — the video unit the clip references "
        "has been deleted from the script; "
        "unit_unused — a video unit in the script is not in this timeline; trim_ignored — the video version the trim "
        "is based on is no longer current, "
        "so the full video is used at render time for now; hold_too_long — a single clip's freeze-frame extension "
        "exceeds 2 seconds. "
        "Three narration-related ones are reported only in TTS narration projects: narration_missing — a voice-over "
        "unit has no narration audio yet (blocking, only blocks the version with narration); "
        "narration_overrun — the narration runs into the next narration (params.cause=next_narration, clip_ids are "
        "the two carrying clips) "
        "or past the end of the timeline (cause=timeline_end); narration_source_collision — the narration extends "
        "onto a dialogue clip (cause=dialogue) "
        "or onto a clip whose source volume is above 0.3 (cause=source_volume); clip_ids are the carrying clip and "
        "the covered clip. "
        "subtitle_missing_glyphs (warning) — the video unit's subtitles contain characters the bundled subtitle font "
        "lacks; params.characters lists them, "
        "and they may not display correctly once burned into the final cut. "
        "bgm is ordered by start; each BGM clip carries id (e.g. b2), bgm_id and name (the referenced BGM), start, "
        "end (the actual end time after clipping "
        "to the timeline end), source_in / source_out (which section of the BGM is used), volume, fade_in and "
        "fade_out (the fades actually in effect); "
        "bgm_missing (blocking) means the BGM a BGM clip references is no longer in the project; clip_ids is that "
        "BGM clip."
    ),
    request_model=ReadTimelineRequest,
    migration=BLOCKED,
    domain_key="edit_timeline",
    handler=read_timeline,
    summary=timeline_readout_summary,
)

EDIT_TIMELINE = ToolDeclaration(
    name="edit_timeline",
    description=(
        "Modify an edit timeline with a batch of operations addressed by clip ID; the whole batch is committed "
        "atomically as one new revision with a change summary. "
        "base_revision is taken from read_timeline's revision and the whole batch is interpreted against it; "
        "operations run in order, each seeing the result of the previous one. "
        "If any operation is invalid the whole batch has no effect and operation_invalid is returned, with params "
        "carrying operation_index (from 0), clip_id, field "
        "and allowed (the valid value range). When base_revision is no longer the latest revision: if none of the "
        "clips this batch touches has been changed since, "
        "it is applied to the latest revision as usual, and the result's concurrent_revisions lists the revisions in "
        "between; otherwise revision_conflict is returned "
        "with latest_revision and conflicting_clip_ids — re-read, then edit again. "
        "Operations: insert creates a clip, its ID is assigned by the server and returned in the result, the same "
        "video unit can be inserted multiple times, "
        "and when source_volume is not given it is 0.3 for voice-over units and 1.0 for dialogue and voiceless "
        "units; delete; move; "
        "set_trim sets in/out points against the current video version — after a version change the trim is voided "
        "and the full video is used for now; set_volume; set_hold; "
        "set_reason; set_transition; place_narration. All times are in seconds with at most three decimals; the "
        "result has the server-normalized values. "
        "A transition hangs on the earlier clip and means the transition to the next clip; it does not change the "
        "total duration: the window is centred on the cut, half on each side, "
        "and the sum of the transition durations on a clip's two sides cannot exceed twice its duration. insert, "
        "delete and move revert every cut whose adjacency changed to a hard cut, "
        "including the moved clip's own transition; if needed, set_transition again later in the same batch. "
        "A video unit's narration hangs on only one of its clips, starts at that clip's start, plays for the "
        "measured duration of the audio, and may extend over following clips: "
        "an inserted clip carries the narration when the unit has no narration-carrying clip yet; when the carrying "
        "clip is deleted, the narration moves to the unit's first remaining clip; "
        "place_narration moves the narration to the specified clip of a voice-over unit, and the unit's previous "
        "carrying clip is unloaded accordingly. "
        "BGM track: insert_bgm places a BGM listed by list_bgm, and the ID (e.g. b2) is assigned by the server; start "
        "is the absolute start on the timeline, "
        "source_in / source_out cut a section of the BGM (whole track when omitted), volume defaults to 0.25 when "
        "omitted, fade_in / fade_out default to 1 second each when omitted; "
        "set_bgm changes only the given fields; delete_bgm. BGM clips are placed by absolute time, and inserting, "
        "deleting or moving on the main track does not shift them. "
        "Only one track at any moment: when a BGM clip changed in this batch overlaps another BGM clip, or its start "
        "is not inside the timeline, operation_invalid is returned. "
        "The part beyond the end of the timeline is cut off at render time, with a 1-second fade-out at the cut; "
        "when fade-in plus fade-out exceed the clip's duration they are shortened proportionally. "
        "BGM loudness is normalized at upload; volume is a multiplier on top of that, rendered identically on both "
        "ends. "
        "The result contains only the new revision, a one-line confirmation message, the new state of the affected "
        "clips (fields as in read_timeline) and bgm, "
        "deleted_clip_ids (including BGM clips), the total duration and the updated issues; the whole timeline is "
        "not returned."
    ),
    request_model=EditTimelineRequest,
    migration=BLOCKED,
    domain_key="timeline_edit",
    handler=edit_timeline,
    summary=timeline_write_summary,
)

RENAME_TIMELINE = ToolDeclaration(
    name="rename_timeline",
    description=(
        "Change an edit timeline's display name; names must be unique within an episode (timeline_name_conflict). "
        "Only the name changes: the edit content, revision history and timeline ID stay the same, no new revision is "
        "created, and already rendered final cuts and JianYing drafts do not go stale because of it."
    ),
    request_model=RenameTimelineRequest,
    migration=BLOCKED,
    domain_key="edit_timeline_summary",
    handler=rename_timeline,
    summary=timeline_renamed_summary,
)

LIST_REVISIONS = ToolDeclaration(
    name="list_revisions",
    description=(
        "List an edit timeline's revision history, oldest to newest by revision number: number, parent, author "
        "(creator, arcreel_agent, "
        "external_agent), summary (change summary), created_at, clip_count, changed_clip_ids (clips changed by that "
        "revision; "
        "null when not recorded) and restored_from (for revisions produced by a rollback, the revision number it "
        "restored to). Read-only, no side effects; "
        "returns timeline_not_found when the ID does not exist. Before copying or rolling back to a revision, "
        "confirm the revision number here first."
    ),
    request_model=ListRevisionsRequest,
    migration=BLOCKED,
    domain_key="edit_timeline_revisions",
    handler=list_revisions,
    summary=revision_history_summary,
)

LIST_BGM = ToolDeclaration(
    name="list_bgm",
    description=(
        "List the BGM uploaded to the project: id (e.g. bgm-3f9a0c21; edit_timeline's insert_bgm references it by "
        "this), name and duration (seconds), "
        "in upload order. BGM belongs to the whole project and every episode's edit timelines can use it; uploading "
        "is done by the creator on the BGM track of the edit view; "
        "when the list is empty, ask the creator to upload first. Read-only, no side effects."
    ),
    request_model=ListBgmRequest,
    migration=BLOCKED,
    domain_key="bgm",
    handler=list_bgm,
    summary=bgm_list_summary,
)

RESTORE_REVISION = ToolDeclaration(
    name="restore_revision",
    description=(
        "Roll an edit timeline back to an older revision: appends a new revision with that revision's content "
        "(restored_from records the source); "
        "history is not rewritten, and the latest revision before the rollback can itself be rolled back to again. "
        "A rollback always acts on the latest revision and replaces the current content wholesale, "
        "so before rolling back, confirm the target revision and the current content with list_revisions and "
        "read_timeline. "
        "Returns revision_unchanged when the target revision's content equals the latest revision, and "
        "revision_not_found when the revision does not exist. "
        "A later edit_timeline based on a pre-rollback revision is checked for conflicts against the clips changed "
        "by the rollback. "
        "The result has the same shape as edit_timeline: new revision, message, affected clips (state after the "
        "rollback), "
        "deleted_clip_ids (clips that no longer exist after the rollback), duration and issues."
    ),
    request_model=RestoreRevisionRequest,
    migration=BLOCKED,
    domain_key="timeline_edit",
    handler=restore_revision,
    summary=timeline_write_summary,
)

EDIT_TIMELINE_TOOLS = (
    CREATE_TIMELINE,
    LIST_TIMELINES,
    READ_TIMELINE,
    EDIT_TIMELINE,
    RENAME_TIMELINE,
    LIST_REVISIONS,
    RESTORE_REVISION,
    LIST_BGM,
)

__all__ = [
    "CREATE_TIMELINE",
    "EDIT_TIMELINE",
    "EDIT_TIMELINE_TOOLS",
    "LIST_BGM",
    "LIST_REVISIONS",
    "LIST_TIMELINES",
    "READ_TIMELINE",
    "RENAME_TIMELINE",
    "RESTORE_REVISION",
]
