"""Declarations for the project content read tools."""

from __future__ import annotations

from server.agent_toolset.declaration import READ_CHECK, Exempt, ToolDeclaration
from server.tool_runtime import (
    EpisodeScriptRequest,
    NoArguments,
    ProjectFileRequest,
    ScriptPlanContentRequest,
    SourceTextRequest,
    get_episode_script,
    get_project_content,
    get_script_plan_content,
    get_source_text,
    list_project_files,
    list_source_files,
    read_project_file,
)

_READ_ONLY_FILE = Exempt("Read-only: fetches project files directly by path, without going through the artifact manifest and without issuing write credentials; must stay readable when migration fails, for diagnosis and repair.")

GET_PROJECT_CONTENT = ToolDeclaration(
    name="get_project_content",
    description=(
        "Read the complete content of the project's project.json (title, content mode, generation mode, style, episode and asset settings, etc.) "
        "and its canonical revision. The order of episodes[] is the episode order, and each item's episode is the episode ID (an internal identifier, not the episode number). "
        "Read-only, no side effects."
    ),
    request_model=NoArguments,
    migration=_READ_ONLY_FILE,
    domain_key="project_content",
    handler=get_project_content,
)

LIST_SOURCE_FILES = ToolDeclaration(
    name="list_source_files",
    description=(
        "List the source text files under the project's source/ (UTF-8 .txt / .md), returning each file's project-relative path, byte size and etag, "
        "as well as the revision of the whole list. Read-only, no side effects. To read the body text use get_source_text."
    ),
    request_model=NoArguments,
    migration=_READ_ONLY_FILE,
    domain_key="source_files",
    handler=list_source_files,
)

GET_SOURCE_TEXT = ToolDeclaration(
    name="get_source_text",
    description=(
        "Read the full text of one UTF-8 source text file under source/, with its revision and etag. Read-only, no side effects. "
        "Paths outside source/, non-text files, symlinks and files over the size limit are refused."
    ),
    request_model=SourceTextRequest,
    migration=_READ_ONLY_FILE,
    domain_key="source_text",
    handler=get_source_text,
)

GET_EPISODE_SCRIPT = ToolDeclaration(
    name="get_episode_script",
    description=(
        "Read the JSON body of one episode's script and its canonical revision. Pass the returned revision as is as "
        "patch_episode_script's base_revision; once the script is changed the revision changes with it, and it must be read again. Read-only, no side effects. "
        "When the project data upgrade has failed, no revision is issued and a project_migration_failed problem is returned; after repairing according to its details, call "
        "retry_project_migration."
    ),
    request_model=EpisodeScriptRequest,
    migration=READ_CHECK,
    domain_key="episode_script",
    handler=get_episode_script,
)

GET_SCRIPT_PLAN_CONTENT = ToolDeclaration(
    name="get_script_plan_content",
    description=(
        "Read the body of the current formal script_plan of the specified episode (the intermediate state of script planning, located in drafts/episode_{episode ID}/) and its canonical "
        "revision. Read-only, no side effects. When the project does not use script_plan it returns script_plan_not_applicable; "
        "when the episode has no script_plan yet it returns file_not_found."
    ),
    request_model=ScriptPlanContentRequest,
    migration=_READ_ONLY_FILE,
    domain_key="script_plan_content",
    handler=get_script_plan_content,
)

LIST_PROJECT_FILES = ToolDeclaration(
    name="list_project_files",
    description=(
        "List the project business text files available for diagnostic reading: project.json, source/ source text, scripts/ scripts and the script_plan under drafts/episode_N/, "
        "returning path, byte size and etag, as well as the revision of the whole list. Sensitive files, symlinks and other paths "
        "are not listed. Read-only, no side effects."
    ),
    request_model=NoArguments,
    migration=_READ_ONLY_FILE,
    domain_key="project_files",
    handler=list_project_files,
)

READ_PROJECT_FILE = ToolDeclaration(
    name="read_project_file",
    description=(
        "Read the content of one project business text file with its revision and etag: JSON files return the parsed object, text files return a string. "
        "The readable scope matches list_project_files; sensitive files, symlinks, out-of-bounds paths and files over the size limit are refused. "
        "Read-only, no side effects. For files that have a dedicated read tool (get_project_content, get_source_text, get_episode_script, "
        "get_script_plan_content), prefer the dedicated tool."
    ),
    request_model=ProjectFileRequest,
    migration=_READ_ONLY_FILE,
    domain_key="project_file",
    handler=read_project_file,
)

CONTENT_READ_TOOLS = (
    GET_PROJECT_CONTENT,
    LIST_SOURCE_FILES,
    GET_SOURCE_TEXT,
    GET_EPISODE_SCRIPT,
    GET_SCRIPT_PLAN_CONTENT,
    LIST_PROJECT_FILES,
    READ_PROJECT_FILE,
)

__all__ = ["CONTENT_READ_TOOLS"]
