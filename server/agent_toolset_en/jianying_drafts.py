"""JianYing draft tool declaration: export an edit timeline as a JianYing draft."""

from __future__ import annotations

from server.agent_toolset.declaration import BLOCKED, ToolDeclaration
from server.media_tools.jianying_drafts import (
    ExportJianyingDraftRequest,
    export_jianying_draft,
    jianying_draft_projection,
    jianying_draft_summary,
)

EXPORT_JIANYING_DRAFT = ToolDeclaration(
    name="export_jianying_draft",
    description=(
        "Export one edit timeline as a JianYing draft: generated locally, free of charge, registered as an artifact; the creator "
        "fills in the local JianYing draft directory on the Web and then downloads it. "
        "Only call it when the creator asks to export a JianYing draft. The draft has one video track and a subtitle track, and a "
        "version with narration additionally has a narration track; when narration or subtitles overlap each other, "
        "an extra track is opened so that nothing overlaps within a track, and added subtitle tracks are moved up as a whole. "
        "Trims, original audio volume, freeze-frame extensions and transitions are all written per the edit timeline; "
        "when there is BGM, there is an additional BGM track, with volume (the loudness normalized at upload multiplied by the "
        "clip's volume) and fade-in / fade-out written into the clips. "
        "Before submitting, the selected revision and narration version are checked, and the following cases are refused "
        "outright without queueing: a blocking-level issue applicable to that version (e.g. video_missing) "
        "returns jianying_draft_blocked, with params.issues listing those issues; no exportable clips returns "
        "jianying_draft_empty; requesting with_narration on a project without TTS narration returns "
        "jianying_draft_narration_unavailable. "
        "When revision is omitted, the latest revision at submission time is exported: if the edit timeline is changed again "
        "during export, or an old revision is exported explicitly, the draft is stale as soon as it comes out, but can still be "
        "downloaded. "
        "Only the latest draft is kept for each narration version of each edit timeline; exporting again overwrites it and "
        "increments version by one. "
        "The terminal result's jianying_draft carries revision, narration, version, artifact_path, duration (edit timeline "
        "duration, in seconds) "
        "and warnings (warning-level issues)."
    ),
    request_model=ExportJianyingDraftRequest,
    migration=BLOCKED,
    domain_key="jianying_draft",
    handler=export_jianying_draft,
    long_task=True,
    summary=jianying_draft_summary,
    projection=jianying_draft_projection,
)

JIANYING_DRAFT_TOOLS = (EXPORT_JIANYING_DRAFT,)

__all__ = ["EXPORT_JIANYING_DRAFT", "JIANYING_DRAFT_TOOLS"]
