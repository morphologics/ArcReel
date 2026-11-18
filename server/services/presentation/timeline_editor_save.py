"""Editor saves: the YouReel Editor's own project of an edit timeline, kept in the studio (owner 2026-11-18, "Save to
Studio").

The editor (the OpenReel fork the studio opens in its own tab) holds its projects only in the member's browser; a
cleared browser or another device loses the edits, and the studio's backups never see them. A save stores the editor's
project as it is, in the pack's own manifest format (``{version: 1, projectJson, assets: [{id, size, type}]}`` plus
``filename``, ``revision``, ``savedAt``, ``editorProjectId``, ``name``), so opening it again is the same code path as
opening a pack. It is not a "Send to Studio": nothing is mapped into the studio's timeline model.

Layout, beside the timeline's renders (so a deleted timeline takes its saves into the project trash and a restore
brings them back, ``lib/edit_timeline/trash.py``)::

    renders/episode_N/<timeline>/editor/save.json            the latest save
    renders/episode_N/<timeline>/editor/save.previous.json   the one before (replaced by each save, never deleted)
    renders/episode_N/<timeline>/editor/media/<asset id>     the media the saves use

Nothing here is ever deleted by a save (project data leaves only through the project trash, ADR 0107): a medium no
longer used stays until the timeline goes. Only media added in the editor are new files; studio media are links.

A medium the studio itself serves in the timeline's pack (stable pack ids) is hard-linked from the project's file (no
second copy on disk; still there after the shot is regenerated or the file moved to versions/), or copied when it is a
pack-made file (still frames are made fresh for each pack). Media added in the editor (uploads, generated speech) are
uploaded first (``store_media``). A save naming a medium the studio can't supply answers with the missing ids; the
editor uploads those and saves again.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.artifacts.rendered_artifact import timeline_renders_dir
from lib.edit_timeline.store import EditTimelineStore
from lib.infra.json_io import atomic_write_json
from lib.project.project_manager import ProjectManager
from server.services.presentation.timeline_oreelpack import TimelineOreelPackService

EDITOR_DIRNAME = "editor"
#: one medium uploaded from the editor (a long recording is the large case)
MAX_MEDIA_BYTES = 2 * 1024**3
#: the project JSON of one save
MAX_PROJECT_JSON = 32 * 1024 * 1024
#: media ids: the pack's (``m`` + hex) and the editor's own (UUIDs)
_ASSET_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CHUNK = 1024 * 1024


class EditorSaveError(Exception):
    """A save the studio refuses; ``code`` names why (the router maps it to a status)."""

    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class SavedMedium:
    path: Path
    type: str


def valid_asset_id(asset_id: str) -> bool:
    return bool(_ASSET_ID.match(asset_id))


class TimelineEditorSaveService:
    def __init__(self, projects: ProjectManager) -> None:
        self._projects = projects
        self._packs = TimelineOreelPackService(projects)

    def _editor_dir(self, project_name: str, timeline_id: str) -> Path:
        """The timeline's editor folder (raises the timeline's own not-found error for an unknown timeline)."""
        document = EditTimelineStore(self._projects, project_name).find(timeline_id)
        project_dir = self._projects.get_project_path(project_name)
        return project_dir / timeline_renders_dir(document.episode, document.id) / EDITOR_DIRNAME

    async def latest(self, project_name: str, timeline_id: str) -> dict[str, Any] | None:
        folder = await asyncio.to_thread(self._editor_dir, project_name, timeline_id)
        path = folder / "save.json"

        def read() -> dict[str, Any] | None:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None

        return await asyncio.to_thread(read)

    async def medium(self, project_name: str, timeline_id: str, asset_id: str) -> SavedMedium | None:
        """A medium the saves use; None when there is no such file."""
        if not valid_asset_id(asset_id):
            return None
        folder = await asyncio.to_thread(self._editor_dir, project_name, timeline_id)
        path = folder / "media" / asset_id
        if not await asyncio.to_thread(path.is_file):
            return None
        return SavedMedium(path=path, type=await asyncio.to_thread(self._type_of, folder, asset_id))

    @staticmethod
    def _type_of(folder: Path, asset_id: str) -> str:
        """The medium's content type, from the saves that list it (newest first)."""
        for save in (folder / "save.json", folder / "save.previous.json"):
            try:
                assets = json.loads(save.read_text(encoding="utf-8")).get("assets") or []
            except (OSError, ValueError):
                continue
            for asset in assets:
                if asset.get("id") == asset_id and isinstance(asset.get("type"), str):
                    return asset["type"]
        return "application/octet-stream"

    async def store_media(self, project_name: str, timeline_id: str, asset_id: str, body: AsyncIterator[bytes]) -> int:
        """Store a medium added in the editor (an upload streamed as the request body); returns its size."""
        if not valid_asset_id(asset_id):
            raise EditorSaveError("invalid_asset_id")
        folder = await asyncio.to_thread(self._editor_dir, project_name, timeline_id)
        media = folder / "media"
        await asyncio.to_thread(media.mkdir, parents=True, exist_ok=True)
        partial = media / f".upload-{asset_id}-{time.monotonic_ns()}"
        size = 0
        handle = await asyncio.to_thread(partial.open, "wb")
        try:
            async for chunk in body:
                size += len(chunk)
                if size > MAX_MEDIA_BYTES:
                    raise EditorSaveError("media_too_large", limit=MAX_MEDIA_BYTES)
                await asyncio.to_thread(handle.write, chunk)
        except BaseException:
            await asyncio.to_thread(handle.close)
            await asyncio.to_thread(partial.unlink, True)  # hard-delete: rollback -- an interrupted upload
            raise
        await asyncio.to_thread(handle.close)
        if size == 0:
            await asyncio.to_thread(partial.unlink, True)  # hard-delete: rollback -- an empty upload
            raise EditorSaveError("media_empty")
        await asyncio.to_thread(os.replace, partial, media / asset_id)
        return size

    async def save(self, project_name: str, timeline_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Keep a save: every medium it names must be stored already, or be one the timeline's pack serves (linked in
        now). Otherwise ``EditorSaveError("media_missing", missing=[…])`` and nothing changes."""
        project_json = body.get("projectJson")
        assets = body.get("assets")
        if body.get("version") != 1 or not isinstance(project_json, str) or not isinstance(assets, list):
            raise EditorSaveError("invalid_save")
        if len(project_json) > MAX_PROJECT_JSON:
            raise EditorSaveError("save_too_large", limit=MAX_PROJECT_JSON)
        clean_assets: list[dict[str, Any]] = []
        for asset in assets:
            if (
                not isinstance(asset, dict)
                or not isinstance(asset.get("id"), str)
                or not valid_asset_id(asset["id"])
                or not isinstance(asset.get("size"), int)
                or asset["size"] <= 0
                or not isinstance(asset.get("type"), str)
            ):
                raise EditorSaveError("invalid_save")
            clean_assets.append({"id": asset["id"], "size": asset["size"], "type": asset["type"][:100]})
        if len({a["id"] for a in clean_assets}) != len(clean_assets):
            raise EditorSaveError("invalid_save")

        folder = await asyncio.to_thread(self._editor_dir, project_name, timeline_id)
        media = folder / "media"
        await asyncio.to_thread(media.mkdir, parents=True, exist_ok=True)
        missing: list[str] = []
        for asset in clean_assets:
            target = media / asset["id"]
            if await asyncio.to_thread(_size_of, target) == asset["size"]:
                continue
            served = await self._packs.media_file(project_name, timeline_id, asset["id"])
            if served is None or served.size != asset["size"]:
                missing.append(asset["id"])
                continue
            await asyncio.to_thread(_link_or_copy, served.path, target)
        if missing:
            raise EditorSaveError("media_missing", missing=missing)

        saved_at = int(time.time() * 1000)
        record = {
            "version": 1,
            "filename": str(body.get("filename") or "")[:200],
            "projectJson": project_json,
            "assets": clean_assets,
            "revision": body.get("revision") if isinstance(body.get("revision"), int) else None,
            "editorProjectId": str(body.get("editorProjectId") or "")[:200],
            "name": str(body.get("name") or "")[:200],
            "savedAt": saved_at,
        }
        await asyncio.to_thread(_keep, folder, record)
        return {"savedAt": saved_at, "assets": len(clean_assets)}


def _size_of(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _link_or_copy(source: Path, target: Path) -> None:
    """A hard link to the project's file (no second copy; it outlives the original's move or replacement), a copy
    where a link can't be made (another file system: the pack's own still frames)."""
    partial = target.with_name(f".link-{target.name}-{time.monotonic_ns()}")
    try:
        os.link(source, partial)
    except OSError:
        shutil.copyfile(source, partial)
    os.replace(partial, target)


def _keep(folder: Path, record: dict[str, Any]) -> None:
    """Write the latest save; the previous latest becomes ``save.previous.json`` (replacing the one before)."""
    latest = folder / "save.json"
    if latest.is_file():
        os.replace(latest, folder / "save.previous.json")
    atomic_write_json(latest, record)
