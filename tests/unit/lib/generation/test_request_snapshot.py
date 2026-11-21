"""Request snapshot taken at enqueue (API G16): editable prompt, declared options and model, references with versions.

入队时取下的请求快照（API G16）：可编辑提示词、声明的选项与模型、带版本的参考。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib.generation.request_snapshot import (
    build_request_snapshot,
    episode_of_script_file,
    generation_resource_type,
    task_types_for_resource_type,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
PROJECT: dict[str, Any] = {
    "content_mode": "drama",
    "aspect_ratio": {"storyboards": "16:9", "videos": "16:9"},
    "characters": {
        "Ann": {"description": "A tall woman", "character_sheet": "characters/Ann.png", "reference_image": "src/a.png"}
    },
    "scenes": {"Kitchen": {"description": "A kitchen", "scene_sheet": "scenes/Kitchen.png"}},
    "props": {},
}
SCRIPT: dict[str, Any] = {
    "content_mode": "drama",
    "scenes": [
        {
            "scene_id": "E1S01",
            "image_prompt": {"scene": "@[Ann] in the @[Kitchen]"},
            "video_prompt": {"action": "She turns"},
            "characters_in_scene": ["Ann", "Ghost"],
            "scenes": ["Kitchen"],
            "props": [],
            "generated_assets": {"storyboard_image": "storyboards/scene_E1S01.png"},
        },
        {
            "scene_id": "E1S02",
            "image_prompt": {"scene": "Close on Ann"},
            "characters_in_scene": ["Ann"],
            "scenes": [],
            "props": [],
            "generated_assets": {},
        },
    ],
}


class _Projects:
    def __init__(self, root: Path) -> None:
        self.root = root

    def load_project(self, name: str) -> dict[str, Any]:
        return json.loads(json.dumps(PROJECT))

    def get_project_path(self, name: str) -> Path:
        return self.root

    def load_script(self, name: str, filename: str) -> dict[str, Any]:
        return json.loads(json.dumps(SCRIPT))


def _versions(root: Path) -> None:
    (root / "versions").mkdir(parents=True)
    (root / "versions" / "versions.json").write_text(
        json.dumps(
            {
                "characters": {"Ann": {"current_version": 3, "versions": [{"version": 3, "file": "v/Ann_v3.png"}]}},
                "storyboards": {
                    "E1S01": {
                        "current_version": 2,
                        "versions": [{"version": 2, "file": "v/E1S01_v2.png", "content_digest": "abc"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _snapshot(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "project_name": "demo",
        "resource_type": None,
        "script_file": "episode_1.json",
        "payload": {},
        "provider_id": "gemini",
        "now": NOW,
        **kwargs,
    }
    return build_request_snapshot(_Projects(tmp_path), **params)


def test_storyboard_snapshot_has_the_script_prompt_options_and_references_with_versions(tmp_path: Path) -> None:
    _versions(tmp_path)
    snapshot = _snapshot(
        tmp_path,
        task_type="storyboard",
        resource_id="E1S02",
        payload={"prompt": {"scene": "Close on Ann"}, "image_model": "m-1", "image_provider": "custom-1"},
    )

    assert snapshot["schema"] == 1
    assert snapshot["taken_at"] == NOW.isoformat()
    # The request prompt equals the script's: kept once. / 请求提示词与剧本一致：只记一次。
    assert snapshot["prompt"] == {
        "editable": {"scene": "Close on Ann"},
        "source": "script.image_prompt",
        "request": None,
    }
    assert snapshot["model"] == {"provider": "custom-1", "model": "m-1"}
    assert snapshot["options"] == {"aspect_ratio": "16:9"}
    by_role = {(ref["role"], ref["resource_id"]): ref for ref in snapshot["references"]}
    assert by_role[("asset_sheet", "Ann")] == {
        "role": "asset_sheet",
        "resource_type": "characters",
        "resource_id": "Ann",
        "file": "characters/Ann.png",
        "version": 3,
    }
    assert by_role[("previous_storyboard", "E1S01")]["version"] == 2
    assert by_role[("previous_storyboard", "E1S01")]["digest"] == "abc"


def test_unregistered_names_are_kept_and_marked(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, task_type="storyboard", resource_id="E1S01")
    ghost = next(ref for ref in snapshot["references"] if ref["resource_id"] == "Ghost")
    assert ghost["registered"] is False
    assert ghost["file"] is None


def test_video_snapshot_has_the_video_prompt_declared_options_and_start_frame(tmp_path: Path) -> None:
    _versions(tmp_path)
    snapshot = _snapshot(
        tmp_path,
        task_type="video",
        resource_id="E1S01",
        payload={"prompt": {"action": "She runs"}, "seed": 7, "duration_seconds": 4, "video_model_override": "v-2"},
    )

    assert snapshot["prompt"]["editable"] == {"action": "She turns"}
    # The request asked for another prompt than the script's: both are kept. / 请求的提示词与剧本不同：两者都记。
    assert snapshot["prompt"]["request"] == {"action": "She runs"}
    assert snapshot["options"] == {"seed": 7, "duration_seconds": 4, "aspect_ratio": "16:9"}
    assert snapshot["model"]["model"] == "v-2"
    [start] = snapshot["references"]
    assert start == {
        "role": "start_frame",
        "resource_type": "storyboards",
        "resource_id": "E1S01",
        "file": "storyboards/scene_E1S01.png",
        "version": 2,
        "digest": "abc",
    }


def test_asset_sheet_snapshot_has_the_description_and_original_images(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, task_type="character", resource_id="Ann", script_file=None)

    assert snapshot["prompt"] == {"editable": "A tall woman", "source": "asset.description", "request": None}
    assert snapshot["options"] == {"aspect_ratio": "16:9"}
    assert snapshot["references"] == [
        {
            "role": "original",
            "resource_type": None,
            "resource_id": "Ann",
            "file": "src/a.png",
            "asset_type": "character",
        }
    ]


def test_image_edit_snapshot_has_the_instruction_and_the_edited_image(tmp_path: Path) -> None:
    _versions(tmp_path)
    snapshot = _snapshot(
        tmp_path,
        task_type="image_edit",
        resource_id="Ann",
        resource_type="character",
        script_file=None,
        payload={"prompt": "make the coat red", "resource_type": "character"},
    )

    assert snapshot["prompt"] == {"editable": None, "source": None, "request": "make the coat red"}
    assert snapshot["references"] == [
        {
            "role": "edit_source",
            "resource_type": "characters",
            "resource_id": "Ann",
            "file": "characters/Ann.png",
            "version": 3,
        }
    ]


def test_resource_type_mapping_and_episode() -> None:
    assert generation_resource_type("storyboard", None) == "storyboards"
    assert generation_resource_type("image_edit", "prop") == "props"
    assert generation_resource_type("character_derivative", None) == "character_derivatives"
    assert generation_resource_type("text_episode_script", None) is None
    assert set(task_types_for_resource_type("characters")) == {"character"}
    assert episode_of_script_file("episode_12.json") == 12
    assert episode_of_script_file("scripts/episode_3.json") == 3
    assert episode_of_script_file(None) is None
