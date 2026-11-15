"""Declarations for the repair channel tools: controlled writes that stay available when the project data upgrade fails, plus re-running the upgrade chain.

The write exemption here belongs only to the Agent tools' repair channel and does not extend to REST: routes that write script body content are still blocked by the migration.
"""

from __future__ import annotations

from server.agent_toolset.declaration import Exempt, ToolDeclaration
from server.tool_runtime import (
    EPISODE_META_FIELDS,
    PROJECT_OVERVIEW_FIELDS,
    PROJECT_SETTINGS,
    MergeAssetRequest,
    NoArguments,
    PatchEpisodeMetaRequest,
    PatchProjectRequest,
    RenameAssetRequest,
    merge_asset,
    patch_episode_meta,
    patch_project,
    rename_asset,
    retry_project_migration,
)

_REPAIR_WRITE = Exempt(
    "Repair channel: when the project data upgrade fails, the named episodes / files can only be fixed through controlled metadata writes before retrying the upgrade; "
    "writes are structurally validated and do not touch script body content."
)

PATCH_PROJECT = ToolDeclaration(
    name="patch_project",
    description=(
        "Add to or modify project.json, in exactly one of three forms; giving several or none is refused: (1) asset upsert (pass table + "
        "entries), upserts by asset table + name: a name that does not exist is added, one that exists has its fields merged; (2) top-level settings write (pass "
        f"settings), allowlisted fields {list(PROJECT_SETTINGS)}, a null value clears the field; (3) project overview edit (pass overview), "
        f"allowlisted fields {list(PROJECT_OVERVIEW_FIELDS)}, only the passed fields are changed, and the overview is created if it does not exist. If the structure is invalid after the write, nothing is written to disk "
        "and a problem is returned. Do not use this tool to rename assets (it would become \"new name created + old name left behind\"); use rename_asset instead. "
        "Still callable when the project data upgrade has failed, for repairs."
    ),
    request_model=PatchProjectRequest,
    migration=_REPAIR_WRITE,
    domain_key="project_patch",
    handler=patch_project,
)

PATCH_EPISODE_META = ToolDeclaration(
    name="patch_episode_meta",
    description=(
        f"Edit the top-level metadata fields of one episode's script (not shot level), allowlisted fields {list(EPISODE_META_FIELDS)}. The script's top-level "
        "title is the single source of truth for the episode title, and changes are mirrored automatically to the episode list in project.json. To change fields inside shots, use patch_episode_script. "
        "Still callable when the project data upgrade has failed, for repairs."
    ),
    request_model=PatchEpisodeMetaRequest,
    migration=_REPAIR_WRITE,
    domain_key="episode_meta_patch",
    handler=patch_episode_meta,
)

RENAME_ASSET = ToolDeclaration(
    name="rename_asset",
    description=(
        "Cascading asset rename: in one go, updates the asset table key, the name references in all episode scripts and script_plan drafts (reference arrays / speaker / "
        "@[名称] (name) mentions), and the associated files and version history named after it. If the new name conflicts with an existing asset in the same table, the whole operation is refused and nothing is written to disk. "
        "Renaming does not affect the global asset library. Always use this tool to rename assets, not patch_project. Still callable when the project data upgrade has failed, for repairs."
    ),
    request_model=RenameAssetRequest,
    migration=_REPAIR_WRITE,
    domain_key="asset_rename",
    handler=rename_asset,
)

MERGE_ASSET = ToolDeclaration(
    name="merge_asset",
    description=(
        "Merge two assets in the same table (characters / scenes / props) that were registered for the same identity: in all episode scripts and script_plan drafts, "
        "references pointing to source (reference arrays / speaker / @[名称] (name) in body text and visual descriptions) are repointed to target in one go. target is kept as is; "
        "source's name and aliases are recorded as aliases of target, derivatives under source move to target, and one with the same name as an existing target derivative is folded into that existing one. "
        "With as_derivative=true (characters only), source becomes a derivative of target instead: visual references are rewritten to target/source, "
        "speaker is changed to target, and source's name is not recorded as an alias. source's description, sheets and their version history, voice settings, reference images and reference audio "
        "are not merged in; they are moved to the project trash. The reference rewrites are not undone by a restore: first use dry_run=true to get the impact listed per episode, relay it to the user, and execute only after they agree. "
        "To rename assets use rename_asset. Still callable when the project data upgrade has failed, for repairs."
    ),
    request_model=MergeAssetRequest,
    migration=_REPAIR_WRITE,
    domain_key="asset_merge",
    handler=merge_asset,
)

RETRY_PROJECT_MIGRATION = ToolDeclaration(
    name="retry_project_migration",
    description=(
        "Re-run this project's data upgrade chain (including artifact backfill). When the upgrade fails the project is blocked, and the only write tools still available while blocked are patch_project / "
        "patch_episode_meta / rename_asset / merge_asset; patch_episode_script is always refused. Following the failure details, use these tools to fix the named episodes / "
        "files, then call this tool; for places they cannot change (such as violations in script body content), report the blocker to the user truthfully, and do not retry repeatedly. "
        "Idempotent: when already at the latest version it returns success directly. On success it returns the new workflow plan workflow_plan; on failure it returns a project_migration_failed "
        "problem, with structured details in params.details (episode / file / violation)."
    ),
    request_model=NoArguments,
    migration=Exempt("The repair entry point itself: re-running the upgrade chain is exactly the way to lift the migration block."),
    domain_key="migration_retry",
    handler=retry_project_migration,
)

REPAIR_CHANNEL_TOOLS = (PATCH_PROJECT, PATCH_EPISODE_META, RENAME_ASSET, MERGE_ASSET, RETRY_PROJECT_MIGRATION)

__all__ = [
    "MERGE_ASSET",
    "PATCH_EPISODE_META",
    "PATCH_PROJECT",
    "RENAME_ASSET",
    "REPAIR_CHANNEL_TOOLS",
    "RETRY_PROJECT_MIGRATION",
]
