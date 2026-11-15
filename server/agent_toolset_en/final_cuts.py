"""Final cut tool declaration: render an edit timeline into a final cut."""

from __future__ import annotations

from server.agent_toolset.declaration import BLOCKED, ToolDeclaration
from server.media_tools.final_cuts import (
    RenderFinalCutRequest,
    final_cut_projection,
    final_cut_summary,
    render_final_cut,
)

RENDER_FINAL_CUT = ToolDeclaration(
    name="render_final_cut",
    description=(
        "Render one edit timeline into a final cut: rendered locally with the bundled ffmpeg, free of charge, registered as an "
        "artifact with a download link. "
        "Only call it when the creator asks for the final video. The narration version (narration) and whether subtitles are "
        "burned in (burn_subtitles) determine the final cut's version; "
        "versions coexist; a version with narration mixes the narration audio into the whole episode's audio, and narration that "
        "overruns is rendered as is: overlapping parts play simultaneously, and anything past the end is cut off with the final cut. "
        "The BGM track is mixed into the whole episode's audio per clip; the volume is the loudness normalized at upload multiplied "
        "by the clip's volume, with fade-in and fade-out. "
        "Before submitting, the selected revision and version are checked, and the following cases are refused outright without "
        "queueing: a blocking-level issue applicable to that version (e.g. video_missing, "
        "plus narration_missing for a version with narration) returns final_cut_blocked, with params.issues listing those issues; "
        "requesting with_narration on a project without TTS narration returns "
        "final_cut_narration_unavailable; an unavailable bundled ffmpeg returns final_cut_ffmpeg_unavailable. "
        "When a video or narration audio version file read by the render task is corrupted, the task fails with "
        "final_cut_video_version_corrupted or "
        "final_cut_narration_version_corrupted, with unit_id and version in params. "
        "When revision is omitted, the latest revision at submission time is rendered: if the edit timeline is changed again "
        "during rendering, or an old revision is rendered explicitly, the final cut is stale as soon as it comes out, "
        "but can still be downloaded. Only the latest final cut is kept for each version of each edit timeline; rendering again "
        "overwrites it and increments version by one. "
        "The terminal result's final_cut carries revision, narration, subtitles, version, artifact_path, acceptance "
        "(expected_duration is the edit timeline duration, "
        "video_duration and audio_duration are the measured durations of the video and audio streams, in seconds) and warnings "
        "(warning-level issues); "
        "download_url is the download link."
    ),
    request_model=RenderFinalCutRequest,
    migration=BLOCKED,
    domain_key="final_cut",
    handler=render_final_cut,
    long_task=True,
    summary=final_cut_summary,
    projection=final_cut_projection,
)

FINAL_CUT_TOOLS = (RENDER_FINAL_CUT,)

__all__ = ["FINAL_CUT_TOOLS", "RENDER_FINAL_CUT"]
