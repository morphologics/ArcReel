"""YouReel Editor saves kept in the studio ("Save to Studio")."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.services.presentation import timeline_editor_save
from server.services.presentation.timeline_editor_save import EditorSaveError, TimelineEditorSaveService

PROJECT_JSON = json.dumps({"id": "youreel-abc-r4", "name": "Episode 3"})


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TimelineEditorSaveService, Path]:
    project = tmp_path / "demo"
    (project / "videos").mkdir(parents=True)
    shot = project / "videos" / "scene_E3S01.mp4"
    shot.write_bytes(b"shot-one")

    class _Projects:
        def get_project_path(self, name: str) -> Path:
            return tmp_path / name

    class _Store:
        def __init__(self, projects: object, name: str) -> None:
            pass

        def find(self, timeline_id: str) -> SimpleNamespace:
            return SimpleNamespace(episode=3, id=timeline_id)

    monkeypatch.setattr(timeline_editor_save, "EditTimelineStore", _Store)
    service = TimelineEditorSaveService(_Projects())  # type: ignore[arg-type]  # a stand-in with only get_project_path

    shot_size = len(b"shot-one")

    class _Packs:
        async def media_file(self, project_name: str, timeline_id: str, asset_id: str) -> SimpleNamespace | None:
            # the pack serves one medium: the shot, under its stable id
            return SimpleNamespace(path=shot, size=shot_size, type="video/mp4") if asset_id == "mshot" else None

    service._packs = _Packs()  # type: ignore[assignment]  # a stand-in serving one pack medium
    return service, project / "renders" / "episode_3" / "tl-1" / "editor"


async def _body(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _save(*assets: dict) -> dict:
    return {"version": 1, "filename": "Episode-r4.oreelpack", "projectJson": PROJECT_JSON, "assets": list(assets),
            "revision": 4, "editorProjectId": "youreel-abc-r4", "name": "Episode 3"}


def test_studio_media_are_linked_in_and_the_save_reads_back(setup) -> None:
    service, folder = setup
    result = asyncio.run(service.save("demo", "tl-1", _save({"id": "mshot", "size": 8, "type": "video/mp4"})))
    assert result["assets"] == 1
    saved = folder / "media" / "mshot"
    assert saved.read_bytes() == b"shot-one"
    # a hard link to the project's file, not a second copy
    assert saved.stat().st_ino == (folder.parents[3] / "videos" / "scene_E3S01.mp4").stat().st_ino
    latest = asyncio.run(service.latest("demo", "tl-1"))
    assert latest is not None
    assert latest["projectJson"] == PROJECT_JSON
    assert latest["revision"] == 4
    assert latest["savedAt"] == result["savedAt"]
    medium = asyncio.run(service.medium("demo", "tl-1", "mshot"))
    assert medium is not None and medium.type == "video/mp4"


def test_media_the_studio_cant_supply_are_asked_for_then_uploaded(setup) -> None:
    service, folder = setup
    voice = {"id": "6f1c2a9e-0d1b-4c55-9a51-3f0e1d2c3b4a", "size": 5, "type": "audio/mpeg"}
    with pytest.raises(EditorSaveError) as refused:
        asyncio.run(service.save("demo", "tl-1", _save({"id": "mshot", "size": 8, "type": "video/mp4"}, voice)))
    assert refused.value.code == "media_missing"
    assert refused.value.details["missing"] == [voice["id"]]
    assert asyncio.run(service.latest("demo", "tl-1")) is None  # nothing kept
    assert asyncio.run(service.store_media("demo", "tl-1", voice["id"], _body(b"he", b"llo"))) == 5
    asyncio.run(service.save("demo", "tl-1", _save({"id": "mshot", "size": 8, "type": "video/mp4"}, voice)))
    assert (folder / "media" / voice["id"]).read_bytes() == b"hello"
    medium = asyncio.run(service.medium("demo", "tl-1", voice["id"]))
    assert medium is not None and medium.type == "audio/mpeg"


def test_each_save_keeps_the_one_before(setup) -> None:
    service, folder = setup
    asyncio.run(service.save("demo", "tl-1", _save()))
    second = _save()
    second["name"] = "Episode 3, later"
    asyncio.run(service.save("demo", "tl-1", second))
    assert json.loads((folder / "save.json").read_text())["name"] == "Episode 3, later"
    assert json.loads((folder / "save.previous.json").read_text())["name"] == "Episode 3"


@pytest.mark.parametrize(
    "body",
    [
        {"version": 2, "projectJson": "{}", "assets": []},
        {"version": 1, "projectJson": 5, "assets": []},
        {"version": 1, "projectJson": "{}", "assets": [{"id": "../x", "size": 1, "type": "video/mp4"}]},
        {"version": 1, "projectJson": "{}", "assets": [{"id": "a", "size": 0, "type": "video/mp4"}]},
        {"version": 1, "projectJson": "{}", "assets": [{"id": "a", "size": 1, "type": "x"}] * 2},
    ],
)
def test_malformed_saves_are_refused(setup, body: dict) -> None:
    service, _ = setup
    with pytest.raises(EditorSaveError) as refused:
        asyncio.run(service.save("demo", "tl-1", body))
    assert refused.value.code == "invalid_save"


def test_uploads_refuse_bad_ids_and_empty_bodies(setup) -> None:
    service, folder = setup
    with pytest.raises(EditorSaveError) as bad:
        asyncio.run(service.store_media("demo", "tl-1", "../escape", _body(b"x")))
    assert bad.value.code == "invalid_asset_id"
    with pytest.raises(EditorSaveError) as empty:
        asyncio.run(service.store_media("demo", "tl-1", "abc", _body()))
    assert empty.value.code == "media_empty"
    assert [p.name for p in (folder / "media").iterdir()] == []  # no partial left behind
    assert asyncio.run(service.medium("demo", "tl-1", "../escape")) is None
