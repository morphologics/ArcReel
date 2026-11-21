"""Light reads (G10): one asset, one episode's shot list, the project list with covers; shapes, 404 codes and that
the shot list reads only the requested episode's script.

轻量读取（G10）：单个资产、一集的分镜列表、带封面的项目列表；响应形状、404 错误码，以及分镜列表只读所请求那一集的剧本。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from lib.artifacts.artifact_activation import register_current_artifact
from lib.artifacts.artifact_manifest import ArtifactKey
from lib.artifacts.version_manager import MANUAL_UPLOAD_VERSION_SOURCE, VersionManager
from lib.project.project_manager import ProjectManager
from server.error_handlers import register_error_handlers
from server.routers import project_reads, projects
from tests.auth_deps import AUTH_DEPENDENCIES, override_auth

PROJECT = "demo"


def _write(project_path: Path, relative: str, data: bytes = b"artifact") -> None:
    path = project_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _segment(segment_id: str, **assets) -> dict:
    return {
        "segment_id": segment_id,
        "duration_seconds": 4,
        "novel_text": "text",
        "characters_in_segment": [],
        "image_prompt": {"scene": "a hallway", "composition": {}},
        "video_prompt": {"action": "", "camera_motion": "Static"},
        "generated_assets": {"storyboard_image": None, "video_clip": None, **assets},
    }


def _write_script(pm: ProjectManager, episode: int, items: list[dict]) -> None:
    project_path = pm.get_project_path(PROJECT)
    script = {"episode": episode, "title": f"E{episode}", "content_mode": "narration", "segments": items}
    _write(project_path, f"scripts/episode_{episode}.json", json.dumps(script).encode())
    pm.add_episode(PROJECT, episode, f"E{episode}", f"scripts/episode_{episode}.json")


@pytest.fixture
def pm(tmp_path: Path) -> ProjectManager:
    manager = ProjectManager(tmp_path / "projects")
    manager.create_project(PROJECT)
    manager.create_project_metadata(PROJECT, "Demo", "", "narration")
    return manager


@pytest.fixture
async def http(pm: ProjectManager, monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(project_reads, "get_project_manager", lambda: pm)
    monkeypatch.setattr(projects, "get_project_manager", lambda: pm)
    app = FastAPI()
    register_error_handlers(app)
    override_auth(app)
    app.include_router(projects.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    app.include_router(project_reads.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


class TestAssetList:
    async def test_lists_every_type_with_sheet_and_derivative_count(self, http, pm):
        project_path = pm.get_project_path(PROJECT)
        pm.add_character(PROJECT, "Alice", "a brave girl " * 40)
        _write(project_path, "characters/Alice.png")
        pm.update_project_character_sheet(PROJECT, "Alice", "characters/Alice.png")
        register_current_artifact(project_path, ArtifactKey.asset_sheet("character", "Alice"))
        pm.update_project(
            PROJECT,
            lambda project: project["characters"]["Alice"].update(
                {"aliases": ["Al"], "derivatives": {"Armor": {"description": "armored"}}}
            ),
        )
        pm.add_scenes_batch(PROJECT, {"Hall": {"description": "a long hall"}})

        response = await http.get(f"/api/v1/projects/{PROJECT}/assets")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["project"] == PROJECT and body["currency"] == "registered"
        by_name = {(a["asset_type"], a["name"]): a for a in body["assets"]}
        alice = by_name[("character", "Alice")]
        assert alice["aliases"] == ["Al"]
        assert alice["derivatives"] == 1
        assert len(alice["description"]) == 280
        assert alice["sheet"]["status"] == "current"
        assert alice["sheet"]["file"]["path"] == "characters/Alice.png"
        hall = by_name[("scene", "Hall")]
        assert hall["sheet"]["status"] == "missing" and hall["derivatives"] == 0

    async def test_type_filter_and_unknown_project(self, http, pm):
        pm.add_character(PROJECT, "Alice", "x")
        pm.add_scenes_batch(PROJECT, {"Hall": {"description": "y"}})

        only = (await http.get(f"/api/v1/projects/{PROJECT}/assets", params={"type": "scene"})).json()
        assert [a["name"] for a in only["assets"]] == ["Hall"]

        missing = await http.get("/api/v1/projects/no-such-project/assets")
        assert missing.status_code == 404
        assert missing.json()["code"] == "project_not_found"


class TestAsset:
    async def test_character_with_registered_sheet_and_derivative(self, http, pm):
        project_path = pm.get_project_path(PROJECT)
        pm.add_character(PROJECT, "Alice", "a brave girl")
        _write(project_path, "characters/Alice.png")
        pm.update_project_character_sheet(PROJECT, "Alice", "characters/Alice.png")
        register_current_artifact(project_path, ArtifactKey.asset_sheet("character", "Alice"))
        pm.update_project(
            PROJECT,
            lambda project: project["characters"]["Alice"].update(
                {"derivatives": {"Armor": {"description": "armored"}}}
            ),
        )

        response = await http.get(f"/api/v1/projects/{PROJECT}/assets/character/Alice")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["asset_type"] == "character"
        assert body["name"] == "Alice"
        assert body["fields"]["description"] == "a brave girl"
        assert "character_sheet" not in body["fields"] and "derivatives" not in body["fields"]
        sheet = body["sheet"]
        assert sheet["status"] == "current"
        assert sheet["file"]["path"] == "characters/Alice.png"
        assert sheet["file"]["present"] is True
        assert sheet["file"]["url"] == f"/api/v1/files/{PROJECT}/characters/Alice.png?v={sheet['file']['version_key']}"
        missing = {"status": "missing", "file": None, "version": None, "versions": 0}
        assert body["derivatives"] == [{"name": "Armor", "description": "armored", "sheet": missing}]
        assert body["currency"] == "registered"

    async def test_unknown_asset_is_a_typed_404(self, http):
        response = await http.get(f"/api/v1/projects/{PROJECT}/assets/scene/Nowhere")

        assert response.status_code == 404
        assert response.json()["code"] == "project_scene_not_found"
        assert response.json()["params"] == {"name": "Nowhere"}

    async def test_unknown_project_and_type(self, http):
        missing = await http.get("/api/v1/projects/nope/assets/character/Alice")
        assert missing.status_code == 404
        assert missing.json()["code"] == "project_not_found"

        bad_type = await http.get(f"/api/v1/projects/{PROJECT}/assets/vehicle/Car")
        assert bad_type.status_code == 422


class TestEpisodeShots:
    async def test_items_in_order_with_status_versions_and_thumbnails(self, http, pm):
        project_path = pm.get_project_path(PROJECT)
        _write(project_path, "storyboards/scene_E1S01.png")
        _write(project_path, "thumbnails/scene_E1S01.jpg")
        _write(project_path, "videos/.E1S01.upload.mp4", b"video")
        VersionManager(project_path).commit_staged_version(
            "videos",
            "E1S01",
            "",
            staged_file=project_path / "videos/.E1S01.upload.mp4",
            current_file=project_path / "videos/scene_E1S01.mp4",
            source=MANUAL_UPLOAD_VERSION_SOURCE,
        )
        _write_script(
            pm,
            1,
            [
                _segment(
                    "E1S01",
                    storyboard_image="storyboards/scene_E1S01.png",
                    video_clip="videos/scene_E1S01.mp4",
                    video_thumbnail="thumbnails/scene_E1S01.jpg",
                ),
                _segment("E1S02"),
            ],
        )

        response = await http.get(f"/api/v1/projects/{PROJECT}/episodes/1/shots")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["skeleton"] == "segments"
        assert body["total_duration_seconds"] == 8
        assert [shot["id"] for shot in body["shots"]] == ["E1S01", "E1S02"]
        first, second = body["shots"]
        assert first["number"] == 1 and first["kind"] == "segment"
        assert first["storyboard"]["file"]["present"] is True
        # Not registered in the manifest: present on disk but not a usable artifact. / 未在清单登记：在盘但不算可用产物。
        assert first["storyboard"]["status"] == "missing"
        assert first["video"]["version"] == 1 and first["video"]["versions"] == 1
        assert first["thumbnail"]["path"] == "thumbnails/scene_E1S01.jpg"
        assert first["prompts"] == {
            "has_image_prompt": True,
            "has_video_prompt": True,
            "image_prompt": None,
            "video_prompt": None,
            "text": None,
        }
        assert second["video"] == {"status": "missing", "file": None, "version": None, "versions": 0}
        assert second["thumbnail"] is None
        assert second["narration"] is None

    async def test_include_prompts(self, http, pm):
        _write_script(pm, 1, [_segment("E1S01")])

        response = await http.get(f"/api/v1/projects/{PROJECT}/episodes/1/shots", params={"include": "prompts"})

        prompts = response.json()["shots"][0]["prompts"]
        assert prompts["image_prompt"] == {"scene": "a hallway", "composition": {}}
        assert prompts["video_prompt"]["camera_motion"] == "Static"

    async def test_reads_only_the_requested_episode(self, http, pm, monkeypatch):
        _write_script(pm, 1, [_segment("E1S01")])
        _write_script(pm, 2, [_segment("E2S01")])
        loaded: list[str] = []
        original = pm.load_script_readonly

        def spy(project_name: str, filename: str) -> dict:
            loaded.append(filename)
            return original(project_name, filename)

        monkeypatch.setattr(pm, "load_script_readonly", spy)
        monkeypatch.setattr(pm, "load_script", lambda *args, **kwargs: pytest.fail("full script load"))

        response = await http.get(f"/api/v1/projects/{PROJECT}/episodes/2/shots")

        assert response.status_code == 200
        assert loaded == ["scripts/episode_2.json"]

    async def test_unknown_episode_is_a_typed_404(self, http, pm):
        _write_script(pm, 1, [_segment("E1S01")])

        response = await http.get(f"/api/v1/projects/{PROJECT}/episodes/9/shots")

        assert response.status_code == 404
        assert response.json()["code"] == "episode_not_found"
        assert response.json()["params"] == {"episode": 9}


class TestProjectCovers:
    async def test_light_list_with_cover_and_counts(self, http, pm):
        project_path = pm.get_project_path(PROJECT)
        _write(project_path, "storyboards/scene_E1S01.png")
        _write_script(pm, 1, [_segment("E1S01", storyboard_image="storyboards/scene_E1S01.png"), _segment("E1S02")])

        response = await http.get("/api/v1/projects", params={"covers": "1"})

        assert response.status_code == 200, response.text
        [item] = response.json()["projects"]
        assert item["name"] == PROJECT
        assert item["title"] == "Demo"
        assert item["episodes"] == 1
        assert item["shots"] == 2
        assert item["cover"].startswith(f"/api/v1/files/{PROJECT}/storyboards/scene_E1S01.png?v=")
        assert item["updated_at"]
        assert "status" not in item

    async def test_default_list_shape_is_unchanged(self, http):
        response = await http.get("/api/v1/projects")

        [item] = response.json()["projects"]
        assert {"name", "title", "thumbnail", "status", "last_activity_at"} <= set(item)
