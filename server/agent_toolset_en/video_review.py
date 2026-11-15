"""Declaration for the footage review tool: builds contact sheets for video units and hands them to the Agent as image content blocks."""

from __future__ import annotations

from lib.video_review.contact_sheet import MAX_FRAMES_PER_SHEET
from server.agent_toolset.declaration import BLOCKED, ToolDeclaration
from server.media_tools.video_review import (
    MAX_FRAMES_PER_CALL,
    MAX_FRAMES_PER_UNIT,
    InspectVideoUnitsRequest,
    inspect_video_units,
    inspect_video_units_images,
    inspect_video_units_projection,
    inspect_video_units_summary,
)

INSPECT_VIDEO_UNITS = ToolDeclaration(
    name="inspect_video_units",
    description=(
        "Look at the footage of video units: for one video version of each video unit, sample frames and assemble them into contact sheets, attached after the result as image content blocks. "
        "Read-only, free of charge. Use it to judge whether footage is usable, where it breaks down, which candidate version is better, and which frame the in and out points fall on. "
        f"Each contact sheet holds at most {MAX_FRAMES_PER_SHEET} frames, with the video unit ID and version number at the top; below each frame are the video unit ID and that frame's start time, "
        "in seconds, with the video's first frame as 0, on the same time axis as the in/out points of read_timeline. When a unit's frames exceed one sheet's capacity they are spread over several sheets. "
        "version applies to every unit in the list, and when omitted each unit's current version is used; to compare candidate versions of the same unit, call once per version. "
        f"The frame budget per call is {MAX_FRAMES_PER_CALL}: when number of units × frames exceeds it, it is divided evenly by the number of units, and the resulting per-unit budget is in frame_budget_per_unit in the result; "
        f"more than {MAX_FRAMES_PER_CALL} units returns frame_budget_exceeded; call in batches. "
        "Beyond the budget there is one raising rule: when a unit's shot count exceeds the per-unit budget, that unit's frame count is raised to the shot count, so the actual total frames of one call can exceed the budget; "
        "for each unit's actual frame count see frame_count in units, and for the actual total frames of all units see total_frames; these are authoritative. "
        "The first time each video version is looked at, the server computes three kinds of signals locally with the bundled ffmpeg and caches them per version, reusing them afterwards: "
        "BLACK is a black-screen segment, CUT is a shot change point, and FREEZE is a frozen segment where the picture does not move (black-screen segments are not counted again). "
        f"A unit has at most {MAX_FRAMES_PER_UNIT} frames: when the shot count does not exceed it, sampling guarantees at least one frame per shot and samples more densely on both sides of signals; "
        "when the shot count exceeds it, frames are picked evenly across shots, and some shots get no frame. Frames that hit a signal are marked CUT / BLACK / FREEZE below the time. "
        "Meaning and use of the three signals: BLACK and FREEZE are suspected defects; first look at the picture to confirm whether it is a generation failure, a freeze frame, or the creator's intentional black or still frame; "
        "when cutting off a bad segment, the interval's start is the candidate out point (removing a bad segment at the end), and end is the candidate in point (removing a bad segment at the start). "
        "CUT is structural information, not a defect: a change point inside a shot is where the new shot's first frame starts, and the frame before the cut is the previous shot's last frame; "
        "use this to set in/out points to frame level, avoiding putting them in the middle of a change. Signals are hints only; they never automatically trim or discard footage, and the final call is yours, judged against the picture. "
        "units in the result are in request order, and each item carries version, frame_count, available_versions (all video version numbers the unit currently has; when there is more than one, the others are candidate versions), status, sheets, and signals when ok. "
        "status is ok; video_missing means the unit has no usable video or that version is missing its file; "
        "video_unreadable means the video cannot be decoded, with detail explaining why. "
        "In each sheets item, image is the attached image index (starting from 1), times are the frame times, and marked_frames lists the times and marks of frames that hit a signal. "
        "signals contains duration_seconds (video duration), black and freeze ({start, end} intervals, in seconds, start inclusive and end exclusive), "
        "cuts (change point times) and shots (shot count, equal to the number of cuts plus 1). "
        "model_review is a reserved field and is always null. "
        "A nonexistent video unit returns video_unit_not_found, with params.unit_ids listing the nonexistent IDs; a nonexistent specified version returns version_not_found, "
        "with params.available_versions listing the unit's existing versions."
    ),
    request_model=InspectVideoUnitsRequest,
    migration=BLOCKED,
    domain_key="inspect_video_units",
    handler=inspect_video_units,
    summary=inspect_video_units_summary,
    projection=inspect_video_units_projection,
    images=inspect_video_units_images,
)

VIDEO_REVIEW_TOOLS = (INSPECT_VIDEO_UNITS,)

__all__ = ["INSPECT_VIDEO_UNITS", "VIDEO_REVIEW_TOOLS"]
