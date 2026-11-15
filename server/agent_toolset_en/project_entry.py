"""Declarations for the project entry tools: listing and creating projects, uploading and editing source text.

``source/`` is a protected write path for the Agent; source text is written only through the two tools here: when a whole-source file is changed, the episode ledger is remapped according to the alignment before and after the change.
"""

from __future__ import annotations

from server.agent_toolset.declaration import BLOCKED, ToolDeclaration, UnscopedToolDeclaration
from server.tool_runtime import (
    CreateProjectToolRequest,
    EditSourceTextRequest,
    NoArguments,
    UploadSourceRequest,
    create_project,
    edit_source_text,
    list_projects,
    upload_source,
)

LIST_PROJECTS = UnscopedToolDeclaration(
    name="list_projects",
    description=(
        "List the ArcReel projects under the current project root that later tools can address, returning each project's name, title, content mode and generation mode. "
        "Directories missing project.json or that cannot be read are not listed. Read-only, no side effects."
    ),
    request_model=NoArguments,
    domain_key="projects",
    handler=list_projects,
)

CREATE_PROJECT = UnscopedToolDeclaration(
    name="create_project",
    description=(
        "Create an ArcReel project and write the complete project metadata (title, content mode, generation mode, aspect ratio, narration delivery, etc.), "
        "returning the normalized project name and the project.json content; later tools address the project by that name. "
        "Narration delivery defaults to post-production voiceover; when use_tts is chosen, an omitted TTS model, voice and speech rate are prefilled from the global defaults and, once written, become the project snapshot. "
        "When a project with the same name already exists it returns project_exists and does not overwrite. Ad projects (content_mode=ad) do not accept default_duration "
        "or grid storyboards, and target_duration and brief are only available for ad projects. If the metadata write fails, the project directory already created is rolled back."
    ),
    request_model=CreateProjectToolRequest,
    domain_key="project",
    handler=create_project,
)

UPLOAD_SOURCE = ToolDeclaration(
    name="upload_source",
    description=(
        "Normalize a piece of text source to UTF-8, write it into the project's source/ directory and register it, returning the project-relative path written, the detected original encoding "
        "and the chapter count. role=whole_source (the default) registers it as a whole-source file, appended to the end of the file list, for episode planning to split; only .txt / .md "
        "file names are accepted, and file names of the form episode_N.txt are reserved for episode files and will be refused; how a file with the same name is handled is decided by on_conflict. role=episode registers it as "
        "an episode with its own source: it allocates a new episode ID, writes source/episode_{episode ID}.txt, appends it to the end of the episode order and additionally returns the episode ID; episodes the user has split themselves "
        "are uploaded one by one this way. Drama projects use source_kind to mark whether this source is a novel (the default) or a finished screenplay written by the user; other content modes "
        "ignore this parameter. When on_conflict=replace overwrites a registered whole-source file, it keeps its position in the list, and the split-out episodes whose source range touches it are remapped according to the alignment of the old and new text; "
        "when split-out episodes are affected it does not write yet, and returns confirmation_required=true, impact (the list of affected episodes grouped by kind) "
        "and revision; tell the user the list truthfully, and after they confirm call again with the same revision. Refused when an episode to be removed or retired still has queued or running tasks. "
        "source/ is written only through this tool and edit_source_text. Writes project files."
    ),
    request_model=UploadSourceRequest,
    migration=BLOCKED,
    domain_key="source",
    handler=upload_source,
)

EDIT_SOURCE_TEXT = ToolDeclaration(
    name="edit_source_text",
    description=(
        "Modify registered source text: filename points to one whole-source file, episode_id points to the source of an episode that has its own source or has no source (an episode without source "
        "becomes one with its own source once written). The source of a split-out episode is derived from the whole source; to change it, change the whole-source file it lives in. replacements replaces by fragment, and each old_text "
        "must occur exactly once in the current source; text rewrites the whole thing. When a whole-source file is changed, the split-out episodes touching it are remapped according to the alignment before and after the change: episodes whose text is unchanged "
        "are only shifted, episodes whose text changed are marked \"Source re-planned\", and episodes deleted entirely are retired or removed; when split-out episodes are affected it does not write yet, and returns "
        "confirmation_required=true, impact (the list of affected episodes grouped by kind) and revision; tell the user the list truthfully, and after they confirm call again with the same "
        "revision. Refused when the file was changed outside ArcReel and the episode ledger has not yet been updated in the Web \"Episodes\" view. Deleting and reordering whole-"
        "source files is done by the user in the \"Episodes\" view. Writes project files."
    ),
    request_model=EditSourceTextRequest,
    migration=BLOCKED,
    domain_key="source_change",
    handler=edit_source_text,
)

PROJECT_ENTRY_TOOLS = (LIST_PROJECTS, CREATE_PROJECT, UPLOAD_SOURCE, EDIT_SOURCE_TEXT)

__all__ = [
    "CREATE_PROJECT",
    "EDIT_SOURCE_TEXT",
    "LIST_PROJECTS",
    "PROJECT_ENTRY_TOOLS",
    "UPLOAD_SOURCE",
]
