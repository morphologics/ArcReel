"""Project trash tool declarations: delete an unreferenced file or a historical version (into the trash only), list deletion records, restore a deletion record.

Emptying the trash and permanent deletion are not declared: both are irreversible and are only offered to the creator in the file view (``docs/adr/0107``).
"""

from __future__ import annotations

from server.agent_toolset.declaration import BLOCKED, Exempt, ToolDeclaration
from server.media_tools.project_trash import (
    DeleteProjectFileRequest,
    RestoreTrashRecordRequest,
    delete_project_file,
    delete_project_file_summary,
    list_trash_records,
    restore_trash_record,
    restore_trash_record_summary,
)
from server.tool_runtime import NoArguments

DELETE_PROJECT_FILE = ToolDeclaration(
    name="delete_project_file",
    description=(
        "Move one unreferenced file or one non-current historical version (a snapshot under versions/) of the project into the "
        "project trash, where it becomes a deletion record; "
        "it can be restored with restore_trash_record within 30 days, and a historical version returns to the version history "
        "when restored. "
        "This is the Agent's only way to delete project files: the media directories and versions/ cannot be written or deleted "
        "directly. "
        "Before deleting, references are re-checked inside the project lock; a file referenced by project data, a script, the "
        "artifact manifest or version history "
        "returns file_view_delete_not_allowed, the current version returns version_delete_current, a source file not included in the "
        "whole source returns file_view_delete_source_in_episodes, "
        "and a missing file returns file_not_found. Only delete files the creator explicitly asked to delete; emptying the trash "
        "and permanent deletion can only be done by the creator in the file view. "
        "On success returns record (with id, files, deleted_at, size)."
    ),
    request_model=DeleteProjectFileRequest,
    migration=BLOCKED,
    domain_key="delete_project_file",
    handler=delete_project_file,
    summary=delete_project_file_summary,
)

LIST_TRASH_RECORDS = ToolDeclaration(
    name="list_trash_records",
    description=(
        "List the deletion records in the project trash, newest first: each has id, object (type and identity of the deleted "
        "object), files (in-project paths before deletion), "
        "deleted_at, origin (user = initiated by the creator, agent = initiated by the Agent) and size; total_size and "
        "retention_days are returned as well. "
        "Read-only, no side effects."
    ),
    request_model=NoArguments,
    migration=Exempt(
        "Read-only: deletion records live in the project private area; it neither reads nor writes the artifact manifest and "
        "issues no write credential; it must stay queryable when migration fails."
    ),
    domain_key="trash_records",
    handler=list_trash_records,
)

RESTORE_TRASH_RECORD = ToolDeclaration(
    name="restore_trash_record",
    description=(
        "Restore one deletion record. Restoring only adds, never overwrites: if the original path is free, the file goes back "
        "to it; if the original path already holds the resource's current file, "
        "the deleted file is added back as its historical version and the current version is unchanged; any other occupied path "
        "is renamed to `name (n).ext`. "
        "A record that does not exist (it may already have been restored or purged) returns trash_record_not_found. "
        "On success returns restored: each item has original (path before deletion), path (path after restoring) and as_history."
    ),
    request_model=RestoreTrashRecordRequest,
    migration=BLOCKED,
    domain_key="restore_trash_record",
    handler=restore_trash_record,
    summary=restore_trash_record_summary,
)

PROJECT_TRASH_TOOLS = (DELETE_PROJECT_FILE, LIST_TRASH_RECORDS, RESTORE_TRASH_RECORD)

__all__ = ["DELETE_PROJECT_FILE", "LIST_TRASH_RECORDS", "PROJECT_TRASH_TOOLS", "RESTORE_TRASH_RECORD"]
