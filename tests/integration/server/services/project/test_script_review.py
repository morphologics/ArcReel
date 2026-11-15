"""Service-layer and pure-logic tests for script_plan->prompt_authoring content review.

Only externally observable behavior is tested: content review state transitions (script_plan produced ->
pending -> blocked -> confirm -> confirmed -> released), scope, waiting for review again after content edits,
and structural validation.

script_plan→prompt_authoring 内容确认的服务层与纯逻辑测试。

只测外部可观察行为：内容确认状态流转（script_plan 产出 → pending → 阻塞 → 确认 → confirmed → 放行）、
适用范围、内容编辑后重新等待确认、结构校验。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from lib.artifacts.artifact_activation import activate_artifact_target_state
from lib.artifacts.artifact_manifest import ArtifactKey, ArtifactManifestEntry, ProjectArtifactManifestAdapter
from lib.artifacts.version_manager import VersionManager
from lib.config.resolver import ConfigResolver
from lib.generation.video_request_facts import VideoRequestFactsFailure
from lib.i18n import _ as i18n_message
from lib.infra.json_io import atomic_write_json
from lib.project import project_trash
from lib.project.project_activity import ACTIVITY_FILENAME, record_project_activity, recorded_project_activity
from lib.project.project_manager import ProjectManager, find_episode
from lib.project.project_schema import CURRENT_PROJECT_SCHEMA_VERSION
from lib.project.resource_paths import resource_relative_path
from lib.script import script_review
from lib.script.draft_quarantine import (
    QUARANTINE_KIND_DRAMA_SCRIPT_PLAN,
    QUARANTINE_KIND_NARRATION_SCRIPT_PLAN,
    QUARANTINE_KIND_PROMPT_AUTHORING,
    QUARANTINE_KIND_SCRIPT_PLAN,
    clear_quarantine,
    quarantine_path,
    write_quarantine,
)
from lib.script.reference_video.draft_validation import DraftViolation
from server.agent_toolset.script_authoring import CONFIRM_SCRIPT_REVIEW, GENERATE_EPISODE_SCRIPT
from server.draft_workflow import DraftContext, DraftWorkflow
from server.services.project.script_review import ScriptReviewError, ScriptReviewService
from server.tool_runtime import TextGenerationResult
from tests.factories import make_video_request_facts, register_project_sources
from tests.fakes import FakeConfigResolver, use_artifact_registration
from tests.integration.server.agent_tool_support import ToolHarness, run_declared_tool


def _drama_script_plan() -> dict:
    return {
        "title": "第一集",
        "scenes": [
            {
                "scene_id": "E1S01",
                "duration_seconds": 8,
                "segment_break": False,
                "characters_in_scene": ["阿离"],
                "scenes": [],
                "props": [],
                "scene_description": "雨夜，阿离立于屋檐下",
                "utterances": [
                    {"kind": "voiceover", "speaker": None, "text": "三年后。"},
                    {"kind": "dialogue", "speaker": "阿离", "text": "你终于回来了。"},
                ],
                "source_text": "三年后，阿离立于屋檐下，轻声道：你终于回来了。",
            }
        ],
    }


def _admitted_drama_script_plan() -> dict:
    """Confirming converts, and the conversion passes speech admission: shots keep only dialogue lines, not mixed
    with voiceover.

    确认即转换，转换过发声准入：分镜只留台词，不与画外音混排。"""
    plan = _drama_script_plan()
    plan["scenes"][0]["utterances"] = [{"kind": "dialogue", "speaker": "阿离", "text": "你终于回来了。"}]
    return plan


def _narration_script_plan() -> dict:
    return {
        "episode": 1,
        "segments": [
            {
                "segment_id": "E1S01",
                "novel_text": "裴与出征后的第二年，送回一个襁褓中的婴儿。",
                "duration_seconds": 6,
                "segment_break": False,
                "characters_in_segment": ["裴与"],
                "scenes": [],
                "props": [],
            }
        ],
    }


def _rv_script_plan() -> dict:
    """Structured intermediate state of a reference_video script_plan (``script_plan_reference_units.json`` shape).

    The body text is the unit's single source of truth: reference images are derived from its ``@[name]`` at read
    time and not written to disk.

    reference_video script_plan 结构化中间态（``script_plan_reference_units.json`` 形状）。

    正文是单元的唯一内容真相：参考图由其中的 ``@[名称]`` 读时派生，不落盘。
    """
    return {
        "units": [
            {
                "unit_id": "E1U01",
                "text": "@[阿离] 立于屋檐下，望向雨幕。\n@[裴与] 策马自远方而来。",
                "duration_seconds": 8,
                "source_text": "阿离立在檐下看雨，裴与骑马远远而来。",
            }
        ],
    }


def _narrowed_rv_facts(supported_durations):
    """Effective duration options are 4/8 s, covering config narrowing and declared-set narrowing after a model
    switch.

    生效档位为 4/8 秒，覆盖配置收窄与换型号后声明全集收窄。"""
    return make_video_request_facts(
        route="reference_video",
        generation_type="i2v",
        supported_durations=supported_durations,
        allowed_durations=(4, 8),
    )


def _off_tier_rv_script_plan() -> dict:
    """The second unit's confirmed duration of 6 s falls outside the narrowed duration options.

    第二个单元的已确认时长 6 秒落在收窄后的档位之外。"""
    plan = _rv_script_plan()
    plan["units"][0]["duration_seconds"] = 4
    plan["units"].append({"unit_id": "E1U02", "text": "@[裴与] 策马而来。", "duration_seconds": 6})
    return plan


@pytest.fixture(autouse=True)
def unresolvable_video_request_facts(set_video_request_facts) -> None:
    """By default this module makes video request facts resolve no model: no DB access, and the system default
    model's tiers do not drift into assertions.

    Cases that need specific tiers use the ``video_request_facts`` fixture or call ``set_video_request_facts``
    again to override.

    本模块默认让视频请求事实解析不到型号：不碰 DB，也不让系统级默认模型的档位漂进断言。

    需要具体档位的用例用 ``video_request_facts`` fixture 或再调 ``set_video_request_facts`` 覆盖。
    """
    set_video_request_facts(VideoRequestFactsFailure("video_capability_unavailable", (("capability", "i2v"),)))


def _make_project(
    tmp_path: Path,
    content_mode: str,
    *,
    generation_mode: str | None = None,
) -> ProjectManager:
    """Build a test project; tiers are determined by the video request facts supplied by the case via
    ``set_video_request_facts``.

    建测试项目；档位由用例经 ``set_video_request_facts`` 供给的视频请求事实决定。"""
    pm = ProjectManager(tmp_path / "projects")
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Demo", "Anime", content_mode)
    pm.add_character("demo", "阿离", "少女")
    pm.add_character("demo", "裴与", "将军")
    pm.add_episode("demo", 1, "第一集", "scripts/episode_1.json")
    if generation_mode is not None:

        def _set_mode(p: dict) -> None:
            p["generation_mode"] = generation_mode

        pm.update_project("demo", _set_mode)
    return pm


def _service(pm: ProjectManager) -> ScriptReviewService:
    """Tier assertions of the content review panel and of the confirm conversion both read video request facts,
    which the case supplies; the injected resolver is only a placeholder.

    内容确认面板的档位与确认转换的档位断言都读视频请求事实，事实由用例供给；注入的解析器只是占位。"""
    return ScriptReviewService(pm, config_resolver=cast(ConfigResolver, FakeConfigResolver()))


def _register_script_plan(pm: ProjectManager) -> None:
    """The conversion reads the registered final script plan: add the source and activate the artifact manifest so
    the plan artifact is provable and registered.

    转换读的是已登记的正式脚本规划：补上源文并激活产物清单，让规划产物可证明、被登记。"""
    project_path = pm.get_project_path("demo")
    source = project_path / "source" / "episode_1.txt"
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("三年后，阿离立于屋檐下：你终于回来了。", encoding="utf-8")
    activate_artifact_target_state(project_path, bump_schema=False)


def _write_script_plan(pm: ProjectManager, content_mode: str, content: dict) -> Path:
    filename = "script_plan_normalized_script.json" if content_mode == "drama" else "script_plan_segments.json"
    drafts = pm.get_project_path("demo") / "drafts" / "episode_1"
    drafts.mkdir(parents=True, exist_ok=True)
    path = drafts / filename
    atomic_write_json(path, content)
    _register_script_plan(pm)
    return path


def _write_rv_script_plan(pm: ProjectManager, content: dict) -> Path:
    """Write the structured script_plan for reference_video (``script_plan_reference_units.json``).

    写出 reference_video 的结构化 script_plan（``script_plan_reference_units.json``）。"""
    drafts = pm.get_project_path("demo") / "drafts" / "episode_1"
    drafts.mkdir(parents=True, exist_ok=True)
    path = drafts / "script_plan_reference_units.json"
    atomic_write_json(path, content)
    _register_script_plan(pm)
    return path


def test_content_fingerprint_of_data_matches_path_based_fingerprint(tmp_path: Path):
    """For the same content, the fingerprint taken from the parsed object must equal the one taken from the file
    path - both share the same normalization logic, so callers can safely use the former instead of a second
    disk read "re-checking the path after loading into memory".

    对同一份内容，从已解析对象取的指纹须与对文件路径取的指纹相同——两者共用同一套
    规范化逻辑，调用方才能安全地用前者替代"读入内存后再对路径复核一次"的二次读盘。
    """
    path = tmp_path / "content.json"
    data = {"b": 2, "a": 1, "nested": {"z": [3, 2, 1]}}
    atomic_write_json(path, data)

    assert script_review.content_fingerprint_of_data(data) == script_review.content_fingerprint(path)


def _write_prompt_authoring(pm: ProjectManager) -> Path:
    """Write the prompt_authoring artifact (the generated script JSON), simulating "prompt_authoring already produced".

    写出 prompt_authoring 产物（生成的剧本 JSON），模拟「已产 prompt_authoring」。"""
    scripts = pm.get_project_path("demo") / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / "episode_1.json"
    atomic_write_json(path, {"title": "第一集", "scenes": []})
    return path


def _make_manual_split_project(tmp_path: Path, content_mode: str) -> ProjectManager:
    """Project without a whole source and with an empty ledger: episodes are built by the caller registering each
    one or writing files directly.

    没有整本源文、账本为空的项目：集由调用方逐集登记或直接写文件构造。"""
    pm = ProjectManager(tmp_path / "projects")
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Demo", "Anime", content_mode)
    return pm


def _write_source_text(pm: ProjectManager, filename: str, text: str) -> Path:
    source_dir = pm.get_project_path("demo") / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / filename
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# State transitions (drama)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 状态流转（drama）
# ---------------------------------------------------------------------------


def _set_episode_target_duration(pm: ProjectManager, seconds: int) -> None:
    def _set(project: dict) -> None:
        project["episode_target_duration"] = seconds

    pm.update_project("demo", _set)


class TestEpisodeTargetDuration:
    """The review panel's "episode total / target" comparison comes from state: target and total come from the
    same read.

    审核面板的「本集合计 / 目标」对比取自 state：目标与合计出自同一次读取。"""

    async def test_state_carries_the_project_target(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        _set_episode_target_duration(pm, 90)
        _write_script_plan(pm, "drama", _drama_script_plan())

        state = await _service(pm).get_state("demo", 1)

        assert state["episode_target_duration"] == 90

    async def test_state_reports_no_target_when_unset(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        _write_script_plan(pm, "drama", _drama_script_plan())

        state = await _service(pm).get_state("demo", 1)

        assert state["episode_target_duration"] is None

    async def test_out_of_range_value_degrades_to_no_target(self, tmp_path):
        """Dirty values from hand-editing project.json should not make the panel compare against an out-of-range target.

        手改 project.json 的脏值不应让面板拿着越界目标去对比。"""
        pm = _make_project(tmp_path, "drama")
        _set_episode_target_duration(pm, 5)
        _write_script_plan(pm, "drama", _drama_script_plan())

        state = await _service(pm).get_state("demo", 1)

        assert state["episode_target_duration"] is None


def _narration_script(*segments: dict, metadata: dict | None = None) -> dict:
    script: dict = {"episode": 1, "title": "第一集", "content_mode": "narration", "segments": list(segments)}
    if metadata is not None:
        script["metadata"] = metadata
    return script


def _narration_script_segment(segment_id: str, **overrides: object) -> dict:
    segment: dict = {
        "segment_id": segment_id,
        "duration_seconds": 6,
        "novel_text": "原文",
        "characters_in_segment": [],
        "scenes": [],
        "props": [],
        "image_prompt": "画面",
        "video_prompt": "动作",
        "generated_assets": {},
    }
    segment.update(overrides)
    return segment


def _write_script(pm: ProjectManager, script: dict) -> None:
    atomic_write_json(pm.get_project_path("demo") / "scripts" / "episode_1.json", script)


class TestDramaGateFlow:
    async def test_no_script_plan_then_pending_then_confirmed(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)

        # script_plan not produced
        # script_plan 未产出
        assert (await svc.get_state("demo", 1))["status"] == "no_script_plan"

        # script_plan produced -> reviewable intermediate state, blocked
        # script_plan 产出 → 可审中间态、阻塞
        _write_script_plan(pm, "drama", _admitted_drama_script_plan())
        state = await svc.get_state("demo", 1)
        assert state["status"] == "pending_review"
        assert state["content"]["scenes"][0]["scene_id"] == "E1S01"
        assert state["content"]["scenes"][0]["utterances"][0]["speaker"] == "阿离"
        project_path = pm.get_project_path("demo")
        project = pm.load_project("demo")
        assert script_review.review_status(project_path, project, 1) == "pending_review"

        # Confirm -> released
        # 确认 → 放行
        confirmed = await svc.confirm("demo", 1)
        assert confirmed["status"] == "confirmed"
        assert confirmed["confirmed_at"]
        project = pm.load_project("demo")
        assert script_review.review_status(project_path, project, 1) == "confirmed"

    async def test_quarantined_script_plan_blocks_confirm_and_prompt_authoring(self, tmp_path, video_request_facts):
        """drama drafts also block independently: while a draft exists confirmation is refused and prompt_authoring is
        blocked, even if the final script_plan was confirmed long ago - fetching it back for editing leaves the final
        file untouched, so looking only at the fingerprint would release the previous content the user has not seen.

        drama 的草稿同样独立阻塞：草稿在场期间确认被拒、prompt_authoring 被阻塞，即使正式 script_plan
        早已确认过——取回编辑时正式文件原封不动，只看指纹会放行用户尚未看过的上一版内容。"""
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        project_path = pm.get_project_path("demo")
        _write_script_plan(pm, "drama", _admitted_drama_script_plan())
        await svc.confirm("demo", 1)
        assert (await svc.get_state("demo", 1))["status"] == "confirmed"

        write_quarantine(
            project_path,
            1,
            QUARANTINE_KIND_DRAMA_SCRIPT_PLAN,
            content={"title": "第一集", "scenes": []},
            violations=[],
        )

        assert (await svc.get_state("demo", 1))["status"] == "pending_review"
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "pending_review"
        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)
        assert exc.value.code == "quarantined"

        clear_quarantine(project_path, 1, QUARANTINE_KIND_DRAMA_SCRIPT_PLAN)
        assert (await svc.get_state("demo", 1))["status"] == "confirmed"

    async def test_saving_confirmed_script_plan_is_rejected_without_writing(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        path = _write_script_plan(pm, "drama", _admitted_drama_script_plan())
        await svc.confirm("demo", 1)
        before = path.read_bytes()

        edited = _admitted_drama_script_plan()
        edited["scenes"][0]["scene_description"] = "雨势渐急，阿离仍站在屋檐下"
        with pytest.raises(ScriptReviewError) as exc:
            await svc.save_content("demo", 1, edited)

        assert exc.value.code == "script_plan_confirmed"
        assert path.read_bytes() == before
        assert (await svc.get_state("demo", 1))["status"] == "confirmed"

    async def test_confirmed_script_plan_stays_read_only_while_a_rerun_draft_is_pending(
        self, tmp_path, video_request_facts
    ):
        """When a script planning rerun leaves an invalid draft, the final script plan is still the confirmed one and
        still cannot be saved.

        重跑脚本规划留下待修复草稿时，正式脚本规划仍是已确认的那一份，照样不能保存。"""
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        path = _write_script_plan(pm, "drama", _admitted_drama_script_plan())
        await svc.confirm("demo", 1)
        write_quarantine(
            pm.get_project_path("demo"),
            1,
            QUARANTINE_KIND_DRAMA_SCRIPT_PLAN,
            content=_admitted_drama_script_plan(),
            violations=[DraftViolation("台词不在原文里")],
        )
        before = path.read_bytes()

        with pytest.raises(ScriptReviewError) as exc:
            await svc.save_content("demo", 1, _admitted_drama_script_plan())

        assert exc.value.code == "script_plan_confirmed"
        assert path.read_bytes() == before

    async def test_rerun_script_plan_after_confirm_repends_and_reopens_editing(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        _write_script_plan(pm, "drama", _admitted_drama_script_plan())
        await svc.confirm("demo", 1)

        rerun = _admitted_drama_script_plan()
        rerun["scenes"][0]["scene_description"] = "雨势渐急，阿离仍站在屋檐下"
        _write_script_plan(pm, "drama", rerun)
        assert (await svc.get_state("demo", 1))["status"] == "pending_review"

        edited = _admitted_drama_script_plan()
        edited["scenes"][0]["scene_description"] = "雨停了"
        state = await svc.save_content("demo", 1, edited)
        assert state["status"] == "pending_review"
        assert state["content"]["scenes"][0]["scene_description"] == "雨停了"

    async def test_legacy_mixed_scene_allows_metadata_edit_but_rejects_speech_edit_atomically(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        path = _write_script_plan(pm, "drama", _drama_script_plan())

        metadata_edit = _drama_script_plan()
        metadata_edit["scenes"][0]["scene_description"] = "雨势渐急，阿离仍站在屋檐下"
        await svc.save_content("demo", 1, metadata_edit)

        speech_edit = json.loads(json.dumps(metadata_edit, ensure_ascii=False))
        speech_edit["scenes"][0]["utterances"][1]["text"] = "别过来。"
        with pytest.raises(ScriptReviewError) as exc_info:
            await svc.save_content("demo", 1, speech_edit)

        assert exc_info.value.code == "speech_admission"
        assert exc_info.value.admission is not None
        assert exc_info.value.admission.problems[0].code == "mixed_speech"
        assert json.loads(path.read_text(encoding="utf-8")) == metadata_edit

    async def test_repairing_marked_drama_candidate_clears_replan_marker(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        candidate = _drama_script_plan()
        candidate["scenes"][0]["needs_replan"] = True
        path = _write_script_plan(pm, "drama", candidate)

        repaired = json.loads(json.dumps(candidate, ensure_ascii=False))
        repaired["scenes"][0]["utterances"] = [{"kind": "dialogue", "speaker": "阿离", "text": "你终于回来了。"}]
        await svc.save_content("demo", 1, repaired)

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert "needs_replan" not in saved["scenes"][0]

    async def test_metadata_edit_cannot_clear_drama_replan_marker(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        candidate = _drama_script_plan()
        candidate["scenes"][0]["needs_replan"] = True
        path = _write_script_plan(pm, "drama", candidate)

        metadata_edit = json.loads(json.dumps(candidate, ensure_ascii=False))
        metadata_edit["scenes"][0].pop("needs_replan")
        metadata_edit["scenes"][0]["scene_description"] = "雨势渐急，阿离仍站在屋檐下"
        await svc.save_content("demo", 1, metadata_edit)

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["scenes"][0]["needs_replan"] is True
        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)
        assert exc.value.code == "speech_admission"
        assert exc.value.admission is not None
        assert exc.value.admission.problems[0].code == "needs_replan"

    async def test_whitespace_reformat_keeps_confirmed(self, tmp_path, video_request_facts):
        """Pure key-order / whitespace reformatting does not change semantics -> fingerprint unchanged, stays confirmed.

        纯键序 / 空白重排不改语义 → 指纹不变、保持 confirmed。"""
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        path = _write_script_plan(pm, "drama", _admitted_drama_script_plan())
        await svc.confirm("demo", 1)

        # Rewrite the same content with different indentation / key order
        # 同内容、不同缩进 / 键序重写
        path.write_text(json.dumps(_admitted_drama_script_plan(), ensure_ascii=False, indent=4), encoding="utf-8")
        assert (await svc.get_state("demo", 1))["status"] == "confirmed"


# ---------------------------------------------------------------------------
# Content review confirms by converting the whole episode: create when there is no final script; when one
# exists, the overwrite must be acknowledged
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 内容确认即整集转换：无正式脚本时新建，已有正式脚本时须认可覆盖
# ---------------------------------------------------------------------------


async def _confirm_over_existing_script(pm: ProjectManager) -> dict:
    """When the episode already has a final script, acknowledge the overwrite by the overwrite listing's version
    and confirm.

    该集已有正式脚本时按覆盖清单的版本认可覆盖并确认。"""
    svc = _service(pm)
    revision = (await svc.get_state("demo", 1))["script_overwrite"]["revision"]
    return await svc.confirm("demo", 1, overwrite_revision=revision)


def _formal_script(pm: ProjectManager) -> dict:
    return json.loads((pm.get_project_path("demo") / "scripts" / "episode_1.json").read_text(encoding="utf-8"))


def _entry_claims(entry_id: str) -> dict[ArtifactKey, ArtifactManifestEntry]:
    digest = f"sha256-v1:{'a' * 64}"
    return {
        ArtifactKey.episode_storyboard(1, entry_id): ArtifactManifestEntry(
            artifact_path=f"storyboards/scene_{entry_id}.png", basis_digest=digest
        ),
        ArtifactKey.episode_video(1, entry_id): ArtifactManifestEntry(
            artifact_path=f"videos/scene_{entry_id}.mp4", basis_digest=digest
        ),
    }


class TestConfirmMaterializesScript:
    async def test_drama_confirm_writes_every_plan_entry_pending_authoring(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "drama")
        plan = _admitted_drama_script_plan()
        second = json.loads(json.dumps(plan["scenes"][0], ensure_ascii=False))
        second["scene_id"] = "E1S02"
        plan["scenes"].append(second)
        _write_script_plan(pm, "drama", plan)
        svc = _service(pm)

        assert (await svc.get_state("demo", 1))["script_overwrite"] is None
        state = await svc.confirm("demo", 1)

        assert state["status"] == "confirmed"
        script = _formal_script(pm)
        assert [scene["scene_id"] for scene in script["scenes"]] == ["E1S01", "E1S02"]
        for scene in script["scenes"]:
            assert scene["pending_authoring"] is True
            assert scene["image_prompt"] is None
            assert scene["video_prompt"] is None
        assert [scene["scene_description"] for scene in script["scenes"]] == [
            entry["scene_description"] for entry in plan["scenes"]
        ]
        assert script["scenes"][0]["utterances"] == plan["scenes"][0]["utterances"]

    async def test_narration_confirm_writes_every_plan_entry_pending_authoring(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())

        await _service(pm).confirm("demo", 1)

        [segment] = _formal_script(pm)["segments"]
        assert segment["segment_id"] == "E1S01"
        assert segment["novel_text"] == _narration_script_plan()["segments"][0]["novel_text"]
        assert segment["pending_authoring"] is True
        assert segment["image_prompt"] is None
        assert segment["video_prompt"] is None

    async def test_reference_confirm_takes_unit_text_and_duration_from_plan(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        _write_rv_script_plan(pm, _rv_script_plan())

        await _service(pm).confirm("demo", 1)

        [unit] = _formal_script(pm)["video_units"]
        planned = _rv_script_plan()["units"][0]
        assert unit["unit_id"] == "E1U1"
        assert unit["text"] == planned["text"]
        assert unit["duration_seconds"] == planned["duration_seconds"]
        assert unit["source_text"] == planned["source_text"]
        assert unit["pending_authoring"] is True

    async def test_existing_script_without_acknowledgement_is_refused_untouched(self, tmp_path):
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())
        _write_script(
            pm,
            _narration_script(
                _narration_script_segment("E1S01", generated_assets={"storyboard_image": "storyboards/a.png"}),
                _narration_script_segment("E1S09", generated_assets={"video_clip": "videos/b.mp4"}),
            ),
        )
        before = (pm.get_project_path("demo") / "scripts" / "episode_1.json").read_bytes()
        svc = _service(pm)
        expected_overwrite = {
            "revision": script_review.published_revision(script_review.content_fingerprint_of_data(json.loads(before))),
            "entries": [
                {
                    "id": "E1S01",
                    "has_storyboard": True,
                    "has_video": False,
                    "has_narration_audio": False,
                    "has_end_frame": False,
                    "grid_id": None,
                },
                {
                    "id": "E1S09",
                    "has_storyboard": False,
                    "has_video": True,
                    "has_narration_audio": False,
                    "has_end_frame": False,
                    "grid_id": None,
                },
            ],
            "storyboard_count": 1,
            "video_count": 1,
            "narration_audio_count": 0,
            "end_frame_count": 0,
            "grid_member_count": 0,
            "grid_count": 0,
        }

        assert (await svc.get_state("demo", 1))["script_overwrite"] == expected_overwrite
        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)

        assert exc.value.code == "overwrite_required"
        assert exc.value.overwrite == expected_overwrite
        assert (pm.get_project_path("demo") / "scripts" / "episode_1.json").read_bytes() == before
        assert script_review.stored_review(pm.load_project("demo"), 1) == {}

    @pytest.mark.parametrize("spelling", ["prefixed", "bare"])
    async def test_acknowledged_overwrite_replaces_script_and_forgets_old_entry_claims(
        self, tmp_path, video_request_facts, spelling: str
    ):
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())
        _write_script(pm, _narration_script(_narration_script_segment("E1S01"), _narration_script_segment("E1S09")))
        adapter = ProjectArtifactManifestAdapter(pm.get_project_path("demo"))
        old_claims = {**_entry_claims("E1S01"), **_entry_claims("E1S09")}
        for key, entry in old_claims.items():
            adapter.put_entry(key, entry)

        svc = _service(pm)
        revision = (await svc.get_state("demo", 1))["script_overwrite"]["revision"]

        assert revision.startswith("sha256-v1:")
        if spelling == "bare":
            revision = revision.removeprefix("sha256-v1:")

        state = await svc.confirm("demo", 1, overwrite_revision=revision)

        assert state["status"] == "confirmed"
        [segment] = _formal_script(pm)["segments"]
        # E1S01 in the plan was already used by the old script; on conversion it gets a new ID after the largest one
        # used in this episode
        # 规划里的 E1S01 已被旧剧本用过，转出时换成本集用过的最大号之后的新编号
        assert segment["segment_id"] == "E1S10"
        assert segment["pending_authoring"] is True
        assert segment["image_prompt"] is None
        snapshot = adapter.snapshot_entries()
        assert not old_claims.keys() & snapshot.keys()
        assert ArtifactKey.episode_script(1) in snapshot

    async def test_acknowledged_overwrite_moves_old_entries_to_the_project_trash(self, tmp_path, video_request_facts):
        """After overwriting, the old entries' media and version history move to the project trash: new entries get
        new IDs and do not inherit old history; removed entries are moved away too.

        覆盖后旧条目的媒体与版本历史移进项目回收站：新条目换新编号，不接旧历史；被移除的条目同样移走。"""
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())
        _write_script(pm, _narration_script(_narration_script_segment("E1S01"), _narration_script_segment("E1S09")))
        project_path = pm.get_project_path("demo")
        versions = VersionManager(project_path)
        for entry_id in ("E1S01", "E1S09"):
            for resource_type in ("storyboards", "end_frames", "videos", "audio"):
                current = project_path / resource_relative_path(resource_type, entry_id)
                current.parent.mkdir(parents=True, exist_ok=True)
                current.write_bytes(b"old-" + resource_type.encode())
                versions.add_version(resource_type, entry_id, "旧提示词", source_file=current)

        await _confirm_over_existing_script(pm)

        for entry_id in ("E1S01", "E1S09"):
            for resource_type in ("storyboards", "end_frames", "videos", "audio"):
                assert not versions.has_versions(resource_type, entry_id)
                assert not (project_path / resource_relative_path(resource_type, entry_id)).exists()
        records = {record.object["id"]: record for record in project_trash.list_records(project_path)}
        assert set(records) == {"E1S01", "E1S09"}
        assert "storyboards/scene_E1S09.png" in records["E1S09"].files

    async def test_acknowledged_overwrite_keeps_old_entries_without_media_recoverable(
        self, tmp_path, video_request_facts
    ):
        """On overwrite every old shot is handled as a structural removal: old text-only entries without generated
        media also become deletion records.

        覆盖时旧分镜都按结构移除处置：没有生成媒体、只有文字的旧条目同样成为删除记录。"""
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())
        _write_script(pm, _narration_script(_narration_script_segment("E1S01"), _narration_script_segment("E1S09")))
        project_path = pm.get_project_path("demo")

        await _confirm_over_existing_script(pm)

        records = {record.object["id"]: record for record in project_trash.list_records(project_path)}
        assert set(records) == {"E1S01", "E1S09"}
        assert all(record.has_snapshot for record in records.values())

    async def test_overwrite_is_refused_untouched_when_version_history_is_unreadable(
        self, tmp_path, video_request_facts
    ):
        """Moving old entries to the project trash is part of the overwrite: when the version records are unreadable,
        the whole overwrite writes nothing and the script and old media stay as they are.

        旧条目移进项目回收站是覆盖的一部分：版本记录读不出时整次覆盖不写入，剧本与旧媒体原样保留。"""
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())
        _write_script(pm, _narration_script(_narration_script_segment("E1S01")))
        project_path = pm.get_project_path("demo")
        current = project_path / resource_relative_path("storyboards", "E1S01")
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_bytes(b"old")
        (project_path / "versions").mkdir(exist_ok=True)
        (project_path / "versions" / "versions.json").write_text("{broken", encoding="utf-8")
        before = (project_path / "scripts" / "episode_1.json").read_bytes()

        with pytest.raises(ScriptReviewError):
            await _confirm_over_existing_script(pm)

        assert (project_path / "scripts" / "episode_1.json").read_bytes() == before
        assert current.read_bytes() == b"old"
        assert project_trash.list_records(project_path) == []

    async def test_acknowledgement_of_an_outdated_script_is_refused_with_the_current_listing(self, tmp_path):
        """An acknowledgement only matches the listed final script: if the final script changed after listing, a
        confirmation with the old version is refused again with the new listing.

        认可只对应被列出的那份正式脚本：列出之后正式脚本又有变化，带旧版本的确认按新清单再次拒绝。"""
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())
        _write_script(pm, _narration_script(_narration_script_segment("E1S01")))
        svc = _service(pm)
        listed_revision = (await svc.get_state("demo", 1))["script_overwrite"]["revision"]
        _write_script(
            pm,
            _narration_script(
                _narration_script_segment("E1S01", generated_assets={"video_clip": "videos/scene_E1S01.mp4"})
            ),
        )
        before = (pm.get_project_path("demo") / "scripts" / "episode_1.json").read_bytes()

        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1, overwrite_revision=listed_revision)

        assert exc.value.code == "overwrite_required"
        assert exc.value.overwrite is not None
        assert exc.value.overwrite["revision"] != listed_revision
        assert exc.value.overwrite["video_count"] == 1
        assert (pm.get_project_path("demo") / "scripts" / "episode_1.json").read_bytes() == before
        assert script_review.stored_review(pm.load_project("demo"), 1) == {}

    async def test_binding_lost_with_another_episode_at_the_canonical_path_is_refused(
        self, tmp_path, video_request_facts
    ):
        """The bound file is gone and the canonical path holds another episode's script: confirmation is refused before
        writing, and neither that episode's script nor this episode's binding is touched.

        Episodes skipped by the migration have exactly this shape. Falling back to the canonical path would make
        confirmation fully rebuild another episode's live script and bind this episode to it.

        绑定文件已不在、规范路径上是别集剧本：确认在写盘前被拒，那一集的剧本与本集绑定都不动。

        迁移跳过的集就是这个形态。回落到规范路径会让确认整份重建别集的在世剧本，并把本集绑上去。
        """
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())
        project_path = pm.get_project_path("demo")
        foreign = {**_narration_script(_narration_script_segment("E2S01")), "episode": 2}
        atomic_write_json(project_path / "scripts" / "episode_1.json", foreign)

        def _rebind(project: dict) -> None:
            find_episode(project, 1)["script_file"] = "scripts/custom.json"

        pm.update_project("demo", _rebind)
        svc = _service(pm)

        assert (await svc.get_state("demo", 1))["script_overwrite"] is None
        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)

        assert exc.value.code == "foreign_formal_script"
        assert exc.value.script_filename == "episode_1.json"
        assert json.loads((project_path / "scripts" / "episode_1.json").read_text(encoding="utf-8")) == foreign
        assert find_episode(pm.load_project("demo"), 1)["script_file"] == "scripts/custom.json"
        assert script_review.stored_review(pm.load_project("demo"), 1) == {}

    async def test_unresolvable_video_model_is_refused_with_a_configuration_hint(self, tmp_path):
        """The confirm conversion must determine shot duration options: when video request facts cannot be resolved,
        confirmation is refused, carrying the facts' problem code and params.

        确认转换要确定分镜时长档位：视频请求事实解析不出时拒绝确认，携带事实的问题码与参数。"""
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())
        svc = _service(pm)

        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)

        assert exc.value.code == "video_request_facts"
        assert exc.value.problem is not None
        assert exc.value.problem.code == "video_capability_unavailable"
        assert exc.value.problem.parameters() == {"capability": "i2v"}
        assert not (pm.get_project_path("demo") / "scripts" / "episode_1.json").exists()
        assert (await svc.get_state("demo", 1))["status"] == "pending_review"

    async def test_refused_conversion_records_no_confirmation(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        _write_script_plan(pm, "drama", _drama_script_plan())

        with pytest.raises(ScriptReviewError) as exc:
            await _service(pm).confirm("demo", 1)

        assert exc.value.code == "speech_admission"
        assert not (pm.get_project_path("demo") / "scripts" / "episode_1.json").exists()
        assert (await _service(pm).get_state("demo", 1))["status"] == "pending_review"

    async def test_script_plan_rewritten_during_materialization_is_a_conflict(
        self, tmp_path, monkeypatch, video_request_facts
    ):
        import lib.script.script_generator as script_generator_module

        pm = _make_project(tmp_path, "narration")
        plan = _narration_script_plan()
        plan_path = _write_script_plan(pm, "narration", plan)
        rewritten = json.loads(json.dumps(plan, ensure_ascii=False))
        rewritten["segments"][0]["novel_text"] = "确认途中被改写的正文。"
        read_overwrite = script_generator_module.formal_script_overwrite

        def rewrite_plan_then_read_overwrite(project_path: Path, project: dict, episode: int):
            # After materialization has loaded and checked the plan but before writing, another entry wrote a new plan.
            # 物化已加载并核对过规划、尚未落盘时，另一入口写入了新规划。
            atomic_write_json(plan_path, rewritten)
            return read_overwrite(project_path, project, episode)

        monkeypatch.setattr(script_generator_module, "formal_script_overwrite", rewrite_plan_then_read_overwrite)

        with pytest.raises(ScriptReviewError) as exc:
            await _service(pm).confirm("demo", 1)

        assert exc.value.code == "conversion_conflict"
        assert not (pm.get_project_path("demo") / "scripts" / "episode_1.json").exists()
        assert script_review.stored_review(pm.load_project("demo"), 1) == {}


# ---------------------------------------------------------------------------
# State transitions (narration, sharing the same gate)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 状态流转（narration，共用同一 gate）
# ---------------------------------------------------------------------------


class TestNarrationGateFlow:
    async def test_pending_then_confirm(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "narration")
        svc = _service(pm)
        _write_script_plan(pm, "narration", _narration_script_plan())

        state = await svc.get_state("demo", 1)
        assert state["status"] == "pending_review"
        assert state["content"]["segments"][0]["novel_text"].startswith("裴与")

        assert (await svc.confirm("demo", 1))["status"] == "confirmed"

    async def test_saving_confirmed_novel_text_is_rejected_until_rerun(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "narration")
        svc = _service(pm)
        path = _write_script_plan(pm, "narration", _narration_script_plan())
        await svc.confirm("demo", 1)
        before = path.read_bytes()

        edited = _narration_script_plan()
        edited["segments"][0]["novel_text"] = "裴与出征后的第三年。"
        with pytest.raises(ScriptReviewError) as exc:
            await svc.save_content("demo", 1, edited)
        assert exc.value.code == "script_plan_confirmed"
        assert path.read_bytes() == before

        rerun = _narration_script_plan()
        rerun["segments"][0]["duration_seconds"] = 8
        _write_script_plan(pm, "narration", rerun)
        await svc.save_content("demo", 1, edited)
        assert (await svc.get_state("demo", 1))["status"] == "pending_review"

    async def test_quarantined_script_plan_blocks_confirm_and_prompt_authoring(self, tmp_path, video_request_facts):
        """narration's invalid draft blocks independently, the same as the other two routes: while a draft exists
        confirmation is refused and prompt_authoring is blocked, even if the final script_plan was confirmed long
        ago - fetching it back for editing leaves the final file untouched, so looking only at the fingerprint would
        release the previous content the user has not seen.

        narration 的待修复草稿与另两条路线同口径地独立阻塞：草稿在场期间确认被拒、prompt_authoring 被
        阻塞，即使正式 script_plan 早已确认过——取回编辑时正式文件原封不动，只看指纹会放行用户尚未
        看过的上一版内容。"""
        pm = _make_project(tmp_path, "narration")
        svc = _service(pm)
        project_path = pm.get_project_path("demo")
        _write_script_plan(pm, "narration", _narration_script_plan())
        await svc.confirm("demo", 1)
        assert (await svc.get_state("demo", 1))["status"] == "confirmed"

        write_quarantine(
            project_path,
            1,
            QUARANTINE_KIND_NARRATION_SCRIPT_PLAN,
            content={"segments": []},
            violations=[],
        )

        assert (await svc.get_state("demo", 1))["status"] == "pending_review"
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "pending_review"
        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)
        assert exc.value.code == "quarantined"

        clear_quarantine(project_path, 1, QUARANTINE_KIND_NARRATION_SCRIPT_PLAN)
        assert (await svc.get_state("demo", 1))["status"] == "confirmed"


# ---------------------------------------------------------------------------
# State transitions (reference_video, sharing the same gate across content_mode)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 状态流转（reference_video，跨 content_mode 共用同一 gate）
# ---------------------------------------------------------------------------


class TestReferenceVideoGateFlow:
    async def test_no_script_plan_then_pending_then_confirmed(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        project_path = pm.get_project_path("demo")

        # script_plan not produced
        # script_plan 未产出
        assert (await svc.get_state("demo", 1))["status"] == "no_script_plan"

        # script_plan produced -> reviewable intermediate state, prompt_authoring blocked
        # script_plan 产出 → 可审中间态、阻塞 prompt_authoring
        _write_rv_script_plan(pm, _rv_script_plan())
        state = await svc.get_state("demo", 1)
        assert state["status"] == "pending_review"
        assert state["content"]["units"][0]["unit_id"] == "E1U01"
        assert state["content"]["units"][0]["text"].startswith("@[阿离]")
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "pending_review"

        # Confirm -> released
        # 确认 → 放行
        confirmed = await svc.confirm("demo", 1)
        assert confirmed["status"] == "confirmed"
        assert confirmed["confirmed_at"]
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "confirmed"

    async def test_saving_confirmed_units_is_rejected_without_writing(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        path = _write_rv_script_plan(pm, _rv_script_plan())
        await svc.confirm("demo", 1)
        before = path.read_bytes()

        edited = _rv_script_plan()
        edited["units"][0]["text"] = "@[阿离] 收伞。"
        with pytest.raises(ScriptReviewError) as exc:
            await svc.save_content("demo", 1, edited)

        assert exc.value.code == "script_plan_confirmed"
        assert path.read_bytes() == before
        assert (await svc.get_state("demo", 1))["status"] == "confirmed"

    async def test_editing_unit_text_after_rerun_keeps_review_pending(self, tmp_path, video_request_facts):
        """Editing unit text after a rerun stays pending review; the text is the only content on disk, and reference
        images do not get a copy written with it.

        重跑后编辑单元正文仍待确认；正文是落盘的唯一内容，参考图不随之落一份副本。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        pm.add_scenes_batch("demo", {"屋檐": {"description": "雨夜屋檐"}})
        svc = _service(pm)
        _write_rv_script_plan(pm, _rv_script_plan())
        await svc.confirm("demo", 1)
        rerun = _rv_script_plan()
        rerun["units"][0]["text"] = "@[阿离] 立于屋檐下。"
        _write_rv_script_plan(pm, rerun)
        assert (await svc.get_state("demo", 1))["status"] == "pending_review"

        edited = _rv_script_plan()
        edited["units"][0]["text"] = "@[阿离] 立于屋檐下。\n镜头扫过 @[屋檐]。"
        await svc.save_content("demo", 1, edited)

        state = await svc.get_state("demo", 1)
        assert state["status"] == "pending_review"
        unit = state["content"]["units"][0]
        assert unit["text"] == edited["units"][0]["text"]
        assert "references" not in unit

    async def test_quarantined_script_plan_blocks_confirm_and_prompt_authoring(self, tmp_path, video_request_facts):
        """Draft present -> confirmation refused, prompt_authoring blocked, even if the final script_plan was confirmed
        long ago.

        The pending draft and "the final script_plan's content fingerprint" are independent: on a re-split violation
        the final file stays unchanged, so looking only at the fingerprint would judge the episode confirmed and
        release it, while the user sees the previous content.

        草稿在场 → 确认被拒、prompt_authoring 被阻塞，即使正式 script_plan 早已确认过。

        待处置草稿与「正式 script_plan 的内容指纹」相互独立：重拆分违约时正式文件保持不变，只看
        指纹会把该集判成 confirmed 并放行，用户看到的却是上一版内容。
        """
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        project_path = pm.get_project_path("demo")
        _write_rv_script_plan(pm, _rv_script_plan())
        await svc.confirm("demo", 1)
        assert (await svc.get_state("demo", 1))["status"] == "confirmed"

        write_quarantine(
            project_path,
            1,
            QUARANTINE_KIND_SCRIPT_PLAN,
            content={"units": []},
            violations=[DraftViolation("坏", code="empty_text", label="unit E1U01")],
        )

        assert (await svc.get_state("demo", 1))["status"] == "pending_review"
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "pending_review"
        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)
        assert exc.value.code == "quarantined"

        # After the draft is cleared, fall back to the existing fingerprint check (content unchanged, confirmation
        # still valid)
        # 草稿清除后回到既有的指纹判定（内容未变，确认仍然有效）
        clear_quarantine(project_path, 1, QUARANTINE_KIND_SCRIPT_PLAN)
        assert (await svc.get_state("demo", 1))["status"] == "confirmed"

    def test_quarantine_path_follows_the_project_variant(self, tmp_path):
        """Drafts resolve by the project's current variant: each of the three routes recognizes its own file name.

        Sharing one file name or judging regardless of variant would treat leftover drafts of other generation modes
        as pending items of the current mode - no current writer would clean that file, and the episode would be
        stuck in the blocked state forever.

        草稿按项目当前变体解析：三条路线各认自己的文件名。

        共用一个文件名或不分变体地判，会让其他生成模式的遗留草稿被当作当前模式的待处置件——
        那份文件没有当前写入方会清理，该集会被永久卡在阻塞态。
        """
        drama_pm = _make_project(tmp_path / "drama", "drama")
        rv_pm = _make_project(tmp_path / "rv", "drama", generation_mode="reference_video")
        narration_pm = _make_project(tmp_path / "nr", "narration")

        drama_path = script_review.script_plan_quarantine_path(
            drama_pm.get_project_path("demo"), drama_pm.load_project("demo"), 1
        )
        rv_path = script_review.script_plan_quarantine_path(
            rv_pm.get_project_path("demo"), rv_pm.load_project("demo"), 1
        )
        narration_path = script_review.script_plan_quarantine_path(
            narration_pm.get_project_path("demo"), narration_pm.load_project("demo"), 1
        )

        assert drama_path is not None
        assert drama_path.name == "script_plan_normalized_script.invalid.json"
        assert rv_path is not None
        assert rv_path.name == "script_plan_reference_units.invalid.json"
        assert narration_path is not None
        assert narration_path.name == "script_plan_segments.invalid.json"

    def test_quarantine_of_another_variant_does_not_block(self, tmp_path):
        """Leftover drafts of other generation modes do not take part in the current mode's blocking check.

        其他生成模式的遗留草稿不参与当前模式的阻塞判定。"""
        pm = _make_project(tmp_path, "drama")
        project_path = pm.get_project_path("demo")
        write_quarantine(
            project_path,
            1,
            QUARANTINE_KIND_SCRIPT_PLAN,
            content={"units": []},
            violations=[DraftViolation("坏", code="empty_text", label="unit E1U01")],
        )

        assert script_review.script_plan_quarantined(project_path, pm.load_project("demo"), 1) is False

    async def test_confirm_rejects_unit_duration_out_of_range(self, tmp_path, video_request_facts):
        """Corrupt script_plan (unit duration out of range) -> confirmation refused by structural validation,
        prompt_authoring not released.

        损坏的 script_plan（unit 时长越界）→ 确认被结构校验拒绝，不放行 prompt_authoring。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        bad = _rv_script_plan()
        bad["units"][0]["duration_seconds"] = 9999  # Beyond the structural sanity range of unit duration / 超出 unit 时长的结构合理性区间
        _write_rv_script_plan(pm, bad)
        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)
        assert exc.value.code == "invalid_content"

    async def test_confirm_rejects_text_with_unregistered_mention(self, tmp_path, video_request_facts):
        """When an asset referenced in a picture slot of the body text is neither registered nor a new asset registered
        for this episode, confirmation is refused, naming the unit and the name.

        正文画面位引用的资产既未登记、也不是本集登记的新增资产时，确认拒绝并指出单元与名字。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        candidate = _rv_script_plan()
        candidate["units"][0]["text"] = "@[酒馆] 的木门被风吹开。"
        _write_rv_script_plan(pm, candidate)

        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)

        assert exc.value.code == "unregistered_references"
        assert exc.value.message == "E1U01: 酒馆"

    async def test_confirm_rejects_speech_problem_in_an_unmarked_unit(self, tmp_path, video_request_facts):
        """Speech admission applies to all units, not just those marked needs_replan: a unit with both character
        dialogue and unattributed narration cannot stack two audio tracks on one video, so it must be refused at
        confirmation.

        发声准入对全部 unit 生效，不只对标了 needs_replan 的那些：一个 unit 里既有人物
        台词又有无归属旁白，两条音轨在同一段视频上无从叠加，须在确认这一关就拒。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        candidate = _rv_script_plan()
        candidate["units"][0]["text"] = "@[阿离] 立于屋檐下。@[阿离]{雨要停了。}\n{雨声渐歇。}"
        _write_rv_script_plan(pm, candidate)

        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)

        assert exc.value.code == "speech_admission"

    @pytest.mark.parametrize("supported_durations", [(4, 6, 8), (4, 8)])
    async def test_confirm_names_the_off_tier_unit_by_position(
        self, tmp_path, set_video_request_facts, supported_durations
    ):
        """A unit whose confirmed duration was narrowed out: reported by its unit position in the script plan with the
        current duration and available options; the reason carries no internal unit ID.

        已确认时长被收窄出局的单元：按脚本规划里的单元位置报出现时长与可选档位，原因不带内部单元 ID。"""
        set_video_request_facts(_narrowed_rv_facts(supported_durations))
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        _write_rv_script_plan(pm, _off_tier_rv_script_plan())

        with pytest.raises(ScriptReviewError) as exc:
            await _service(pm).confirm("demo", 1)

        assert exc.value.code == "duration_off_tier"
        assert exc.value.params == {"position": 2, "duration_seconds": 6, "tiers": "4 / 8"}
        assert "视频单元 2" in exc.value.message
        assert "E1U02" not in exc.value.message
        assert not (pm.get_project_path("demo") / "scripts" / "episode_1.json").exists()
        assert (await _service(pm).get_state("demo", 1))["status"] == "pending_review"

    @pytest.mark.parametrize("supported_durations", [(4, 6, 8), (4, 8)])
    async def test_confirm_tool_reports_the_off_tier_unit_by_position(
        self, tmp_path, set_video_request_facts, supported_durations
    ):
        """Agent entry: in the same scenario the failure reason is explained by unit position, without the internal
        unit ID.

        Agent 入口：同一场景下失败原因按单元位置说明，不含内部单元 ID。"""
        set_video_request_facts(_narrowed_rv_facts(supported_durations))
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        _write_rv_script_plan(pm, _off_tier_rv_script_plan())
        ctx = ToolHarness(
            project_name="demo",
            data_root=tmp_path / "projects",
            pm=pm,
            config_resolver=cast(ConfigResolver, FakeConfigResolver()),
        )

        refused = await run_declared_tool(CONFIRM_SCRIPT_REVIEW, ctx, {"episode_id": 1})

        assert refused.problem is not None
        assert "视频单元 2" in refused.problem.detail
        assert "6s" in refused.problem.detail
        assert "[4, 8]" in refused.problem.detail
        assert "E1U02" not in refused.problem.detail

    async def test_confirm_other_conversion_refusals_stay_generic(self, tmp_path, video_request_facts):
        """Conversion refusals other than off-tier duration still use the generic conversion_refused.

        时长越档之外的转换拒绝仍走通用的 conversion_refused。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        plan = _rv_script_plan()
        plan["units"].append(dict(plan["units"][0]))
        _write_rv_script_plan(pm, plan)

        with pytest.raises(ScriptReviewError) as exc:
            await _service(pm).confirm("demo", 1)

        assert exc.value.code == "conversion_refused"
        assert "unit_id 重复" in exc.value.message

    async def test_reference_duration_tiers_take_narrowed_with_reference_durations(
        self, tmp_path, set_video_request_facts
    ):
        """The gate's dropdown tiers are those narrowed by the r2v facts, not the declared set - the same yardstick as
        the validation before prompt_authoring writes.

        Veo 3.1 at 1080p only accepts 8 s; if the declared set were exposed, users could pick 4/6 s, neither save
        nor confirm would stop it, and only prompt_authoring would hard-reject - content the user already confirmed
        would fail only after paying.

        gate 下拉的档位是 r2v 事实收窄后的档位而非声明全集，与 prompt_authoring 落盘前的校验同一把尺。

        Veo 3.1 设 1080p 时只接受 8 秒；若暴露声明全集，用户能选中 4/6 秒，save + confirm 都不拦，
        直到 prompt_authoring 才硬拒——用户已确认过的内容变成付完钱才失败。
        """
        set_video_request_facts(
            {
                "r2v": make_video_request_facts(
                    route="reference_video",
                    generation_type="r2v",
                    resolution="1080p",
                    supported_durations=(4, 6, 8),
                    allowed_durations=(8,),
                    excluded_durations=(4, 6),
                ),
                "i2v": VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "i2v"),)),
            }
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)

        tiers = await svc.get_reference_duration_tiers("demo", 1)
        assert tiers == {"with_references": [8], "units": {}}

    async def test_reference_duration_tiers_report_each_plan_unit_bucket(self, tmp_path, set_video_request_facts):
        """The panel's per-unit tiers follow the server-side bucket: a unit with a registered character but no sheet
        lands in i2v and names the unusable reference.

        面板逐单元取档以服务端定桶为准：登记了角色却没有资产图的单元落 i2v，并点名不可用引用。"""
        set_video_request_facts(
            {
                "i2v": make_video_request_facts(
                    route="reference_video",
                    generation_type="i2v",
                    supported_durations=(5, 10),
                    allowed_durations=(5, 10),
                ),
                "r2v": make_video_request_facts(
                    route="reference_video",
                    generation_type="r2v",
                    supported_durations=(4, 6, 8),
                    allowed_durations=(8,),
                ),
            }
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        plan = _rv_script_plan()
        plan["units"].append(
            {"unit_id": "E1U02", "text": "空镜：雨停了。", "duration_seconds": 5, "source_text": "雨停了。"}
        )

        tiers = await _service(pm).get_reference_duration_tiers("demo", 1, plan["units"])

        assert tiers is not None
        units = tiers["units"]
        assert set(units) == {"E1U01", "E1U02"}
        assert (units["E1U01"]["declared_capability"], units["E1U01"]["hydrated_capability"]) == ("r2v", "i2v")
        assert units["E1U01"]["unavailable_references"] == [
            {"type": "character", "name": "阿离"},
            {"type": "character", "name": "裴与"},
        ]
        assert units["E1U01"]["allowed_durations"] == [5, 10]
        assert [problem["code"] for problem in units["E1U01"]["problems"]] == [
            "reference_asset_missing",
            "reference_capability_changed",
        ]
        assert units["E1U02"]["hydrated_capability"] == "i2v"
        assert units["E1U02"]["allowed_durations"] == [5, 10]
        assert units["E1U02"]["problems"] == []

    async def test_no_image_i2v_tier_is_available_in_review_state(self, tmp_path, set_video_request_facts):
        """Units without references take tiers from the i2v bucket facts: seconds unique to i2v are kept in both the
        read-time migration and the panel tiers.

        无引用单元按 i2v 桶事实取档：i2v 独有的秒数在读时迁移与面板档位里都保留。"""
        set_video_request_facts(
            {
                "r2v": make_video_request_facts(route="reference_video", generation_type="r2v"),
                "i2v": make_video_request_facts(
                    route="reference_video",
                    generation_type="i2v",
                    provider_id="ark",
                    model_id="doubao-seedance-2-0-260128",
                    supported_durations=(5, 10),
                    allowed_durations=(5, 10),
                ),
            }
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        plan = _rv_script_plan()
        plan["units"][0]["duration_seconds"] = 5
        plan["units"][0]["text"] = "镜头1：空镜，晨光照亮街道。"
        _write_rv_script_plan(pm, plan)
        service = _service(pm)

        state = await service.get_state("demo", 1)
        tiers = await service.get_reference_duration_tiers("demo", 1, plan["units"])

        assert state["content"]["units"][0]["duration_seconds"] == 5
        assert 5 in state["supported_durations"]
        assert tiers["units"]["E1U01"]["allowed_durations"] == [5, 10]
        assert 5 not in tiers["with_references"]

    @pytest.mark.parametrize("operation", ["read", "confirm"])
    async def test_unknown_i2v_preserves_no_image_legacy_duration(self, tmp_path, set_video_request_facts, operation):
        """When the i2v bucket facts cannot be resolved: the read-time migration does not rewrite seconds of units
        without references using r2v tiers, and confirmation is refused with the i2v problem code.

        i2v 桶事实解析不出时：读时迁移不借 r2v 档位改写无引用单元的秒数，确认按 i2v 的问题码拒绝。"""
        set_video_request_facts(
            {
                "r2v": make_video_request_facts(route="reference_video", generation_type="r2v"),
                "i2v": VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "i2v"),)),
            }
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        plan = {
            "units": [
                {
                    "unit_id": "E1U01",
                    "text": "镜头1：空镜，街道渐亮。",
                    "duration_seconds": 5,
                    "source_text": "原文",
                    "duration_override": False,
                }
            ]
        }
        path = _write_rv_script_plan(pm, plan)
        service = _service(pm)

        if operation == "confirm":
            with pytest.raises(ScriptReviewError) as exc:
                await service.confirm("demo", 1)
            assert exc.value.code == "video_request_facts"
            assert exc.value.problem is not None
            assert exc.value.problem.code == "reference_capability_unavailable"

        state = await service.get_state("demo", 1)
        tiers = await service.get_reference_duration_tiers("demo", 1, plan["units"])

        assert state["content"]["units"][0]["duration_seconds"] == 5
        assert json.loads(path.read_text())["units"][0]["duration_seconds"] == 5
        assert state["supported_durations"] is None
        assert tiers["units"]["E1U01"]["allowed_durations"] is None
        assert tiers["units"]["E1U01"]["problem"]["code"] == "reference_capability_unavailable"

    async def test_reference_duration_tiers_none_when_with_reference_facts_unresolved(self, tmp_path):
        """None when the r2v bucket's video request facts cannot be resolved; the presentation layer stays read-only and
        borrows no declared set to offer options.

        r2v 桶的视频请求事实解析不出时为 None，呈现层保持只读，不借任何声明全集提供可选项。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)

        assert await svc.get_reference_duration_tiers("demo", 1) is None

    async def test_reference_duration_tiers_none_for_non_reference_video_episode(self, tmp_path, video_request_facts):
        """Non-reference_video variants evaluate no facts and return None directly - the criterion is the method's own
        script_plan_kind, not callers short-circuiting on whether get_state.supported_durations is non-None.

        非 reference_video 变体不求值事实、直接 None——判据是方法自身的 script_plan_kind，
        不能靠调用方按 get_state.supported_durations 是否非 None 短路。"""
        pm = _make_project(tmp_path, "drama")  # generation_mode defaults, not reference_video / generation_mode 缺省，非 reference_video
        svc = _service(pm)

        assert await svc.get_reference_duration_tiers("demo", 1) is None

    async def test_reference_duration_tiers_take_custom_provider_facts(self, tmp_path, set_video_request_facts):
        """Custom providers (``custom-`` prefix) are not in ``PROVIDER_REGISTRY``: the only source of the tier table
        is the video request facts, and the tiers from the r2v facts go into the panel unchanged.

        自定义供应商（``custom-`` 前缀）不在 ``PROVIDER_REGISTRY``：档位表唯一来源是视频请求事实，
        r2v 事实给出的档位原样进面板。"""
        set_video_request_facts(
            {
                "r2v": make_video_request_facts(
                    route="reference_video",
                    generation_type="r2v",
                    provider_id="custom-acme",
                    model_id="acme-video",
                    supported_durations=(5, 10),
                    allowed_durations=(5, 10),
                ),
                "i2v": VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "i2v"),)),
            }
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)

        tiers = await svc.get_reference_duration_tiers("demo", 1)
        assert tiers == {"with_references": [5, 10], "units": {}}


class TestReferenceVideoScriptPlanMigration:
    """One-time consolidation migration on the gate side for existing script_plan drafts (per-shot durations).

    存量 script_plan 草稿（per-shot 时长）在 gate 侧的一次性收编迁移。"""

    @staticmethod
    def _legacy_script_plan(seconds: int = 8) -> dict:
        """Pre-consolidation shape: the body is already in v9 form but still carries the retired ``duration_override``
        marker.

        收编前形状：正文已是 v9 形态，但仍带着退役的 ``duration_override`` 标记。"""
        legacy = _rv_script_plan()
        legacy["units"][0]["duration_seconds"] = seconds
        legacy["units"][0]["duration_override"] = False
        return legacy

    async def test_migration_takes_slot_so_prompt_authoring_never_sees_a_non_member_duration(
        self, tmp_path, set_video_request_facts
    ):
        """Seconds written by the content review migration must be tier members, not just "within the structural range".

        The migration is idempotent and one-time; whoever runs first decides the final value, and the normal product
        flow does content review before generation: if content review wrote a non-tier value within the structural
        range, prompt_authoring's enum schema would then hard-reject, and the user could neither see nor fix the
        problem in the gate.
        So content review and generation use the same tier table.

        内容确认迁移落盘的秒数必是档位成员，不能只是「落在结构区间内」。

        迁移幂等一次性、谁先跑谁定终局，而正常产品流程是先做内容确认再生成：内容确认若按结构
        区间落一个非档位秒数，prompt_authoring 的枚举 schema 随后硬拒，用户在 gate 里看不出问题也改不动。
        故内容确认与生成侧取同一份档位表。
        """
        set_video_request_facts(
            make_video_request_facts(
                route="reference_video",
                generation_type="i2v",
                supported_durations=(4, 8, 12),
                allowed_durations=(4, 8, 12),
            )
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        # 10 s is within the structural range but not a tier member - a structural-only clamp would freeze it as is.
        # 10s 落在结构区间内，但不是档位成员——只做结构 clamp 时会原样固化。
        legacy["units"][0]["duration_seconds"] = 10
        path = _write_rv_script_plan(pm, legacy)

        assert (await svc.get_state("demo", 1))["content"]["units"][0]["duration_seconds"] == 12
        assert json.loads(path.read_text(encoding="utf-8"))["units"][0]["duration_seconds"] == 12

    async def test_custom_provider_draft_migration_takes_slot_from_facts(self, tmp_path, set_video_request_facts):
        """Custom providers (``custom-`` prefix) are not in ``PROVIDER_REGISTRY``: only video request facts can provide
        the tier table.

        Read-time consolidation picks tiers from the facts' declared set, so the written seconds are tier members and
        prompt_authoring's enum schema will not hard-reject; ``supported_durations`` carries the same tiers, so the
        panel's options and the consolidated value share one source.

        自定义供应商（``custom-`` 前缀）不在 ``PROVIDER_REGISTRY``：档位表只有视频请求事实给得出。

        读时收编按事实的声明全集取档，落盘的秒数是档位成员，prompt_authoring 的枚举 schema 不会硬拒；
        ``supported_durations`` 一并带出同一份档位，面板的可选项与收编到的值同源。
        """
        set_video_request_facts(
            make_video_request_facts(
                route="reference_video",
                generation_type="i2v",
                provider_id="custom-acme",
                model_id="acme-video",
                supported_durations=(5, 10),
                allowed_durations=(5, 10),
            )
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        # 7 s is within the structural range but not a member of [5, 10].
        # 7s 在结构区间内，但不是 [5, 10] 的成员。
        legacy["units"][0]["duration_seconds"] = 7
        path = _write_rv_script_plan(pm, legacy)

        state = await svc.get_state("demo", 1)
        assert state["content"]["units"][0]["duration_seconds"] == 10
        assert state["supported_durations"] == [5, 10]
        assert json.loads(path.read_text(encoding="utf-8"))["units"][0]["duration_seconds"] == 10

    async def test_custom_provider_direct_confirm_takes_slot_from_facts(self, tmp_path, set_video_request_facts):
        """When the Agent / API bypasses get_state and confirms directly, consolidation also uses the facts' tiers - if
        the two entries disagreed, whichever runs first would freeze a non-tier value on disk (the migration is
        idempotent and one-time).

        Agent / API 绕过 get_state 直接 confirm 时同样按事实档位收编——两个入口口径不一致
        的话，先跑的那个会把非档位秒数固化到盘上（迁移幂等一次性）。"""
        set_video_request_facts(
            make_video_request_facts(
                route="reference_video",
                generation_type="i2v",
                provider_id="custom-acme",
                model_id="acme-video",
                supported_durations=(5, 10),
                allowed_durations=(5, 10),
            )
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        legacy["units"][0]["duration_seconds"] = 7
        _write_rv_script_plan(pm, legacy)

        state = await svc.confirm("demo", 1)
        assert state["status"] == "confirmed"
        assert state["content"]["units"][0]["duration_seconds"] == 10

    async def test_migration_falls_back_to_structural_clamp_without_video_backend(
        self, tmp_path, set_video_request_facts
    ):
        """Project without a resolvable video model configured: the tier table is unavailable, so fall back to the
        structural range clamp rather than blocking draft loading.

        项目未配置可解析的视频型号：档位表取不到，退回结构区间 clamp 而非阻断草稿加载。"""
        set_video_request_facts(VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "i2v"),)))
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        legacy["units"][0]["duration_seconds"] = 10

        _write_rv_script_plan(pm, legacy)
        assert (await svc.get_state("demo", 1))["content"]["units"][0]["duration_seconds"] == 10

    async def test_legacy_draft_is_migrated_on_read_and_written_back(self, tmp_path, video_request_facts):
        """Reading the state consolidates: the retired ``duration_override`` is stripped, the unit duration is kept,
        written once, and a second read does not rewrite.

        读状态即收编：退役的 ``duration_override`` 被剥掉、unit 时长保持，且一次落盘、二次读不再改写。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        path = _write_rv_script_plan(pm, self._legacy_script_plan())

        unit = (await svc.get_state("demo", 1))["content"]["units"][0]
        assert unit["duration_seconds"] == 8
        assert "duration_override" not in unit

        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["units"][0]["duration_seconds"] == 8
        assert "duration_override" not in on_disk["units"][0]

        # Idempotent: a second read does not rewrite the stored content.
        # 幂等：二次读不再改写落盘内容。
        before = path.read_bytes()
        await svc.get_state("demo", 1)
        assert path.read_bytes() == before

    async def test_legacy_draft_can_be_confirmed_and_saved(self, tmp_path, video_request_facts):
        """After consolidation, existing drafts can be confirmed and saved in the gate - before migration both hit
        structural validation (the unit carries a retired field).

        收编后存量草稿在 gate 里可确认、可保存——迁移前两者都撞结构校验（unit 带已退役字段）。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        _write_rv_script_plan(pm, self._legacy_script_plan())

        edited = (await svc.get_state("demo", 1))["content"]
        edited["units"][0]["text"] = "@[阿离] 收伞。"
        assert (await svc.save_content("demo", 1, edited))["status"] == "pending_review"

        confirmed = await svc.confirm("demo", 1)
        assert confirmed["status"] == "confirmed"

    async def test_confirm_survives_migration_without_reopening_review(self, tmp_path, video_request_facts):
        """Migration is mechanical consolidation, not a content edit: confirmed episodes do not go back to pending
        review because of loading.

        迁移是机械收编、不是内容编辑：已确认的分集不因加载而重新等待确认。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        path = _write_rv_script_plan(pm, legacy)

        # First record a confirmation fingerprint on the pre-consolidation content (simulating an episode reviewed
        # before the upgrade).
        # 先在收编前的内容上记录确认指纹（模拟升级前已通过审核的分集）。
        def _confirm_legacy(p: dict) -> None:
            script_review.apply_confirmation(p, 1, script_review.content_fingerprint(path), "2026-01-01T00:00:00Z")

        pm.update_project("demo", _confirm_legacy)
        assert (await svc.get_state("demo", 1))["status"] == "confirmed"

        state = await svc.get_state("demo", 1)
        assert state["status"] == "confirmed"
        assert state["confirmed_at"] == "2026-01-01T00:00:00Z"
        assert state["content"]["units"][0]["duration_seconds"] == 8

    async def test_confirm_reopens_review_when_migration_clamps_duration(self, tmp_path, set_video_request_facts):
        """A migration with warnings (duration rewritten by clamp) is not pure format consolidation: confirmed episodes
        must wait for review again, not carry the confirmation over like pure structural consolidation - the
        clamped seconds are not the value the user saw when confirming.

        迁移带 warnings（时长被 clamp 改写）不是纯格式收编：已确认分集须重新等待确认，
        不能像纯结构收编那样平移确认——clamp 后的秒数不是用户确认时看到的值。
        """
        set_video_request_facts(
            make_video_request_facts(
                route="reference_video",
                generation_type="i2v",
                supported_durations=(4, 8, 12),
                allowed_durations=(4, 8, 12),
            )
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        # 90 s exceeds the largest tier (12 s); the migration picks a tier, rewrites, and records a warning.
        # 90s 超出最大档位（12s），迁移会取档改写并记 warning。
        legacy["units"][0]["duration_seconds"] = 90
        path = _write_rv_script_plan(pm, legacy)

        def _confirm_legacy(p: dict) -> None:
            script_review.apply_confirmation(p, 1, script_review.content_fingerprint(path), "2026-01-01T00:00:00Z")

        pm.update_project("demo", _confirm_legacy)

        state = await svc.get_state("demo", 1)
        assert state["status"] == "pending_review"
        assert state["content"]["units"][0]["duration_seconds"] == 12

    async def test_clamping_migration_reopens_review_for_grandfathered_episode(self, tmp_path, set_video_request_facts):
        """An existing episode that never stored a confirmation fingerprint and was released by the grandfather rule
        (prompt_authoring exists): after the migration clamps and rewrites the duration it must wait for review
        again - the migration writes idempotently and retries produce no warnings, so without a mismatch marker,
        later generation would silently adopt values the user never saw.

        从未存过确认指纹、靠 grandfather 判据（prompt_authoring 已存在）放行的存量集：迁移 clamp
        改写时长后须重新等待确认——迁移幂等落盘，重试不再产生 warnings，不落失配标记的话
        后续生成会静默采用用户从未过目的取值。
        """
        set_video_request_facts(
            make_video_request_facts(
                route="reference_video",
                generation_type="i2v",
                supported_durations=(4, 8, 12),
                allowed_durations=(4, 8, 12),
            )
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        legacy["units"][0]["duration_seconds"] = 90
        _write_rv_script_plan(pm, legacy)
        _write_prompt_authoring(pm)

        state = await svc.get_state("demo", 1)
        assert state["status"] == "pending_review"
        # An idempotent re-read does not put the state back to grandfather release: the mismatch marker is persisted.
        # 幂等重读不会把状态放回 grandfather 放行：失配标记已持久化。
        assert (await svc.get_state("demo", 1))["status"] == "pending_review"

    async def test_clamping_migration_marker_survives_interrupted_project_write(
        self, tmp_path, monkeypatch, set_video_request_facts
    ):
        """The migration does two writes, "project mismatch marker + draft": after the project write fails, a retry
        must still converge to pending review.

        If the draft were written first, the retry would judge changed=False and the marker could never be added, so
        a grandfathered episode would stay confirmed with the clamped duration; if the marker is written first, the
        draft still has pre-migration content, and a retry reruns the migration and self-heals.

        迁移是「project 失配标记 + 草稿」两次写：project 那次失败后重试仍须收敛到待确认。

        草稿先落盘则重试判 changed=False、标记再也补不上，grandfather 存量集会带着被 clamp
        的时长停在 confirmed；标记先落盘时草稿仍是迁移前内容，重试重跑迁移即自愈。
        """
        set_video_request_facts(
            make_video_request_facts(
                route="reference_video",
                generation_type="i2v",
                supported_durations=(4, 8, 12),
                allowed_durations=(4, 8, 12),
            )
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        legacy["units"][0]["duration_seconds"] = 90
        _write_rv_script_plan(pm, legacy)
        _write_prompt_authoring(pm)

        original_update = pm.update_project
        failed: list[int] = []

        def _fail_first_project_write(*args, **kwargs):
            if not failed:
                failed.append(1)
                raise OSError("project write interrupted")
            return original_update(*args, **kwargs)

        monkeypatch.setattr(pm, "update_project", _fail_first_project_write)

        with pytest.raises(OSError, match=r"project write interrupted"):
            await svc.get_state("demo", 1)

        monkeypatch.setattr(pm, "update_project", original_update)
        assert (await svc.get_state("demo", 1))["status"] == "pending_review"

    async def test_confirm_direct_call_confirms_migrated_content(self, tmp_path, set_video_request_facts):
        """The Agent / API may bypass get_state and call confirm directly: when the migration triggers inside confirm
        and clamps (lenient rule: clamp outside the enum + warning), confirm confirms and releases based on the
        migrated stored content.

        Agent / API 可能绕过 get_state 直接调用 confirm：迁移在 confirm 内部触发并 clamp
        时（枚举外 clamp + warning 的宽容口径），confirm 按迁移后的落盘内容确认放行。
        """
        set_video_request_facts(
            make_video_request_facts(
                route="reference_video",
                generation_type="i2v",
                supported_durations=(4, 8, 12),
                allowed_durations=(4, 8, 12),
            )
        )
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        legacy["units"][0]["duration_seconds"] = 90
        _write_rv_script_plan(pm, legacy)

        state = await svc.confirm("demo", 1)
        assert state["status"] == "confirmed"
        assert state["content"]["units"][0]["duration_seconds"] == 12

    async def test_confirmation_carry_uses_written_content_not_post_write_reread(
        self, tmp_path, monkeypatch, video_request_facts
    ):
        """After the migration writes back, carrying the confirmation fingerprint over must use the just-written
        content directly, not read the disk again - if a concurrent edit lands between the write-back and that read,
        it would read the concurrent content's fingerprint and wrongly carry the confirmation record over to
        unreviewed content.

        迁移写回后平移确认指纹须用刚写入的内容直接算，不能再读一次磁盘——写回与该次读取
        之间若有并发编辑落下，读到的会是并发内容的指纹，把确认记录错误地平移到一份未经审阅
        的内容上。
        """
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        path = _write_rv_script_plan(pm, legacy)
        before = script_review.content_fingerprint(path)

        def _confirm_legacy(p: dict) -> None:
            script_review.apply_confirmation(p, 1, before, "2026-01-01T00:00:00Z")

        pm.update_project("demo", _confirm_legacy)

        concurrent_edit = self._legacy_script_plan()
        concurrent_edit["units"][0]["text"] = "并发编辑：紧随迁移写回落盘。"

        written: list[dict] = []
        original_write = script_review.atomic_write_json

        def _write_then_concurrent_edit(target_path, data):
            written.append(json.loads(json.dumps(data)))
            original_write(target_path, data)
            atomic_write_json(path, concurrent_edit)

        monkeypatch.setattr(script_review, "atomic_write_json", _write_then_concurrent_edit)

        await svc.get_state("demo", 1)

        migrated_fingerprint = script_review.content_fingerprint_of_data(written[0])
        concurrent_fingerprint = script_review.content_fingerprint_of_data(concurrent_edit)
        stored = script_review.stored_review(pm.load_project("demo"), 1)

        assert stored["confirmed_at"] == "2026-01-01T00:00:00Z"
        assert stored["fingerprint"] == migrated_fingerprint
        assert stored["fingerprint"] != concurrent_fingerprint

    async def test_migration_carries_confirmation_that_lands_after_project_snapshot_loaded(
        self, tmp_path, monkeypatch, video_request_facts
    ):
        """The project snapshot loaded by get_state before migration is not refreshed afterwards: if a confirmation
        happens after this snapshot was loaded and before the migration write-back completes, the decision to carry
        the confirmation must not rely on this stale snapshot - that would misjudge the fresh confirmation as
        "unconfirmed", skip the move, and lose it forever (the migration is idempotent; later retries cannot recover
        it).

        get_state 在迁移前加载的 project 快照此后不再刷新：若确认发生在这份快照加载
        之后、迁移写回完成之前，携带确认的判断不能依赖这份陈旧快照——那样会把刚发生的
        确认误判成"未确认"而跳过搬移，永久丢失它（迁移幂等，往后重试也补不回来）。
        """
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        legacy = self._legacy_script_plan()
        path = _write_rv_script_plan(pm, legacy)
        before = script_review.content_fingerprint(path)

        original_migrate = script_review.migrate_unit_durations

        def _migrate_with_concurrent_confirm(units, **kwargs):
            # Simulate another request's confirm() landing a confirmation after get_state loads the project snapshot and
            # before the migration completes.
            # 模拟另一请求的 confirm() 在 get_state 加载 project 快照之后、迁移完成之前落下确认。
            def _confirm_legacy(p: dict) -> None:
                script_review.apply_confirmation(p, 1, before, "2026-01-01T00:00:00Z")

            pm.update_project("demo", _confirm_legacy)
            return original_migrate(units, **kwargs)

        monkeypatch.setattr(script_review, "migrate_unit_durations", _migrate_with_concurrent_confirm)

        state = await svc.get_state("demo", 1)
        assert state["status"] == "confirmed"

    async def test_migration_does_not_confirm_an_unconfirmed_episode(self, tmp_path, video_request_facts):
        """When the fingerprint did not match to begin with (script_plan really changed), the confirmation record is
        not carried over and the episode is handled as pending review as usual.

        指纹本就对不上（script_plan 确实改过）时不平移确认记录，照常按待确认处理。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        _write_rv_script_plan(pm, self._legacy_script_plan())

        def _stale_confirm(p: dict) -> None:
            script_review.apply_confirmation(p, 1, "0" * 64, "2026-01-01T00:00:00Z")

        pm.update_project("demo", _stale_confirm)
        assert (await svc.get_state("demo", 1))["status"] == "pending_review"


class TestReferenceVideoPromptAuthoringEnforcement:
    async def test_confirm_tool_materializes_the_script_authoring_reads(self, tmp_path, video_request_facts):
        """Agent path: while the rv script_plan is unconfirmed there is no final script yet, so the authoring entry
        points to content review; the confirm_script_review tool generates the final script on confirmation, and the
        authoring entry is released.

        Agent 路径：rv 的 script_plan 未确认时尚无正式脚本，编写入口指向内容确认；confirm_script_review
        工具确认即生成正式脚本，编写入口随之放行。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        _write_rv_script_plan(pm, _rv_script_plan())
        project_path = pm.get_project_path("demo")

        ctx = ToolHarness(
            project_name="demo",
            data_root=tmp_path / "projects",
            pm=pm,
            config_resolver=cast(ConfigResolver, FakeConfigResolver()),
        )
        refused = await run_declared_tool(GENERATE_EPISODE_SCRIPT, ctx, {"episode_id": 1})
        assert refused.problem is not None
        assert "尚无正式脚本" in refused.problem.detail
        assert "内容确认" in refused.problem.detail

        result = await run_declared_tool(CONFIRM_SCRIPT_REVIEW, ctx, {"episode_id": 1})
        assert result.problem is None, result
        assert (project_path / "scripts" / "episode_1.json").exists()

        dry_run = await run_declared_tool(GENERATE_EPISODE_SCRIPT, ctx, {"episode_id": 1, "dry_run": True})
        assert dry_run.problem is None, dry_run


# ---------------------------------------------------------------------------
# Scope: drama / narration / reference_video are under the gate; ad is not
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 适用范围：drama / narration / reference_video 纳入 gate；ad 不纳入
# ---------------------------------------------------------------------------


class TestApplicability:
    async def test_reference_video_applicable(self, tmp_path, video_request_facts):
        """reference_video (across content_mode) is under the gate; the script_plan variant is judged reference_video.

        reference_video（跨 content_mode）纳入 gate，script_plan 变体判为 reference_video。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        project = pm.load_project("demo")
        assert script_review.script_plan_kind(project) == "reference_video"
        assert script_review.is_applicable(project) is True
        # No script_plan produced -> no_script_plan (distinct from ad's not_applicable).
        # 未产 script_plan → no_script_plan（区别于 ad 的 not_applicable）。
        assert (await _service(pm).get_state("demo", 1))["status"] == "no_script_plan"

    async def test_ad_not_applicable(self, tmp_path):
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("addemo")
        pm.create_project_metadata("addemo", "Ad", "Anime", "ad")
        svc = _service(pm)
        assert script_review.script_plan_kind(svc.pm.load_project("addemo")) is None
        assert (await svc.get_state("addemo", 1))["status"] == "not_applicable"


# ---------------------------------------------------------------------------
# Edit validation + confirmation precondition errors
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 编辑校验 + 确认前置错误
# ---------------------------------------------------------------------------


class TestErrors:
    @pytest.mark.parametrize("kind", ["drama", "narration", "reference_video"])
    async def test_agent_draft_keeps_formal_plan_readonly_until_discarded(self, tmp_path, video_request_facts, kind):
        pm = _make_project(
            tmp_path,
            "narration" if kind == "narration" else "drama",
            generation_mode="reference_video" if kind == "reference_video" else None,
        )
        if kind == "reference_video":
            content = _rv_script_plan()
            path = _write_rv_script_plan(pm, content)
            doc_type = "reference_script_plan"
        else:
            content = _admitted_drama_script_plan() if kind == "drama" else _narration_script_plan()
            path = _write_script_plan(pm, kind, content)
            doc_type = f"{kind}_script_plan"
        service = _service(pm)
        state = await service.get_state("demo", 1)
        original = path.read_bytes()
        workflow = DraftWorkflow(
            DraftContext(project_name="demo", data_root=pm.data_root, pm=pm, config_resolver=service.config_resolver)
        )
        opened = await workflow.open(1, doc_type)
        quarantine_kind = script_review.script_plan_quarantine_kind(pm.load_project("demo"))
        assert quarantine_kind is not None
        draft_path = quarantine_path(pm.get_project_path("demo"), 1, quarantine_kind)
        original_draft = draft_path.read_bytes()
        edited = json.loads(json.dumps(state["content"]))
        if kind == "narration":
            edited["segments"][0]["novel_text"] += "后来。"
        elif kind == "drama":
            edited["title"] = "修改后的标题"
        else:
            edited["units"][0]["text"] += "\n雨停了。"

        with pytest.raises(ScriptReviewError) as exc:
            await service.save_content("demo", 1, edited, state["fingerprint"])

        assert exc.value.code == "draft_agent_owned"
        assert path.read_bytes() == original
        assert draft_path.read_bytes() == original_draft
        await workflow.discard(1, doc_type, opened["revision"])
        assert (await service.save_content("demo", 1, edited, state["fingerprint"]))["status"] == "pending_review"

    async def test_save_empty_dialogue_speaker_returns_structured_admission(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        path = _write_script_plan(pm, "drama", _drama_script_plan())

        bad = _drama_script_plan()
        bad["scenes"][0]["utterances"][1] = {"kind": "dialogue", "speaker": None, "text": "无人"}
        with pytest.raises(ScriptReviewError) as exc:
            await svc.save_content("demo", 1, bad)
        assert exc.value.code == "speech_admission"
        assert exc.value.admission is not None
        assert exc.value.admission.unit_id == "E1S01"
        assert exc.value.admission.problems[0].code == "empty_speaker"
        assert exc.value.admission.problems[0].locations[0].path == ("utterances", 1, "speaker")
        assert json.loads(path.read_text(encoding="utf-8")) == _drama_script_plan()

    async def test_save_invalid_non_speech_content_rejected(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        _write_script_plan(pm, "drama", _drama_script_plan())

        bad = _drama_script_plan()
        bad["scenes"][0]["duration_seconds"] = "invalid"
        with pytest.raises(ScriptReviewError) as exc:
            await svc.save_content("demo", 1, bad)
        assert exc.value.code == "invalid_content"

    async def test_confirm_without_script_plan_rejected(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)
        assert exc.value.code == "no_script_plan"

    async def test_confirm_marked_drama_candidate_returns_structured_admission(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        candidate = _drama_script_plan()
        candidate["scenes"][0]["needs_replan"] = True
        _write_script_plan(pm, "drama", candidate)

        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)

        assert exc.value.code == "speech_admission"
        assert exc.value.admission is not None
        assert exc.value.admission.unit_id == "E1S01"
        assert exc.value.admission.problems[0].code == "needs_replan"
        project = pm.load_project("demo")
        assert script_review.review_status(pm.get_project_path("demo"), project, 1) == "pending_review"

    async def test_save_not_applicable_rejected(self, tmp_path):
        pm = _make_project(tmp_path, "ad")  # ad has no structured script_plan; the gate does not apply / ad 无结构化 script_plan，gate 不适用
        svc = _service(pm)
        with pytest.raises(ScriptReviewError) as exc:
            await svc.save_content("demo", 1, _drama_script_plan())
        assert exc.value.code == "not_applicable"

    async def test_get_state_unregistered_episode_rejected(self, tmp_path):
        """Gate applies but the episode is not registered in project.json -> episode_not_found (not a false
        no_script_plan).

        适用 gate 但分集未登记 project.json → episode_not_found（而非误报 no_script_plan）。"""
        pm = _make_project(tmp_path, "drama")  # Only episode 1 registered / 仅登记第 1 集
        svc = _service(pm)
        with pytest.raises(ScriptReviewError) as exc:
            await svc.get_state("demo", 99)
        assert exc.value.code == "episode_not_found"

    async def test_save_unregistered_episode_writes_no_orphan(self, tmp_path):
        """Saving for an unregistered episode -> episode_not_found, and no orphan script_plan file drafts/episode_99 is
        written.

        给未登记分集保存 → episode_not_found，且不落 drafts/episode_99 孤儿 script_plan 文件。"""
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        with pytest.raises(ScriptReviewError) as exc:
            await svc.save_content("demo", 99, _drama_script_plan())
        assert exc.value.code == "episode_not_found"
        orphan = pm.get_project_path("demo") / "drafts" / "episode_99" / "script_plan_normalized_script.json"
        assert not orphan.exists()

    async def test_save_with_stale_fingerprint_conflicts_reference_video(self, tmp_path, video_request_facts):
        """rv concurrent edits: the baseline fingerprint carried by the save differs from the current value on disk
        (another party saved meanwhile) -> conflict, nothing written or overwritten; retrying with the latest
        fingerprint (equivalent to after refresh and merge) is let through.

        rv 并发编辑：保存携带的基线指纹与盘上现值不一致（编辑期间另一方已保存）→ conflict、
        不落盘不覆盖；拿最新指纹（等价于刷新合并后）重试放行。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        path = _write_rv_script_plan(pm, _rv_script_plan())
        stale = (await svc.get_state("demo", 1))["fingerprint"]

        # Another editor saves first (content changes -> fingerprint drifts)
        # 另一编辑方先保存（内容变化 → 指纹漂移）
        other = _rv_script_plan()
        other["units"][0]["text"] = "@[阿离] 转身离开。"
        await svc.save_content("demo", 1, other)
        before = path.read_text(encoding="utf-8")

        mine = _rv_script_plan()
        mine["units"][0]["text"] = "@[裴与] 下马。"
        with pytest.raises(ScriptReviewError) as exc:
            await svc.save_content("demo", 1, mine, stale)
        assert exc.value.code == "conflict"
        assert path.read_text(encoding="utf-8") == before

        fresh = (await svc.get_state("demo", 1))["fingerprint"]
        state = await svc.save_content("demo", 1, mine, fresh)
        assert state["status"] == "pending_review"
        assert json.loads(path.read_text(encoding="utf-8"))["units"][0]["text"] == "@[裴与] 下马。"

    async def test_save_with_stale_fingerprint_conflicts_drama(self, tmp_path):
        """drama/narration web saves are protected by the same baseline comparison: the same conflict error code.

        drama/narration 的 web 保存同样受基线比对保护：同一个 conflict 错误码。"""
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        path = _write_script_plan(pm, "drama", _drama_script_plan())
        stale = (await svc.get_state("demo", 1))["fingerprint"]

        other = _drama_script_plan()
        other["title"] = "另一方改的标题"
        await svc.save_content("demo", 1, other)
        before = path.read_text(encoding="utf-8")

        with pytest.raises(ScriptReviewError) as exc:
            await svc.save_content("demo", 1, _drama_script_plan(), stale)
        assert exc.value.code == "conflict"
        assert path.read_text(encoding="utf-8") == before

    async def test_save_without_fingerprint_skips_baseline_check(self, tmp_path, video_request_facts):
        """Direct calls without a baseline fingerprint keep the original semantics: no comparison, written directly.

        不带基线指纹的直连调用维持原语义：不比对、直接落盘。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        _write_rv_script_plan(pm, _rv_script_plan())
        other = _rv_script_plan()
        other["units"][0]["text"] = "@[阿离] 转身离开。"
        await svc.save_content("demo", 1, other)

        state = await svc.save_content("demo", 1, _rv_script_plan())
        assert state["status"] == "pending_review"

    async def test_rv_save_clears_stale_prompt_authoring_quarantine_on_change(self, tmp_path, video_request_facts):
        """A web save that changes script_plan content -> the present prompt_authoring draft is voided (its
        structure-preserving diff is based on the old script_plan); a save with unchanged content does not clear it.
        Same exit and same semantics as Agent-side writes.

        web 保存改了 script_plan 内容 → 在场的 prompt_authoring 草稿作废（其保结构 diff 以旧 script_plan 为
        基底）；内容未变的保存不清。与 Agent 侧写盘同一出口、同一语义。"""
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        svc = _service(pm)
        project_path = pm.get_project_path("demo")
        _write_rv_script_plan(pm, _rv_script_plan())
        # First write the normalized shape (including model default fields) via one save, so the "content unchanged"
        # comparison has the same baseline
        # 先经一次保存把归一化形状（含模型默认字段）落盘，"内容未变"的比较才有同一基准
        await svc.save_content("demo", 1, _rv_script_plan())
        prompt_authoring_q = quarantine_path(project_path, 1, QUARANTINE_KIND_PROMPT_AUTHORING)

        write_quarantine(
            project_path, 1, QUARANTINE_KIND_PROMPT_AUTHORING, content={"units": [{"text": "旧基底"}]}, violations=[]
        )
        await svc.save_content("demo", 1, _rv_script_plan())  # Content unchanged (validation result matches the disk) / 内容未变（校验结果与盘上一致）
        assert prompt_authoring_q.exists()

        edited = _rv_script_plan()
        edited["units"][0]["text"] = "@[阿离] 转身离开。"
        await svc.save_content("demo", 1, edited)
        assert not prompt_authoring_q.exists()

    async def test_confirm_corrupt_script_plan_rejected(self, tmp_path):
        """Corrupt script_plan file (invalid JSON, but content_fingerprint still yields a hash) -> confirmation refused
        by structural validation.

        script_plan 文件损坏（非法 JSON，但 content_fingerprint 仍产哈希）→ 确认被结构校验拒绝。"""
        pm = _make_project(tmp_path, "drama")
        svc = _service(pm)
        path = _write_script_plan(pm, "drama", _drama_script_plan())
        path.write_bytes(b"\x00\x01 not json at all {")
        with pytest.raises(ScriptReviewError) as exc:
            await svc.confirm("demo", 1)
        assert exc.value.code == "invalid_content"


# ---------------------------------------------------------------------------
# Single write exit (lib.script.script_review.write_script_plan_locked)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 单一写盘出口（lib.script.script_review.write_script_plan_locked）
# ---------------------------------------------------------------------------


class TestScriptPlanWriteStore:
    def _project_path(self, tmp_path: Path) -> Path:
        pm = _make_project(tmp_path, "drama", generation_mode="reference_video")
        return pm.get_project_path("demo")

    def test_conflict_on_stale_baseline_keeps_file(self, tmp_path: Path):
        project_path = self._project_path(tmp_path)
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 1}]})
        stale = "0" * 64

        with (
            pytest.raises(script_review.ScriptPlanWriteConflict) as exc,
            script_review.script_plan_write_lock(project_path, 1),
        ):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 2}]}, expected_fingerprint=stale)

        assert exc.value.expected == stale
        assert exc.value.actual == script_review.content_fingerprint_of_data({"units": [{"v": 1}]})
        assert exc.value.current_content == {"units": [{"v": 1}]}
        path = script_review.official_reference_script_plan_path(project_path, 1)
        assert json.loads(path.read_text(encoding="utf-8")) == {"units": [{"v": 1}]}

    def test_matching_baseline_and_none_baseline_write(self, tmp_path: Path):
        """Matching baseline passes; a ``None`` baseline means "the file did not exist when the baseline was taken",
        and the first write also passes.

        基线一致放行；``None`` 基线表示「取基线时文件不存在」，首写同样放行。"""
        project_path = self._project_path(tmp_path)
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 1}]}, expected_fingerprint=None)
        current = script_review.content_fingerprint(script_review.official_reference_script_plan_path(project_path, 1))
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 2}]}, expected_fingerprint=current)
        path = script_review.official_reference_script_plan_path(project_path, 1)
        assert json.loads(path.read_text(encoding="utf-8")) == {"units": [{"v": 2}]}

    def test_none_baseline_conflicts_when_file_appeared(self, tmp_path: Path):
        """Baseline None (no final file when the baseline was taken) but the file was written by another party before
        the write -> conflict, no overwrite.

        基线 None（取基线时无正式文件）而写盘前文件已被另一方写出 → 冲突，不覆盖。"""
        project_path = self._project_path(tmp_path)
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 1}]})
        with (
            pytest.raises(script_review.ScriptPlanWriteConflict),
            script_review.script_plan_write_lock(project_path, 1),
        ):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 2}]}, expected_fingerprint=None)

    def test_prompt_authoring_quarantine_cleared_only_on_change(self, tmp_path: Path):
        project_path = self._project_path(tmp_path)
        prompt_authoring_q = quarantine_path(project_path, 1, QUARANTINE_KIND_PROMPT_AUTHORING)
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 1}]})

        # Content unchanged -> not cleared
        # 内容未变 → 不清
        write_quarantine(
            project_path, 1, QUARANTINE_KIND_PROMPT_AUTHORING, content={"units": [{"text": "基底"}]}, violations=[]
        )
        with script_review.script_plan_write_lock(project_path, 1):
            assert script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 1}]}) is False
        assert prompt_authoring_q.exists()

        # Content changed -> cleared
        # 内容变了 → 清
        with script_review.script_plan_write_lock(project_path, 1):
            assert script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 2}]}) is True
        assert not prompt_authoring_q.exists()

        # Migration write-back is treated as mechanical consolidation: not cleared even if the content changed
        # 迁移回写按机械收编处理：内容变了也不清
        write_quarantine(
            project_path, 1, QUARANTINE_KIND_PROMPT_AUTHORING, content={"units": [{"text": "基底"}]}, violations=[]
        )
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(
                project_path, 1, {"units": [{"v": 3}]}, clear_prompt_authoring_quarantine=False
            )
        assert prompt_authoring_q.exists()

    def test_successful_noop_refreshes_active_manifest_claim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        project_path = self._project_path(tmp_path)
        project_file = project_path / "project.json"
        project = json.loads(project_file.read_text(encoding="utf-8"))
        project["schema_version"] = CURRENT_PROJECT_SCHEMA_VERSION
        atomic_write_json(project_file, project)
        content = {"units": [{"v": 1}]}
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, content)

        registered: list[object] = []
        use_artifact_registration(monkeypatch, lambda _project_path, key: registered.append(key))

        with script_review.script_plan_write_lock(project_path, 1):
            assert script_review.write_script_plan_locked(project_path, 1, content) is False

        from lib.artifacts.artifact_manifest import ArtifactKey

        assert registered == [ArtifactKey.episode_script_plan(1)]

    def test_content_change_moves_project_activity_forward(self, tmp_path: Path):
        # The script plan is JSON; its modification time does not count as activity; the write exit does the
        # bookkeeping; writes with unchanged content do not count as activity.
        # 脚本规划是 JSON，修改时间不计入活动，由写盘出口记账；内容未变的写入不算活动。
        project_path = self._project_path(tmp_path)
        stale = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
        (project_path / ACTIVITY_FILENAME).write_text(stale.isoformat(), encoding="utf-8")

        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 1}]})
        recorded = recorded_project_activity(project_path)
        assert recorded is not None
        assert recorded > stale

        (project_path / ACTIVITY_FILENAME).write_text(stale.isoformat(), encoding="utf-8")
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 1}]})
        assert recorded_project_activity(project_path) == stale

    def test_failed_commit_leaves_project_activity_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # A content change voids downstream drafts; the draft-clearing exit records within the transaction; when
        # registration fails and rolls back, the ledger returns to its original value too.
        # 内容变化会作废下游草稿，清草稿的出口在事务内记账；登记失败回滚时账本一并回到原值。
        project_path = self._project_path(tmp_path)
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 1}]})
        write_quarantine(
            project_path, 1, QUARANTINE_KIND_PROMPT_AUTHORING, content={"units": [{"text": "基底"}]}, violations=[]
        )
        stale = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
        (project_path / ACTIVITY_FILENAME).write_text(stale.isoformat(), encoding="utf-8")

        def _fail(*_args: object) -> None:
            raise RuntimeError("registration failed")

        use_artifact_registration(monkeypatch, _fail)
        with (
            script_review.script_plan_write_lock(project_path, 1),
            pytest.raises(RuntimeError, match="registration failed"),
        ):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 2}]})

        assert quarantine_path(project_path, 1, QUARANTINE_KIND_PROMPT_AUTHORING).exists()
        assert recorded_project_activity(project_path) == stale

    def test_failed_commit_keeps_activity_recorded_by_other_writers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The ledger is project-global; writers such as drafts and memory do not go through the final file's lock:
        # a rollback must not erase their concurrent bookkeeping.
        # 账本是项目全局的，草稿、记忆等写入方不经过正式文件的锁：回滚时不能抹掉它们同期的记账。
        project_path = self._project_path(tmp_path)
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 1}]})
        stale = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
        (project_path / ACTIVITY_FILENAME).write_text(stale.isoformat(), encoding="utf-8")

        def _other_writer_then_fail(*_args: object) -> None:
            record_project_activity(project_path)
            raise RuntimeError("registration failed")

        use_artifact_registration(monkeypatch, _other_writer_then_fail)
        with (
            script_review.script_plan_write_lock(project_path, 1),
            pytest.raises(RuntimeError, match="registration failed"),
        ):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 2}]})

        recorded = recorded_project_activity(project_path)
        assert recorded is not None
        assert recorded > stale

    def test_deleting_the_script_plan_moves_project_activity_forward(self, tmp_path: Path):
        project_path = self._project_path(tmp_path)
        with script_review.script_plan_write_lock(project_path, 1):
            script_review.write_script_plan_locked(project_path, 1, {"units": [{"v": 1}]})
        stale = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
        (project_path / ACTIVITY_FILENAME).write_text(stale.isoformat(), encoding="utf-8")

        path = script_review.official_reference_script_plan_path(project_path, 1)
        assert script_review.delete_script_plan_file(project_path, 1, path) is True

        recorded = recorded_project_activity(project_path)
        assert recorded is not None
        assert recorded > stale


# ---------------------------------------------------------------------------
# prompt_authoring and content review: authoring is not blocked by the review gate; the confirm tool uses the
# same service
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# prompt_authoring 与内容确认：编写不受确认门禁阻塞，确认工具走同一服务
# ---------------------------------------------------------------------------


class TestPromptAuthoringEnforcement:
    async def test_pending_review_does_not_block_authoring_the_formal_script(self, tmp_path, video_request_facts):
        """Authoring only reads the final script: after a script_plan rerun not yet confirmed, the authoring entry is
        released as usual.

        编写只读正式剧本：script_plan 重跑后尚未确认时，编写入口照常放行。"""
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())
        _write_script(pm, _narration_script({**_narration_script_segment("E1S01"), "pending_authoring": True}))
        pm.save_script(
            "demo",
            _narration_script({**_narration_script_segment("E1S01"), "pending_authoring": True}),
            "episode_1.json",
        )
        pm.update_project(
            "demo", lambda p: script_review.apply_confirmation(p, 1, "sha256-v1:" + "0" * 64, "2026-01-01T00:00:00Z")
        )
        project_path = pm.get_project_path("demo")
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "pending_review"

        ctx = ToolHarness(
            project_name="demo",
            data_root=tmp_path / "projects",
            pm=pm,
            config_resolver=cast(ConfigResolver, FakeConfigResolver()),
        )
        result = await run_declared_tool(GENERATE_EPISODE_SCRIPT, ctx, {"episode_id": 1, "dry_run": True})

        assert isinstance(result.value, TextGenerationResult), result
        assert "DRY RUN" in result.value.message

    async def test_confirm_tool_unblocks_prompt_authoring(self, tmp_path, video_request_facts):
        """Agent path: after the confirm_script_review tool confirms, the gate releases (the existing
        script_plan->prompt_authoring flow is not broken).

        Agent 路径：confirm_script_review 工具确认后，gate 放行（既有 script_plan→prompt_authoring 不被破坏）。"""
        pm = _make_project(tmp_path, "drama")
        _write_script_plan(pm, "drama", _admitted_drama_script_plan())
        project_path = pm.get_project_path("demo")
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "pending_review"

        ctx = ToolHarness(
            project_name="demo",
            data_root=tmp_path / "projects",
            pm=pm,
            config_resolver=cast(ConfigResolver, FakeConfigResolver()),
        )
        result = await run_declared_tool(CONFIRM_SCRIPT_REVIEW, ctx, {"episode_id": 1})

        assert result.problem is None, result
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "confirmed"

    async def test_confirm_tool_refusal_carries_the_same_loss_text_as_the_web_state(
        self, tmp_path, video_request_facts
    ):
        """The loss listing the Agent receives and the one the Web confirmation dialog reads are the same server-side
        text; the token is separately in params.

        Agent 收到的丢失清单与 Web 确认框读的是同一份服务端文本，令牌另在 params 里。"""
        pm = _make_project(tmp_path, "narration")
        _write_script_plan(pm, "narration", _narration_script_plan())
        _write_script(
            pm,
            _narration_script(
                _narration_script_segment(
                    "E1S01",
                    end_frame_image="end_frames/scene_E1S01.png",
                    generated_assets={"narration_audio": "audio/segment_E1S01.wav", "grid_id": "grid_ab12"},
                )
            ),
        )
        web_overwrite = script_review.overwrite_with_text(
            (await _service(pm).get_state("demo", 1))["script_overwrite"], i18n_message
        )
        assert web_overwrite is not None
        ctx = ToolHarness(
            project_name="demo",
            data_root=tmp_path / "projects",
            pm=pm,
            config_resolver=cast(ConfigResolver, FakeConfigResolver()),
        )

        refused = await run_declared_tool(CONFIRM_SCRIPT_REVIEW, ctx, {"episode_id": 1})

        assert refused.problem is not None
        assert refused.problem.code == "script_overwrite_required"
        assert refused.problem.params == {"script_overwrite": web_overwrite}
        assert web_overwrite["text"] in refused.problem.detail
        for lost in ("配音 1 段", "尾帧 1 张", "宫格归属 1 处"):
            assert lost in web_overwrite["text"]

    async def test_confirm_tool_reports_the_video_request_facts_problem(self, tmp_path, set_video_request_facts):
        """Agent path: when video request facts cannot be resolved, the confirmation receipt carries the facts'
        problem code and params, not just an internal error category.

        Agent 路径：视频请求事实解析不出时，确认回执带事实的问题码与参数，不止于内部错误类别。"""
        set_video_request_facts(
            VideoRequestFactsFailure(
                "video_supported_durations_incompatible",
                (("provider", "p"), ("model", "m"), ("resolution", "1080p"), ("capability", "i2v")),
            )
        )
        pm = _make_project(tmp_path, "drama")
        _write_script_plan(pm, "drama", _admitted_drama_script_plan())
        project_path = pm.get_project_path("demo")
        ctx = ToolHarness(
            project_name="demo",
            data_root=tmp_path / "projects",
            pm=pm,
            config_resolver=cast(ConfigResolver, FakeConfigResolver()),
        )

        result = await run_declared_tool(CONFIRM_SCRIPT_REVIEW, ctx, {"episode_id": 1})

        assert result.problem is not None
        text = result.problem.detail
        assert "video_supported_durations_incompatible" in text
        assert "resolution=1080p" in text
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "pending_review"


# ---------------------------------------------------------------------------
# Existing-data enumeration: gate-derived states of {script_plan yes/no x prompt_authoring yes/no x
# script_plan_review yes/no}
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 存量穷举：{script_plan 有无 × prompt_authoring 有无 × script_plan_review 有无} 的 gate 派生态
# ---------------------------------------------------------------------------


class TestLegacyEnumeration:
    async def test_no_script_plan(self, tmp_path):
        pm = _make_project(tmp_path, "drama")
        assert (await _service(pm).get_state("demo", 1))["status"] == "no_script_plan"

    async def test_script_plan_no_prompt_authoring_no_review_pending(self, tmp_path):
        """First script_plan produced after the feature (no prompt_authoring, no confirmation) -> pending review,
        blocked.

        feature 后首次产 script_plan（未产 prompt_authoring、无确认）→ 待确认、阻塞。"""
        pm = _make_project(tmp_path, "drama")
        _write_script_plan(pm, "drama", _drama_script_plan())
        assert (await _service(pm).get_state("demo", 1))["status"] == "pending_review"

    async def test_script_plan_prompt_authoring_no_review_pending(self, tmp_path):
        """script_plan + prompt_authoring produced, no confirmation record -> no way to tell whether the final script
        came from this plan, pending review.

        已产 script_plan + prompt_authoring、无确认记录 → 无从判断正式脚本出自这份规划，待确认。"""
        pm = _make_project(tmp_path, "drama")
        _write_script_plan(pm, "drama", _drama_script_plan())
        _write_prompt_authoring(pm)
        project_path = pm.get_project_path("demo")
        assert (await _service(pm).get_state("demo", 1))["status"] == "pending_review"
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "pending_review"

    async def test_script_plan_prompt_authoring_review_matching_confirmed(self, tmp_path, video_request_facts):
        pm = _make_project(tmp_path, "drama")
        _write_script_plan(pm, "drama", _admitted_drama_script_plan())
        _write_prompt_authoring(pm)
        await _confirm_over_existing_script(pm)
        assert (await _service(pm).get_state("demo", 1))["status"] == "confirmed"

    async def test_script_plan_prompt_authoring_review_mismatch_pending(self, tmp_path, video_request_facts):
        """script_plan rerun after confirmation (even with prompt_authoring present) -> pending review again; the
        fingerprint takes precedence over grandfather.

        已确认后 script_plan 又被重跑（即便 prompt_authoring 在）→ 重新等待确认，指纹优先于 grandfather。"""
        pm = _make_project(tmp_path, "drama")
        _write_script_plan(pm, "drama", _admitted_drama_script_plan())
        _write_prompt_authoring(pm)
        await _confirm_over_existing_script(pm)
        rerun = _admitted_drama_script_plan()
        rerun["scenes"][0]["source_text"] = "改写后的原文锚"
        _write_script_plan(pm, "drama", rerun)
        assert (await _service(pm).get_state("demo", 1))["status"] == "pending_review"


# ---------------------------------------------------------------------------
# Manual pre-split self-heal: when the episodes[] ledger is empty but derived source/episode_N.txt files
# exist, _require_episode self-heals by adding entries instead of declaring a deadlock.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 手动预拆分自愈：episodes[] 账本为空但 source/episode_N.txt 派生文件已存在时，
# _require_episode 自愈补建条目而非直接判死锁。
# ---------------------------------------------------------------------------


class TestOwnSourceEpisodes:
    async def test_unregistered_episode_file_is_not_an_episode(self, tmp_path):
        """An unregistered episode_N.txt in source/ is not added as an episode: reading the state reports
        episode_not_found and the ledger is untouched.

        source/ 里未登记的 episode_N.txt 不会被补登为一集：读状态报 episode_not_found，账本不动。"""
        pm = _make_manual_split_project(tmp_path, "narration")
        _write_source_text(pm, "episode_1.txt", "裴与出征后的第二年。")

        with pytest.raises(ScriptReviewError) as exc:
            await _service(pm).get_state("demo", 1)

        assert exc.value.code == "episode_not_found"
        assert pm.load_project("demo")["episodes"] == []

    async def test_confirm_unblocks_prompt_authoring_for_an_own_source_episode(self, tmp_path, video_request_facts):
        """An episode registered one by one with its own source goes through the same content review exit.

        逐集登记的自带原文的集走同一条内容确认出口。"""
        pm = _make_manual_split_project(tmp_path, "drama")
        pm.add_character("demo", "阿离", "少女")
        assert register_project_sources(pm, "demo", own_episodes=("任意原文内容",)) == [1]
        _write_script_plan(pm, "drama", _admitted_drama_script_plan())

        confirmed = await _service(pm).confirm("demo", 1)
        assert confirmed["status"] == "confirmed"

        project_path = pm.get_project_path("demo")
        assert script_review.review_status(project_path, pm.load_project("demo"), 1) == "confirmed"
        ep = script_review.find_episode(pm.load_project("demo"), 1)
        assert ep is not None
        assert ep["source_origin"] == "own"
        assert "source_range" not in ep
