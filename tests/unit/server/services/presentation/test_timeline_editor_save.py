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


def _editor_project(markers: list[dict]) -> str:
    return json.dumps({"version": "1", "project": {
        "timeline": {
            "tracks": [{"id": "v1", "clips": [
                {"id": "c1", "mediaId": "mshot", "metadata": {"arcreel": {"shot_label": "Shot 4 · Hallway"}}},
                {"id": "c2", "mediaId": "mup"},
            ]}],
            "markers": markers,
        },
        "mediaLibrary": {"items": [{"id": "mshot", "name": "E3S04.mp4"}, {"id": "mup", "name": "upload.mov"}]},
    }})


def test_review_markers_list_the_markers_for_the_director_from_each_latest_save(tmp_path, monkeypatch) -> None:
    timelines = [SimpleNamespace(id="tl-a", episode=3, name="Full cut"), SimpleNamespace(id="tl-b", episode=4, name="Draft"),
                 SimpleNamespace(id="tl-c", episode=4, name="No save")]

    class _Projects:
        def get_project_path(self, name: str) -> Path:
            return tmp_path / name

    class _Store:
        def __init__(self, projects: object, name: str) -> None:
            pass

        def find(self, timeline_id: str) -> SimpleNamespace:
            return next(t for t in timelines if t.id == timeline_id)

        def list_documents(self, episode: int | None = None) -> list[SimpleNamespace]:
            return [t for t in timelines if episode is None or t.episode == episode]

    monkeypatch.setattr(timeline_editor_save, "EditTimelineStore", _Store)
    service = TimelineEditorSaveService(_Projects())  # type: ignore[arg-type]  # a stand-in with only get_project_path
    star = {"id": "m1", "time": 21.1, "label": "Shot 4 · Hallway_0:21.1", "color": "#ef4444", "episode": "3 · Title",
            "clipId": "c1", "forDirector": True}
    local = {"id": "m2", "time": 5, "label": "note", "color": "#38bdf8", "clipId": "c2"}
    later = {"id": "m3", "time": 2, "label": "check", "color": "#a855f7", "forDirector": True}
    for timeline, markers, revision in (("tl-a", [star, local], 7), ("tl-b", [later], 2)):
        folder = tmp_path / "demo" / "renders" / f"episode_{3 if timeline == 'tl-a' else 4}" / timeline / "editor"
        folder.mkdir(parents=True)
        (folder / "save.json").write_text(json.dumps({"projectJson": _editor_project(markers), "revision": revision,
                                                       "savedAt": 1700000000000}), encoding="utf-8")

    found = asyncio.run(service.review_markers("demo"))
    assert [m["id"] for m in found] == ["m1", "m3"]  # the Director's only, in episode then time order
    first = found[0]
    assert first["tag"] == "3 · Title_Shot 4 · Hallway_0:21.1"
    assert first["clip_name"] == "Shot 4 · Hallway"
    assert (first["timeline_id"], first["episode"], first["revision"], first["for_director"]) == ("tl-a", 3, 7, True)
    assert found[1]["tag"] == "check" and found[1]["clip_name"] is None

    with_local = asyncio.run(service.review_markers("demo", include_local=True))
    assert [m["id"] for m in with_local] == ["m2", "m1", "m3"]
    assert with_local[0]["clip_name"] == "upload.mov" and with_local[0]["for_director"] is False

    assert [m["id"] for m in asyncio.run(service.review_markers("demo", episode=4))] == ["m3"]


def test_review_markers_skip_an_unreadable_save(tmp_path, monkeypatch) -> None:
    class _Projects:
        def get_project_path(self, name: str) -> Path:
            return tmp_path / name

    class _Store:
        def __init__(self, projects: object, name: str) -> None:
            pass

        def find(self, timeline_id: str) -> SimpleNamespace:
            return SimpleNamespace(id=timeline_id, episode=1, name="x")

        def list_documents(self, episode: int | None = None) -> list[SimpleNamespace]:
            return [SimpleNamespace(id="tl-x", episode=1, name="x")]

    monkeypatch.setattr(timeline_editor_save, "EditTimelineStore", _Store)
    service = TimelineEditorSaveService(_Projects())  # type: ignore[arg-type]  # a stand-in with only get_project_path
    folder = tmp_path / "demo" / "renders" / "episode_1" / "tl-x" / "editor"
    folder.mkdir(parents=True)
    (folder / "save.json").write_text(json.dumps({"projectJson": "{not json"}), encoding="utf-8")
    assert asyncio.run(service.review_markers("demo")) == []
