"""Behaviour tests for the single source of truth of script_plan / episode paths.

Only the externally observable contract is tested: structured script_plan file name resolution, the boundary of also accepting old .md, episode script paths,
and the convergence invariant "registering a new content_mode in one place is covered by gate / web / Agent writes alike".

script_plan / episode 路径单一真相源的行为测试。

只测外部可观察契约：结构化 script_plan 文件名解析、旧版 .md 兼认边界、episode 剧本路径，
以及"新增 content_mode 登记一处即被 gate / web / Agent 写盘共同覆盖"这一收敛不变量。
"""

from __future__ import annotations

from pathlib import Path

from lib.episode import episode_paths
from lib.script import script_review
from server.routers import files


def test_script_plan_filename_by_content_mode():
    assert episode_paths.script_plan_filename("drama") == "script_plan_normalized_script.json"
    assert episode_paths.script_plan_filename("narration") == "script_plan_segments.json"
    # ad does not use a structured script_plan / ad 不走结构化 script_plan
    assert episode_paths.script_plan_filename("ad") is None
    assert episode_paths.script_plan_filename("unknown") is None


def test_script_plan_read_candidates_includes_legacy_md():
    assert episode_paths.script_plan_read_candidates("narration") == (
        "script_plan_segments.json",
        "script_plan_segments.md",
    )
    assert episode_paths.script_plan_read_candidates("drama") == (
        "script_plan_normalized_script.json",
        "script_plan_normalized_script.md",
    )
    assert episode_paths.script_plan_read_candidates("ad") == ()


def test_episode_script_paths():
    assert episode_paths.episode_script_filename(3) == "episode_3.json"
    assert episode_paths.episode_script_relpath(3) == "scripts/episode_3.json"


def test_episode_drafts_dir():
    assert episode_paths.episode_drafts_dir(Path("/p"), 2) == Path("/p/drafts/episode_2")


def test_new_content_mode_registered_once_covers_gate_and_web(monkeypatch, tmp_path):
    """Registering a new mode in one place in SCRIPT_PLAN_FILENAMES should make the gate path and the web stage files agree automatically.

    The episode's script progress (``script_status``) is derived by the project summary from the artifact state of script_plan and the final script, and what it probes
    is exactly the gate path here, so there is no longer a fourth independent candidate name table to keep in sync.

    在 SCRIPT_PLAN_FILENAMES 登记一处新模式，gate 路径与 web 阶段文件应自动一致。

    该集的脚本进度（``script_status``）由项目摘要按 script_plan 与正式脚本的产物态派生，探测的
    正是这里的 gate 路径，故不再有第四条独立的候选名表需要同步。
    """
    monkeypatch.setitem(episode_paths.SCRIPT_PLAN_FILENAMES, "docudrama", "script_plan_docu.json")

    # Content review: script_plan_path points at the registered structured file name / 内容确认：script_plan_path 指向登记的结构化文件名
    project = {"content_mode": "docudrama", "episodes": [{"episode": 1}]}
    gate_path = script_review.script_plan_path(tmp_path, project, 1)
    assert gate_path is not None
    assert gate_path == tmp_path / "drafts" / "episode_1" / "script_plan_docu.json"

    # Web draft read/write: _stage_files returns the same file name / web 草稿读写：_stage_files 返回同一文件名
    assert files._stage_files("docudrama") == {"script_plan": "script_plan_docu.json"}


def test_ad_has_no_structured_script_plan_across_web_and_gate(tmp_path):
    """ad does not use a structured script_plan: the web stage mapping is empty and the gate path resolves to None.

    ad 不走结构化 script_plan：web 阶段映射为空、gate 路径解析为 None。"""
    # Web draft read/write: ad does not fall into the drama file name by mistake and returns an empty mapping; ad takes precedence over generation_mode,
    # and with a reference_video stamp there is likewise no script_plan
    # web 草稿读写：ad 不误落 drama 文件名，返回空映射；ad 优先于 generation_mode，
    # 带 reference_video 戳同样无 script_plan
    assert files._stage_files("ad") == {}
    assert files._stage_files("ad", generation_mode="reference_video") == {}
    # gate: ad has no structured script_plan to probe, so the episode's script progress is never judged "segmented"
    # gate：ad 无结构化 script_plan 可探测，该集的脚本进度因此不会被判为"已分段"
    assert script_review.script_plan_path(tmp_path, {"content_mode": "ad", "episodes": [{"episode": 1}]}, 1) is None


def test_gate_only_json_and_web_also_md():
    """gate only accepts structured .json; web reads also accept the old .md (an existing semantic difference, see ADR 0041).

    gate 只认结构化 .json；web 读取兼认旧版 .md（既有语义差异，见 ADR 0041）。"""
    # gate's registry contains no .md at all / gate 的登记表不含任何 .md
    assert all(name.endswith(".json") for name in episode_paths.SCRIPT_PLAN_FILENAMES.values())
    # web read candidates include the old .md / web 读取候选含旧 .md
    assert "script_plan_segments.md" in episode_paths.script_plan_read_candidates("narration")
    assert "script_plan_normalized_script.md" in episode_paths.script_plan_read_candidates("drama")


def test_script_plan_path_follows_reference_video_across_content_modes(tmp_path):
    """rv is a generation_mode dimension across content_modes: once a narration/drama project has rv, its gate path moves to
    the rv-specific structured file name rather than its own content_mode's name; the artifact state probe of the episode's script progress follows the same path.

    rv 是跨 content_mode 的 generation_mode 维度：narration/drama 项目挂 rv 后，gate 路径都改落
    rv 专属结构化文件名，而非各自 content_mode 对应名——该集脚本进度的产物态探测同走这条路径。
    """
    for content_mode in ("narration", "drama"):
        project = {"content_mode": content_mode, "generation_mode": "reference_video", "episodes": [{"episode": 1}]}
        assert script_review.script_plan_path(tmp_path, project, 1) == (
            episode_paths.episode_drafts_dir(tmp_path, 1) / episode_paths.REFERENCE_VIDEO_SCRIPT_PLAN_FILENAME
        )
        # When the first split fails validation only a draft is produced and the formal file was never written: the draft has its own probe slot,
        # so the episode is still judged "segmented" rather than falling back to source review.
        # 首轮拆分未过校验时只产出草稿、正式文件从未写过：草稿另有探测位，
        # 该集因此仍被判为"已分段"，而不是退回源文审阅。
        assert script_review.script_plan_quarantine_path(tmp_path, project, 1) == (
            episode_paths.episode_drafts_dir(tmp_path, 1)
            / episode_paths.REFERENCE_VIDEO_SCRIPT_PLAN_QUARANTINE_FILENAME
        )

    # Projects without rv keep the existing content_mode file name, unaffected / 未挂 rv 的项目沿用 content_mode 既有文件名，不受影响
    assert script_review.script_plan_path(tmp_path, {"content_mode": "narration", "episodes": [{"episode": 1}]}, 1) == (
        episode_paths.episode_drafts_dir(tmp_path, 1) / episode_paths.script_plan_filename("narration")
    )
    # ad takes precedence over generation_mode: even with rv there is no structured script_plan (same measure as gate/web, see the previous test)
    # ad 优先于 generation_mode：即便挂 rv 也无结构化 script_plan（与 gate/web 同口径，见上一测试）
    assert (
        script_review.script_plan_path(
            tmp_path,
            {"content_mode": "ad", "generation_mode": "reference_video", "episodes": [{"episode": 1}]},
            1,
        )
        is None
    )
