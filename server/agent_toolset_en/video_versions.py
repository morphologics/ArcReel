"""Video version selection tool declaration: switch the current video version of a video unit."""

from __future__ import annotations

from server.agent_toolset.declaration import BLOCKED, ToolDeclaration
from server.media_tools.video_versions import (
    SelectVideoVersionRequest,
    select_video_version,
    select_video_version_summary,
)

SELECT_VIDEO_VERSION = ToolDeclaration(
    name="select_video_version",
    description=(
        "Switch a video unit's current video version to the given version number (the same service as version restore on the Web). "
        "current is the unit's single answer across the whole project; unit preview, edit and export all take it, and the switch "
        "takes effect immediately. "
        "Free of charge, no regeneration, and no creator confirmation needed. "
        "Two uses: (1) after the first review round, another candidate version of a unit is better, so switch to it; "
        "(2) on acceptance after regeneration, the new version is worse than the old one, so switch back to the old version. "
        "unit_id is the shot ID for storyboard-to-video, and the unit_id for reference-to-video. "
        "A nonexistent video unit returns video_unit_not_found; a nonexistent version number returns version_not_found, "
        "with params.available_versions listing the unit's existing versions; a version lacking a complete provenance "
        "description (e.g. a manual upload) returns request_invalid. "
        "On success returns current_version and file_path."
    ),
    request_model=SelectVideoVersionRequest,
    migration=BLOCKED,
    domain_key="select_video_version",
    handler=select_video_version,
    summary=select_video_version_summary,
)

VIDEO_VERSION_TOOLS = (SELECT_VIDEO_VERSION,)

__all__ = ["SELECT_VIDEO_VERSION", "VIDEO_VERSION_TOOLS"]
