"""Project cover picker unit tests: verify the priority and robustness of the fallback chain.

项目封面选择器单测：验证 fallback 链的优先级与鲁棒性。"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lib.project.project_manager import ProjectManager
from server.services.project.project_cover import resolve_project_cover


def _mk_manager(scripts_by_file: dict[str, dict]) -> MagicMock:
    """Build a fake ProjectManager whose load_script looks up a table by file name; raises FileNotFoundError when missing.

    get_project_path raises FileNotFoundError, so cover URLs carry no version key and only the picking order is verified;
    version-key cases use ``_real_manager`` to look up a real project directory.

    构造 fake ProjectManager，load_script 按文件名查表返回；缺失则抛 FileNotFoundError。

    get_project_path 抛 FileNotFoundError，封面 URL 不带版本键，只验证挑选顺序；
    版本键的用例用 ``_real_manager`` 走真实的项目目录查找。
    """
    mgr = MagicMock()
    mgr.get_project_path.side_effect = FileNotFoundError("proj")

    def _load_script(_project_name: str, filename: str) -> dict:
        if filename in scripts_by_file:
            return scripts_by_file[filename]
        raise FileNotFoundError(filename)

    mgr.load_script.side_effect = _load_script
    return mgr


# The three shot skeletons of storyboard-to-video: (content_mode, entry array key). / 分镜图生视频的三种分镜骨架：(content_mode, 条目数组键)。
_STORYBOARD_SKELETONS = [
    pytest.param("narration", "segments", id="narration-segments"),
    pytest.param("drama", "scenes", id="drama-scenes"),
    pytest.param("ad", "shots", id="ad-shots"),
]


def test_returns_video_thumbnail_when_present_in_reference_mode():
    """Reference mode with a generated video: hits video_thumbnail, the highest priority.

    reference 模式已生成视频：命中 video_thumbnail 最高优先级。"""
    project = {
        "episodes": [{"script_file": "scripts/episode_1.json"}],
        "scenes": {"S": {"scene_sheet": "scenes/s.png"}},
    }
    scripts = {
        "scripts/episode_1.json": {
            "video_units": [
                {"generated_assets": {"video_thumbnail": "reference_videos/thumbnails/E1U1.jpg"}},
            ]
        }
    }
    url = resolve_project_cover(_mk_manager(scripts), "proj", project)
    assert url == "/api/v1/files/proj/reference_videos/thumbnails/E1U1.jpg"


@pytest.mark.parametrize(("content_mode", "items_key"), _STORYBOARD_SKELETONS)
def test_returns_video_thumbnail_in_storyboard_mode(content_mode, items_key):
    """Storyboard-to-video: entries of all three shot skeletons can hit video_thumbnail, without falling to the project-level scene image.

    分镜图生视频：三种分镜骨架的条目都能命中 video_thumbnail，不落到项目级场景图。"""
    project = {
        "episodes": [{"script_file": "scripts/episode_1.json"}],
        "scenes": {"S": {"scene_sheet": "scenes/s.png"}},
    }
    scripts = {
        "scripts/episode_1.json": {
            "content_mode": content_mode,
            items_key: [
                {"generated_assets": {"video_thumbnail": "thumbnails/scene_E1S1.jpg"}},
            ],
        }
    }
    url = resolve_project_cover(_mk_manager(scripts), "proj", project)
    assert url == "/api/v1/files/proj/thumbnails/scene_E1S1.jpg"


def test_video_thumbnail_beats_storyboard_image_across_all_episodes():
    """As long as any episode has a video_thumbnail, it beats the first episode's storyboard_image:
    the key contract of scanning in two passes (episode order does not lock in priority).

    只要任意一集有 video_thumbnail，胜过第一集的 storyboard_image ——
    分两趟扫的关键合同（episode 顺序不锁死优先级）。"""
    project = {
        "episodes": [
            {"script_file": "scripts/episode_1.json"},
            {"script_file": "scripts/episode_2.json"},
        ]
    }
    scripts = {
        "scripts/episode_1.json": {
            "segments": [{"generated_assets": {"storyboard_image": "storyboards/scene_E1S1_first.png"}}]
        },
        "scripts/episode_2.json": {
            "segments": [{"generated_assets": {"video_thumbnail": "thumbnails/scene_E2S1.jpg"}}]
        },
    }
    url = resolve_project_cover(_mk_manager(scripts), "proj", project)
    assert url == "/api/v1/files/proj/thumbnails/scene_E2S1.jpg"


@pytest.mark.parametrize(("content_mode", "items_key"), _STORYBOARD_SKELETONS)
def test_falls_back_to_storyboard_image_when_no_video_thumbnail(content_mode, items_key):
    """When none of the three shot skeletons has a video first frame, take the entry's storyboard_image, still ahead of the project-level scene image.

    三种分镜骨架都没有视频首帧时，取条目上的 storyboard_image，仍优先于项目级场景图。"""
    project = {
        "episodes": [{"script_file": "scripts/episode_1.json"}],
        "scenes": {"S": {"scene_sheet": "scenes/s.png"}},
    }
    scripts = {
        "scripts/episode_1.json": {
            "content_mode": content_mode,
            items_key: [
                {"generated_assets": {"storyboard_image": "storyboards/scene_E1S1_first.png"}},
            ],
        }
    }
    url = resolve_project_cover(_mk_manager(scripts), "proj", project)
    assert url == "/api/v1/files/proj/storyboards/scene_E1S1_first.png"


def test_reference_mode_without_generated_assets_falls_back_to_scene_sheet():
    """Reference-to-video with no video generated: use the first scene reference image as the cover.

    参考生视频未生成任何视频：用第一张场景参考图当封面。"""
    project = {
        "episodes": [{"script_file": "scripts/episode_1.json"}],
        "scenes": {"酒馆": {"scene_sheet": "scenes/酒馆.png"}},
        "characters": {"张三": {"character_sheet": "characters/张三.png"}},
    }
    scripts = {"scripts/episode_1.json": {"video_units": [{"generated_assets": {"status": "pending"}}]}}
    url = resolve_project_cover(_mk_manager(scripts), "proj", project)
    # scene takes priority over character / scene 优先于 character
    assert url == "/api/v1/files/proj/scenes/酒馆.png"


def test_tolerates_corrupt_generated_assets():
    """When generated_assets is non-dict bad data (such as a string) it is treated as missing, falling back to scene_sheet without raising.

    generated_assets 为非 dict 脏数据（如字符串）时按缺失处理，回退到 scene_sheet，不抛异常。"""
    project = {
        "episodes": [{"script_file": "scripts/episode_1.json"}],
        "scenes": {"酒馆": {"scene_sheet": "scenes/酒馆.png"}},
    }
    scripts = {"scripts/episode_1.json": {"video_units": [{"generated_assets": "corrupt"}]}}
    url = resolve_project_cover(_mk_manager(scripts), "proj", project)
    assert url == "/api/v1/files/proj/scenes/酒馆.png"


def test_tolerates_corrupt_item_arrays():
    """Bad data such as a non-list entry array, non-dict entries or non-string asset paths is skipped as missing, without raising and without masking real assets in the same script.

    条目数组非 list、条目非 dict、资产路径非字符串的脏数据按缺失跳过，不抛异常，也不遮蔽同一剧本里的真实资产。"""
    project = {
        "episodes": [{"script_file": "scripts/episode_1.json"}],
        "scenes": {"酒馆": {"scene_sheet": "scenes/酒馆.png"}},
    }
    scripts = {
        "scripts/episode_1.json": {
            "segments": {"E1S1": "corrupt"},
            "scenes": [
                "corrupt",
                None,
                {"generated_assets": {"video_thumbnail": 1}},
                {"generated_assets": {"video_thumbnail": "thumbnails/scene_E1S2.jpg"}},
            ],
            "shots": 0,
        }
    }
    url = resolve_project_cover(_mk_manager(scripts), "proj", project)
    assert url == "/api/v1/files/proj/thumbnails/scene_E1S2.jpg"


def test_falls_back_to_character_sheet_when_no_scenes():
    project = {
        "episodes": [],
        "characters": {"张三": {"character_sheet": "characters/张三.png"}},
    }
    url = resolve_project_cover(_mk_manager({}), "proj", project)
    assert url == "/api/v1/files/proj/characters/张三.png"


def test_returns_none_for_empty_project():
    url = resolve_project_cover(_mk_manager({}), "proj", {})
    assert url is None


def test_missing_script_file_does_not_break_fallback():
    """When scripts/episode_N.json is missing / corrupted, it should still reach the asset fallback.

    scripts/episode_N.json 缺失 / 损坏时仍应走到资产 fallback。"""
    project = {
        "episodes": [{"script_file": "scripts/episode_missing.json"}],
        "scenes": {"S": {"scene_sheet": "scenes/s.png"}},
    }
    url = resolve_project_cover(_mk_manager({}), "proj", project)
    assert url == "/api/v1/files/proj/scenes/s.png"


def test_episode_without_script_file_is_skipped():
    """An episode entry without a script_file key (script plan not finished): just skip it without error.

    episode 条目里没 script_file 键（脚本规划未完成）：跳过即可，不报错。"""
    project = {
        "episodes": [{"episode": 1}],
        "characters": {"X": {"character_sheet": "characters/x.png"}},
    }
    url = resolve_project_cover(_mk_manager({}), "proj", project)
    assert url == "/api/v1/files/proj/characters/x.png"


def test_preloaded_scripts_skips_manager_load():
    """When preloaded_scripts is passed and covers all episodes, manager.load_script should not be called again.

    This is the hot-path contract of list_projects: it shares one script load with the project summary projection,
    avoiding two JSON parses for cover + status.

    传入 preloaded_scripts 且覆盖所有 episode 时，不应再调用 manager.load_script。

    这是 list_projects 的 hot-path 合同：与项目摘要投影共用一份剧本加载，
    避免 cover + status 两次 JSON 解析。"""
    project = {
        "episodes": [
            {"script_file": "scripts/episode_1.json"},
            {"script_file": "scripts/episode_2.json"},
        ],
    }
    preloaded = {
        "scripts/episode_1.json": {"segments": []},
        "scripts/episode_2.json": {"segments": [{"generated_assets": {"video_thumbnail": "thumbnails/E2.jpg"}}]},
    }
    # Empty map: if cover did not use the preload, everything would be FileNotFoundError
    mgr = _mk_manager({})  # 空 map：若 cover 不走预加载，会全部 FileNotFoundError
    url = resolve_project_cover(mgr, "proj", project, preloaded_scripts=preloaded)
    assert url == "/api/v1/files/proj/thumbnails/E2.jpg"
    mgr.load_script.assert_not_called()


def test_preloaded_scripts_falls_back_to_manager_for_missing_entries():
    """Episodes not covered by preloaded_scripts fall back to manager.load_script, keeping the "best effort" contract.

    preloaded_scripts 未覆盖的集：回退 manager.load_script，保持"尽力而为"的合同。"""
    project = {
        "episodes": [
            {"script_file": "scripts/episode_1.json"},
            {"script_file": "scripts/episode_2.json"},
        ],
    }
    preloaded = {
        "scripts/episode_1.json": {"segments": []},
    }
    # The manager only provides episode_2: simulates "episode 1 preloaded, episode 2 needs fetching from source".
    # manager 仅提供 episode_2：模拟"1 集已预加载，2 集需回源"。
    mgr = _mk_manager(
        {"scripts/episode_2.json": {"segments": [{"generated_assets": {"video_thumbnail": "thumbnails/E2.jpg"}}]}}
    )
    url = resolve_project_cover(mgr, "proj", project, preloaded_scripts=preloaded)
    assert url == "/api/v1/files/proj/thumbnails/E2.jpg"
    # Preload hits episode_1: its load_script should not be triggered; episode_2 missing from preload: fetched from source once.
    # 预加载命中 episode_1：不应触发其 load_script；episode_2 missing from preload：回源一次。
    called_files = {call.args[1] for call in mgr.load_script.call_args_list}
    assert called_files == {"scripts/episode_2.json"}


def test_mixed_segments_and_video_units_do_not_shadow_each_other():
    """When status:pending empty-shell video_units are mixed into a storyboard-to-video script, the real
    video_thumbnail / storyboard_image in segments still take part in picking, not skipped down to scene_sheet.

    分镜图生视频剧本里混入 status:pending 空壳 video_units 时，segments 里的真实
    video_thumbnail / storyboard_image 仍参与挑选，不被跳过而退到 scene_sheet。"""
    project = {
        "episodes": [{"script_file": "scripts/episode_1.json"}],
        "scenes": {"选秀大殿": {"scene_sheet": "scenes/选秀大殿.png"}},
    }
    scripts = {
        "scripts/episode_1.json": {
            "segments": [
                {"generated_assets": {"storyboard_image": "storyboards/scene_E1S1.png"}},
                {"generated_assets": {"video_thumbnail": "thumbnails/scene_E1S1.jpg"}},
            ],
            "video_units": [
                {"unit_id": "E1U1", "generated_assets": {"status": "pending"}},
                {"unit_id": "E1U2", "generated_assets": {"status": "pending"}},
            ],
        }
    }
    url = resolve_project_cover(_mk_manager(scripts), "proj", project)
    assert url == "/api/v1/files/proj/thumbnails/scene_E1S1.jpg"


@pytest.mark.parametrize(
    "sheet_value",
    [None, "", 0],
)
def test_ignores_falsy_sheet_values(sheet_value):
    """scene_sheet/character_sheet may be None/empty string/number 0; all should be skipped, not mistakenly chosen.

    scene_sheet/character_sheet 可能是 None/空串/数字 0，都应被跳过不误选。"""
    project = {
        "episodes": [],
        "scenes": {"S": {"scene_sheet": sheet_value}},
        "characters": {"X": {"character_sheet": "characters/x.png"}},
    }
    url = resolve_project_cover(_mk_manager({}), "proj", project)
    assert url == "/api/v1/files/proj/characters/x.png"


def _real_manager(tmp_path: Path, *, create: str | None = "proj") -> ProjectManager:
    """A real ProjectManager with the data root under tmp_path; ``create`` gives the project names to create beforehand.

    数据根落在 tmp_path 的真实 ProjectManager；``create`` 给出要预先建好的项目名。"""
    manager = ProjectManager(tmp_path)
    if create is not None:
        manager.create_project(create)
    return manager


def test_cover_url_carries_mtime_version_key_when_file_exists(tmp_path: Path):
    """When the cover file exists the URL gets ?v=<mtime_ns>, and after regeneration (mtime changes) the URL changes too.

    封面文件在场时 URL 附 ?v=<mtime_ns>，重新生成（mtime 变化）后 URL 随之变化。"""
    project = {"scenes": {"酒馆": {"scene_sheet": "scenes/酒馆.png"}}}
    manager = _real_manager(tmp_path)
    sheet = manager.get_project_path("proj") / "scenes" / "酒馆.png"
    sheet.parent.mkdir(exist_ok=True)
    sheet.write_bytes(b"png")
    os.utime(sheet, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

    first = resolve_project_cover(manager, "proj", project)
    assert first == "/api/v1/files/proj/scenes/酒馆.png?v=1700000000000000000"

    os.utime(sheet, ns=(1_700_000_060_000_000_000, 1_700_000_060_000_000_000))
    assert resolve_project_cover(manager, "proj", project) != first


def test_cover_url_has_no_version_key_when_file_missing(tmp_path: Path):
    """When a cover path is registered but the file is not present, no version key is added and the URL is handed to the files route as is.

    登记了封面路径但文件不在场时不附版本键，URL 原样交给 files 路由处理。"""
    project = {"scenes": {"酒馆": {"scene_sheet": "scenes/酒馆.png"}}}
    url = resolve_project_cover(_real_manager(tmp_path), "proj", project)
    assert url == "/api/v1/files/proj/scenes/酒馆.png"


@pytest.mark.parametrize("project_name", ["ghost", "../escape"], ids=["missing-project", "traversal-name"])
def test_cover_url_has_no_version_key_when_project_lookup_fails(tmp_path: Path, project_name: str):
    """When the project directory cannot be found (nonexistent or invalid name), nothing is raised; the cover URL is given as usual without a version key.

    项目目录查不到（不存在或名称非法）时不抛异常，封面 URL 照常给出、不附版本键。"""
    project = {"scenes": {"酒馆": {"scene_sheet": "scenes/酒馆.png"}}}
    url = resolve_project_cover(_real_manager(tmp_path, create=None), project_name, project)
    assert url == f"/api/v1/files/{project_name}/scenes/酒馆.png"
