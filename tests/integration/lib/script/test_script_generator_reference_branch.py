"""Tests for the ScriptGenerator reference_video branch.

Prompt authoring reads only the final script's video_units: by default it rewrites the bodies of units with
pending-authoring marks; ``entry_ids`` limits the scope and ``rewrite`` rewrites explicitly; the script plan is only
read in the content review conversion (``materialize_script_plan``).

ScriptGenerator reference_video 分支测试。

提示词编写只读正式剧本的 video_units：默认改写带待编写标记的单元正文，``entry_ids`` 划定范围、``rewrite`` 显式重写；
脚本规划只在内容确认转换（``materialize_script_plan``）里被读取。
"""

import asyncio
import json as _json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from lib.artifacts.artifact_activation import activate_artifact_target_state
from lib.config.resolver import ConfigResolver
from lib.generation.video_request_facts import VideoRequestFactsError, VideoRequestFactsFailure
from lib.project import project_manager as project_manager_module
from lib.project.project_manager import ProjectManager, ScriptWriteConflict
from lib.project.project_schema import CURRENT_PROJECT_SCHEMA_VERSION
from lib.script import script_review
from lib.script.draft_quarantine import (
    QUARANTINE_KIND_PROMPT_AUTHORING,
    QUARANTINE_KIND_SCRIPT_PLAN,
    quarantine_path,
    write_quarantine,
)
from lib.script.reference_video.draft_validation import DraftViolation
from lib.script.reference_video.request_projection import configured_reference_request_facts
from lib.script.reference_video.text_parser import extract_mentions
from lib.script.reference_video.unit_capabilities import evaluate_reference_unit_capabilities
from lib.script.script_generator import (
    PlanningVideoFacts,
    PromptAuthoringUnitRejected,
    ScriptGenerator,
    UnitDurationOffTier,
)
from server.services.project.episode_drafts import EpisodeDraftService
from tests.factories import make_video_request_facts
from tests.fakes import use_text_generator

SCRIPT_PLAN_UNIT = {"unit_id": "E1U01", "text": "@[主角] 推开 @[酒馆] 的门", "duration_seconds": 4}
SCRIPT_PLAN_UNITS_JSON = _json.dumps({"units": [SCRIPT_PLAN_UNIT]}, ensure_ascii=False)


def _prompt_authoring_response(*texts: str, title: str = "t") -> str:
    """prompt_authoring LLM output: flat ``{title, units: [{text}]}`` -- unit_id and duration are not in the output.

    prompt_authoring 的 LLM 产出：扁平 ``{title, units: [{text}]}``——unit_id 与时长不进输出。"""
    return _json.dumps({"title": title, "units": [{"text": t} for t in texts]}, ensure_ascii=False)


def _fake_prompt_authoring_generator(*texts: str) -> MagicMock:
    generator = MagicMock()
    generator.model = "mock"
    generator.generate = AsyncMock(return_value=MagicMock(text=_prompt_authoring_response(*texts)))
    return generator


def _idle_generator() -> MagicMock:
    """A text generation double whose call can be asserted: cases use it to tell that interception happened before the
    paid call.

    被调用即可断言出来的文本生成替身：用例据此判断拦截发生在付费调用之前。"""
    generator = MagicMock()
    generator.model = "mock"
    generator.generate = AsyncMock()
    return generator


#: Valid prompt authoring corresponding to ``SCRIPT_PLAN_UNIT``: no dialogue to change.
#: 与 ``SCRIPT_PLAN_UNIT`` 对应的合法提示词编写：无台词可改。
PROMPT_AUTHORING_UNIT_TEXT = "镜头1：中景，平视。@[主角] 推开 @[酒馆] 的门，侧身跨过门槛。"


def _activate_project_artifacts(project_dir: Path, episode: int = 1) -> None:
    """After filling in the episode's provenance inputs, do a full artifact activation on the project.

    The artifact manifest is the only rule for reading generated artifacts: being on disk does not mean registered, and
    an unregistered script_plan cannot enter conversion. ``episode`` only decides which episode's
    ``source/episode_{episode}.txt`` is filled in; the registration scope is the whole project.

    补齐该集的溯源输入后，对项目做一次全量产物激活。

    产物清单是读取已生成产物的唯一口径：落盘本身不代表已登记，未登记的 script_plan 进不了转换。
    ``episode`` 只决定补写哪一集的 ``source/episode_{episode}.txt``；登记范围是整个项目。
    """
    source = project_dir / "source" / f"episode_{episode}.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("原文", encoding="utf-8")
    activate_artifact_target_state(project_dir, bump_schema=False)


def _claim_character_sheet(project_dir: Path, name: str) -> None:
    """Write an asset sheet for a registered character and claim it via artifact activation: only then are units whose
    body mentions it bucketed as r2v.

    给已登记角色落一张资产图并经产物激活认领：正文提及它的单元据此才按 r2v 定桶。"""
    sheet = project_dir / "characters" / f"{name}.png"
    sheet.parent.mkdir(parents=True, exist_ok=True)
    sheet.write_bytes(b"png")
    project_file = project_dir / "project.json"
    project = _json.loads(project_file.read_text(encoding="utf-8"))
    project["characters"].setdefault(name, {"description": "d"})["character_sheet"] = f"characters/{name}.png"
    project_file.write_text(_json.dumps(project, ensure_ascii=False), encoding="utf-8")
    _activate_project_artifacts(project_dir)


def _write_script_plan(project_dir: Path, payload: str, episode: int = 1) -> None:
    """Write a formal script_plan and register it in the artifact manifest.

    写正式 script_plan 并登记进产物清单。"""
    path = project_dir / "drafts" / f"episode_{episode}" / "script_plan_reference_units.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    _activate_project_artifacts(project_dir, episode)


def _script_path(project: Path) -> Path:
    return project / "scripts" / "episode_1.json"


def _write_formal_units(project_dir: Path, units: list[dict[str, Any]], *, title: str = "第1集") -> Path:
    """Write a final script via the unified write entry; units carry the pending-authoring mark by default, except those
    given ``pending_authoring`` explicitly.

    经写盘统一入口落一份正式剧本；单元默认带待编写标记，显式给出 ``pending_authoring`` 的除外。"""
    project = _json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    script = {
        "title": title,
        "episode": 1,
        "content_mode": project["content_mode"],
        "novel": {"title": project["title"], "chapter": "第1集"},
        "video_units": [{"pending_authoring": True, **unit} for unit in units],
    }
    return ProjectManager.for_project_dir(project_dir).save_script(project_dir.name, script, "episode_1.json")


def _formal_units(project_dir: Path) -> dict[str, dict[str, Any]]:
    script = _json.loads(_script_path(project_dir).read_text(encoding="utf-8"))
    return {unit["unit_id"]: unit for unit in script["video_units"]}


@pytest.fixture(autouse=True)
def reference_request_facts(set_video_request_facts):
    """Duration options in this module always come from video request facts: by default both buckets share (4, 6, 8);
    cases needing different or failing buckets override in place.

    本模块的档位一律来自视频请求事实：缺省两桶同一份 (4, 6, 8)，需要两桶不同或失败的用例就地覆盖。"""
    set_video_request_facts(make_video_request_facts(route="reference_video", generation_type="i2v"))


async def _materialize(generator: ScriptGenerator, episode: int = 1):
    """Do the content review conversion with the current plan and current final script as acknowledged objects; when the
    plan is absent the loader errors first.

    以当前规划与当前正式剧本为认可对象做内容确认转换；规划缺席时由加载器先行报错。"""
    plan_path = generator.project_path / "drafts" / f"episode_{episode}" / "script_plan_reference_units.json"
    script_path = generator.project_path / "scripts" / f"episode_{episode}.json"
    return await generator.materialize_script_plan(
        episode,
        expected_plan_revision=script_review.content_fingerprint(plan_path) or "",
        expected_script_fingerprint=script_review.content_fingerprint(script_path),
        project_update=lambda _project: None,
    )


def _write_reference_project(
    tmp_path: Path, *, video_backend: str, content_mode: str = "narration", resolution: str | None = None
) -> Path:
    """Build a minimal reference-to-video project with a script plan.

    ``video_backend`` / ``resolution`` are only the project's self-declared identity; the duration options planning
    reads come from the video request facts each case provides.

    造一个带脚本规划的参考生视频最小项目。

    ``video_backend`` / ``resolution`` 只是项目自报身份；规划读到的档位来自各用例提供的视频请求事实。
    """
    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    (project_dir / "project.json").write_text(
        """{
          "schema_version": __SCHEMA__,
          "title": "t",
          "content_mode": "__MODE__",
          "generation_mode": "reference_video",
          "video_backend": "__BACKEND__",
          "overview": {"synopsis": "s", "genre": "g", "theme": "th", "world_setting": "w"},
          "style": "国漫",
          "style_description": "水墨",
          "characters": {"主角": {"description": "d"}},
          "scenes": {"酒馆": {"description": "d"}},
          "props": {},
          "episodes": [
            {"episode": 1, "title": "t1", "script_file": "scripts/episode_1.json",
             "generation_mode": "reference_video"}
          ]
        }""".replace("__SCHEMA__", str(CURRENT_PROJECT_SCHEMA_VERSION))
        .replace("__BACKEND__", video_backend)
        .replace("__MODE__", content_mode),
        encoding="utf-8",
    )
    if resolution is not None:
        project_file = project_dir / "project.json"
        project = _json.loads(project_file.read_text(encoding="utf-8"))
        project["model_settings"] = {video_backend: {"resolution": resolution}}
        project_file.write_text(_json.dumps(project, ensure_ascii=False), encoding="utf-8")
    _write_script_plan(project_dir, SCRIPT_PLAN_UNITS_JSON)
    return project_dir


@pytest.fixture
def plan_only_reference_project(tmp_path: Path) -> Path:
    """A project with only a script plan and no final script yet: the input of the content review conversion.

    只有脚本规划、尚无正式剧本的项目：内容确认转换的输入。"""
    return _write_reference_project(tmp_path, video_backend="vidu/vidu2.0", resolution="1080p")


@pytest.fixture
def reference_project(plan_only_reference_project: Path) -> Path:
    """A project whose final script has one unit pending authoring (``SCRIPT_PLAN_UNIT``).

    正式剧本里有一个待编写单元（``SCRIPT_PLAN_UNIT``）的项目。"""
    _write_formal_units(plan_only_reference_project, [SCRIPT_PLAN_UNIT])
    return plan_only_reference_project


@pytest.fixture
def wide_tier_reference_project(tmp_path: Path) -> Path:
    """Project for cases where the two buckets have different options: r2v 3-16 seconds, i2v 1-16 seconds, with facts
    provided per bucket by each case.

    Reference image constraints only narrow, so "with-image options stricter than without" is the only meaningful
    direction. Each case writes its own final script.

    两桶档位不同的用例用的项目：r2v 3–16 秒、i2v 1–16 秒由各用例按桶提供事实。

    参考图约束只做收窄，故「带图档位严于不带图」是唯一有意义的方向。正式剧本由各用例自行写入。
    """
    return _write_reference_project(tmp_path, video_backend="vidu/viduq3-turbo")


@pytest.mark.asyncio
async def test_build_prompt_renders_pending_formal_units(reference_project: Path):
    gen = ScriptGenerator(reference_project)
    prompt = await gen.build_prompt(episode=1)
    # The body of the pending unit in the final script enters the prompt verbatim, together with the unit duration
    # 正式剧本里待编写单元的正文逐字进入 prompt，与 unit 时长一起
    assert "@[主角] 推开 @[酒馆] 的门" in prompt
    assert "（时长 4s）" in prompt
    # unit_id is not sent to prompt_authoring: output aligns by position
    # unit_id 不下发给 prompt_authoring：产出按位对齐
    assert "E1U01" not in prompt


@pytest.mark.asyncio
async def test_prompt_authoring_ignores_the_script_plan(reference_project: Path):
    """Authoring reads only the final script: with the script plan missing, or disagreeing with the final script,
    authoring proceeds as usual.

    编写只读正式剧本：脚本规划缺失、或与正式剧本不一致，编写照常进行。"""
    plan = reference_project / "drafts" / "episode_1" / "script_plan_reference_units.json"
    plan.write_text(
        _json.dumps({"units": [{"unit_id": "E1U09", "text": "规划里另一段正文", "duration_seconds": 8}]}),
        encoding="utf-8",
    )
    prompt = await ScriptGenerator(reference_project).build_prompt(episode=1)
    assert "@[主角] 推开 @[酒馆] 的门" in prompt
    assert "规划里另一段正文" not in prompt

    plan.unlink()
    out = await ScriptGenerator(
        reference_project, generator=_fake_prompt_authoring_generator(PROMPT_AUTHORING_UNIT_TEXT)
    ).generate(episode=1)
    assert _formal_units(reference_project)["E1U01"]["text"] == PROMPT_AUTHORING_UNIT_TEXT
    assert out == _script_path(reference_project)


@pytest.mark.asyncio
async def test_reference_prompt_authoring_ends_with_instructions_on_both_paths(reference_project: Path):
    gen = ScriptGenerator(reference_project)
    assert "# 附加指令" not in await gen.build_prompt(episode=1)
    preview = await gen.build_prompt(episode=1, instructions="多给人物面部特写")
    assert preview.endswith("逐 unit 产出。\n\n# 附加指令\n多给人物面部特写")

    fake_generator = _fake_prompt_authoring_generator(PROMPT_AUTHORING_UNIT_TEXT)
    await ScriptGenerator(reference_project, generator=fake_generator).generate(
        episode=1, instructions="多给人物面部特写"
    )
    assert fake_generator.generate.await_args.args[0].prompt == preview


@pytest.mark.asyncio
async def test_script_generator_uses_reference_schema_on_generate(reference_project: Path):
    """prompt_authoring produces bodies with a flat schema, writes them back to pending units and clears the mark;
    unit_id and duration carry over from the final script.

    prompt_authoring 用扁平 schema 出正文，写回待编写单元并清除标记；unit_id 与时长沿用正式剧本。"""
    from lib.script.script_models import ReferencePromptAuthoringFlatScript

    fake_generator = _fake_prompt_authoring_generator(PROMPT_AUTHORING_UNIT_TEXT)

    out = await ScriptGenerator(reference_project, generator=fake_generator).generate(episode=1)

    data = _json.loads(out.read_text(encoding="utf-8"))
    # A reference-to-video script's content_mode inherits the project-level narration/drama; generation mode is a
    # project-level fact, and the script stores no generation mode marker.
    # 参考生视频剧本 content_mode 继承项目级 narration/drama；生成模式是项目级事实，
    # 剧本不落盘任何生成模式标记。
    assert data["content_mode"] == "narration"
    assert "generation_mode" not in data
    # Episode total duration is not stored: it is a derived per-unit sum computed by the project summary on read
    # 集总时长不落盘：它是逐 unit 求和的派生值，由项目摘要读时计算
    assert "duration_seconds" not in data
    assert len(data["video_units"]) == 1
    unit = data["video_units"][0]
    assert unit["unit_id"] == "E1U01"
    assert unit["duration_seconds"] == 4
    assert unit["text"].startswith("镜头1：中景，平视。")
    assert "pending_authoring" not in unit
    assert extract_mentions(unit["text"]) == ["主角", "酒馆"]

    # response_schema is flat and does not contain duration_seconds -- the LLM is not asked to write durations
    # response_schema 是扁平形状，且不含 duration_seconds——时长没让 LLM 写
    schema = fake_generator.generate.await_args.args[0].response_schema
    assert schema is ReferencePromptAuthoringFlatScript
    assert "duration_seconds" not in _json.dumps(schema.model_json_schema())


@pytest.mark.asyncio
async def test_prompt_authoring_rewrites_only_pending_units(reference_project: Path):
    """Authored units without the mark stay byte-identical; only bodies of pending units are rewritten, other fields kept
    as is.

    已编写、无标记的单元逐字节不变；只有待编写单元的正文被改写，其余字段原样保留。"""
    authored = {
        "unit_id": "E1U01",
        "text": "镜头1：远景。@[主角] 站在 @[酒馆] 门口。",
        "duration_seconds": 4,
        "note": "用户备注",
        "pending_authoring": False,
    }
    pending = {
        "unit_id": "E1U02",
        "text": "@[主角] 走进 @[酒馆]",
        "duration_seconds": 4,
        "note": "新增单元",
        "source_text": "他推门走进酒馆。",
    }
    _write_formal_units(reference_project, [authored, pending])
    before = _formal_units(reference_project)

    rewritten: list[str] = []
    new_text = "镜头1：中景。@[主角] 走进 @[酒馆]，环顾四周。"
    fake_generator = _fake_prompt_authoring_generator(new_text)
    await ScriptGenerator(reference_project, generator=fake_generator).generate(
        episode=1, rewritten_entry_ids=rewritten
    )

    assert rewritten == ["E1U02"]
    prompt = fake_generator.generate.await_args.args[0].prompt
    assert "@[主角] 走进 @[酒馆]" in prompt
    after = _formal_units(reference_project)
    assert _json.dumps(after["E1U01"], sort_keys=True) == _json.dumps(before["E1U01"], sort_keys=True)
    assert after["E1U02"] == {
        "unit_id": "E1U02",
        "text": new_text,
        "duration_seconds": 4,
        "note": "新增单元",
        "source_text": "他推门走进酒馆。",
    }


@pytest.mark.asyncio
async def test_prompt_authoring_without_pending_units_skips_the_model(reference_project: Path):
    _write_formal_units(reference_project, [{**SCRIPT_PLAN_UNIT, "pending_authoring": False}])
    before = _script_path(reference_project).read_bytes()
    generator = _idle_generator()

    rewritten: list[str] = ["stale"]
    out = await ScriptGenerator(reference_project, generator=generator).generate(
        episode=1, rewritten_entry_ids=rewritten
    )

    assert out == _script_path(reference_project)
    assert rewritten == []
    generator.generate.assert_not_awaited()
    assert _script_path(reference_project).read_bytes() == before


@pytest.mark.asyncio
async def test_entry_ids_rewrite_an_authored_unit_keeping_other_fields(reference_project: Path):
    authored = {**SCRIPT_PLAN_UNIT, "text": "镜头1：远景。@[主角] 推开 @[酒馆] 的门。", "note": "保留"}
    _write_formal_units(reference_project, [{**authored, "pending_authoring": False}])

    await ScriptGenerator(
        reference_project, generator=_fake_prompt_authoring_generator(PROMPT_AUTHORING_UNIT_TEXT)
    ).generate(
        episode=1,
        entry_ids=["E1U01"],
        rewrite=True,
        overwrite_revision=script_review.content_fingerprint(reference_project / "scripts" / "episode_1.json"),
    )

    unit = _formal_units(reference_project)["E1U01"]
    assert unit == {"unit_id": "E1U01", "text": PROMPT_AUTHORING_UNIT_TEXT, "duration_seconds": 4, "note": "保留"}


@pytest.mark.asyncio
async def test_prompt_authoring_rejects_formal_duration_outside_effective_tiers(
    reference_project: Path, set_video_request_facts
):
    """The full option set [4, 6, 8] is narrowed to [4] by the resolution constraint: an 8-second unit in the final script
    is no longer valid. It must not be silently snapped and written; it must fail loud and point to the timeline.

    Interception must happen before the TextBackend call: when neither the with-reference nor the without-reference
    effective options accept the duration, this authoring is bound to fail; catching it only at output parsing means
    the user has already paid for it.

    档位全集 [4, 6, 8] 被分辨率约束收窄到 [4]：正式剧本里 8 秒的单元不再是合法值。
    不能静默取档改写落盘，须 fail-loud 并指向时间线。

    拦截须发生在 TextBackend 调用之前：带引用与不带引用两种生效档位都不接受该时长时，本次编写
    必然失败；放到输出解析阶段才拦，用户已经为它付了费。
    """
    set_video_request_facts(
        make_video_request_facts(
            route="reference_video", generation_type="i2v", supported_durations=(4, 6, 8), allowed_durations=(4,)
        )
    )
    _write_formal_units(
        reference_project,
        [
            {**SCRIPT_PLAN_UNIT, "pending_authoring": False},
            {**SCRIPT_PLAN_UNIT, "unit_id": "E1U04", "duration_seconds": 8},
        ],
    )
    generator = _idle_generator()

    gen = ScriptGenerator(reference_project, generator=generator)
    with pytest.raises(PromptAuthoringUnitRejected, match=r"not in the active duration tiers.*timeline") as exc_info:
        await gen.generate(episode=1)
    generator.generate.assert_not_awaited()
    # Classified as an input to fix; the unit is named by its UI position (the 2nd), with the current duration and the
    # available options.
    # 归为修改输入，按界面位置称呼单元（第 2 个），带上现时长与可选档位。
    assert (exc_info.value.code, exc_info.value.params) == (
        "prompt_authoring_duration_off_tier",
        {"unit_id": "E1U04", "position": 2, "duration_seconds": 8, "tiers": "4"},
    )


@pytest.mark.asyncio
async def test_no_image_five_second_unit_uses_i2v_facts_through_conversion_and_authoring(
    tmp_path: Path, set_video_request_facts
):
    project = _write_reference_project(tmp_path, video_backend="gemini-aistudio/veo-3.1-generate-preview")
    _write_script_plan(
        project,
        _json.dumps(
            {
                "units": [
                    {
                        "unit_id": "E1U01",
                        "text": "镜头1：空镜，街道渐亮。",
                        "duration_seconds": 5,
                        "source_text": "原文",
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    set_video_request_facts(
        make_video_request_facts(
            route="reference_video",
            generation_type="i2v",
            provider_id="ark",
            model_id="doubao-seedance-2-0-260128",
            supported_durations=tuple(range(4, 16)),
            allowed_durations=tuple(range(4, 16)),
        )
    )

    generator = ScriptGenerator(
        project,
        generator=_fake_prompt_authoring_generator("镜头1：远景，晨光照亮空旷街道。"),
    )

    await _materialize(generator)
    await generator.generate(episode=1)

    assert _formal_units(project)["E1U1"]["duration_seconds"] == 5


@pytest.mark.asyncio
async def test_no_image_conversion_rejects_unavailable_i2v_even_when_r2v_accepts_duration(
    tmp_path: Path, set_video_request_facts
):
    project = _write_reference_project(tmp_path, video_backend="gemini-aistudio/veo-3.1-generate-preview")
    _write_script_plan(
        project,
        _json.dumps({"units": [{"unit_id": "E1U01", "text": "镜头1：空镜，街道渐亮。", "duration_seconds": 8}]}),
    )
    set_video_request_facts(
        {
            "r2v": make_video_request_facts(route="reference_video", generation_type="r2v"),
            "i2v": VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "i2v"),)),
        }
    )

    with pytest.raises(VideoRequestFactsError) as exc:
        await _materialize(ScriptGenerator(project))

    assert exc.value.code == "reference_capability_unavailable"
    assert not _script_path(project).exists()


@pytest.mark.asyncio
async def test_script_generator_narrows_duration_tiers_per_unit_not_episode_wide(
    wide_tier_reference_project: Path, set_video_request_facts
):
    """In the same episode one unit has reference images (narrowed to 3-16 seconds) and the other has none (still
    1-16 seconds): the latter's already valid 2 seconds must not be changed to 3 because of the former's narrowing --
    snapping must recompute the effective options from each unit's own reference image state, not apply the coarse
    set narrowed by an episode-level any(...).

    同集内一个 unit 带参考图（收窄到 3–16 秒）、另一个不带（仍是 1–16 秒）：后者本已合法的
    2 秒不应因前者的收窄被连带改成 3——取档须按每个 unit 自己的参考图状态重算生效档位，
    不套用 episode 级 any(...) 收窄出的粗粒度集合。
    """
    set_video_request_facts(
        make_video_request_facts(
            route="reference_video",
            generation_type="i2v",
            supported_durations=tuple(range(1, 17)),
            allowed_durations=tuple(range(1, 17)),
        )
    )
    project = wide_tier_reference_project
    _write_formal_units(
        project,
        [
            {"unit_id": "E1U01", "text": "@[主角] 推门", "duration_seconds": 3},
            {"unit_id": "E1U02", "text": "空镜", "duration_seconds": 2},
        ],
    )

    fake_generator = _fake_prompt_authoring_generator("镜头1：中景。@[主角] 推门", "镜头1：空镜，风吹过门廊")
    gen = ScriptGenerator(project, generator=fake_generator)
    await gen.generate(episode=1)

    units_by_id = _formal_units(project)
    assert units_by_id["E1U01"]["duration_seconds"] == 3
    # Not changed along by the narrowing of the other unit with images
    assert units_by_id["E1U02"]["duration_seconds"] == 2  # 未被另一个带图 unit 的收窄连带改动


@pytest.mark.asyncio
async def test_script_generator_takes_duration_tier_from_final_output_references(
    wide_tier_reference_project: Path, set_video_request_facts
):
    """The unit's current body has references (with-image options start at 3 seconds, 2 seconds not included), and the
    authoring output drops the references (falling back to the text-only options 1-16 seconds, where 2 seconds is
    valid): snapping must recompute from the final references state, not the pre-rewrite body state.

    单元现有正文带引用（带图档位最短 3 秒，2 秒不在其中），编写输出去掉了引用（回落到纯文本档位
    1–16 秒，2 秒合法）：取档须按最终落地的 references 状态重算，不能沿用改写前的正文状态。
    """
    set_video_request_facts(
        make_video_request_facts(
            route="reference_video",
            generation_type="i2v",
            supported_durations=tuple(range(1, 17)),
            allowed_durations=tuple(range(1, 17)),
        )
    )
    project = wide_tier_reference_project
    _write_formal_units(project, [{"unit_id": "E1U01", "text": "@[主角] 推门", "duration_seconds": 2}])

    fake_generator = _fake_prompt_authoring_generator("镜头1：空镜，门廊在风里轻响")
    gen = ScriptGenerator(project, generator=fake_generator)
    await gen.generate(episode=1)

    unit = _formal_units(project)["E1U01"]
    assert extract_mentions(unit["text"]) == []
    assert unit["duration_seconds"] == 2


@pytest.mark.asyncio
async def test_prompt_authoring_takes_the_i2v_tier_for_a_registered_reference_without_image(
    wide_tier_reference_project: Path, set_video_request_facts, db_factory
):
    """Prompt authoring snaps by the bucket of available reference images: a registered reference without an image puts
    the unit in i2v, the same as the content review panel and execution; 2 seconds is valid within the i2v options
    (1-16 seconds) and is not judged off-option by the r2v options (3-16 seconds) and stored as a draft just because
    the body has `@`.

    提示词编写的取档按可用参考图定桶：已登记但缺图的引用让单元与内容确认面板、执行一样落 i2v，
    2 秒在 i2v 档位（1–16 秒）内合法，不因正文带 `@` 就按 r2v 档位（3–16 秒）判越档落草稿。
    """
    set_video_request_facts(
        make_video_request_facts(
            route="reference_video",
            generation_type="i2v",
            supported_durations=tuple(range(1, 17)),
            allowed_durations=tuple(range(1, 17)),
        )
    )
    project = wide_tier_reference_project
    _write_formal_units(project, [{"unit_id": "E1U01", "text": "@[主角] 推门", "duration_seconds": 2}])
    resolver = ConfigResolver(db_factory)
    gen = ScriptGenerator(
        project, generator=_fake_prompt_authoring_generator("镜头1：中景。@[主角] 推门"), config_resolver=resolver
    )

    await gen.generate(episode=1)

    unit = _formal_units(project)["E1U01"]
    assert "@[主角]" in unit["text"]
    assert unit["duration_seconds"] == 2
    (capability,) = await evaluate_reference_unit_capabilities(
        gen.project_json, project, [unit], request_facts=configured_reference_request_facts(gen.project_json, resolver)
    )
    assert capability.generation_type == "i2v"


@pytest.mark.asyncio
async def test_conversion_requires_i2v_facts_for_a_registered_reference_without_image(
    plan_only_reference_project: Path, set_video_request_facts
):
    """Content review conversion buckets a unit with a registered reference without image as i2v: refuse when i2v facts
    cannot be resolved, instead of borrowing the r2v options to pass a plan that cannot be executed.

    内容确认转换对已登记但缺图的单元按 i2v 定桶：i2v 事实不可解析时拒绝，不借 r2v 档位放行一份执行不了的方案。"""
    set_video_request_facts(VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "i2v"),)))

    with pytest.raises(VideoRequestFactsError) as exc:
        await _materialize(ScriptGenerator(plan_only_reference_project))

    assert exc.value.code == "reference_capability_unavailable"
    assert not _script_path(plan_only_reference_project).exists()


@pytest.mark.asyncio
async def test_prompt_authoring_reads_the_r2v_tier_for_a_unit_with_a_usable_reference(
    reference_project: Path, set_video_request_facts
):
    """A unit with usable reference images snaps by the r2v bucket's facts: with r2v narrowed to [4] and i2v still
    [4, 8], 8 seconds is not blocked before the paid call (dropping the references would make it valid); once the
    authoring output keeps the references it is judged off-option by the r2v options, and the output lands in an
    invalid draft rather than passing by the i2v options.

    带可用参考图的单元按 r2v 桶的事实取档：r2v 收窄到 [4]、i2v 仍是 [4, 8] 时，8 秒在付费调用前不拦
    （去掉引用就能合法落地），编写输出保留引用后按 r2v 档位判越档，产出落待修复草稿而非按 i2v 档位放行。
    """
    set_video_request_facts(
        {
            "r2v": make_video_request_facts(
                route="reference_video", generation_type="r2v", supported_durations=(4, 8), allowed_durations=(4,)
            ),
            "i2v": make_video_request_facts(
                route="reference_video", generation_type="i2v", supported_durations=(4, 8), allowed_durations=(4, 8)
            ),
        }
    )
    _claim_character_sheet(reference_project, "主角")
    _write_formal_units(reference_project, [{**SCRIPT_PLAN_UNIT, "duration_seconds": 8}])
    generator = _fake_prompt_authoring_generator(PROMPT_AUTHORING_UNIT_TEXT)

    gen = ScriptGenerator(reference_project, generator=generator)
    with pytest.raises(DraftViolation, match="not among the currently available durations"):
        await gen.generate(episode=1)

    generator.generate.assert_awaited_once()
    assert _formal_units(reference_project)["E1U01"]["duration_seconds"] == 8
    envelope = _json.loads(_prompt_authoring_quarantine(reference_project).read_text(encoding="utf-8"))
    assert [v["code"] for v in envelope["violations"]] == ["duration_off_tier"]


@pytest.mark.asyncio
async def test_script_generator_rejects_prompt_authoring_unit_count_change(reference_project: Path):
    """prompt_authoring merging / splitting / adding / removing units: the unit count must match the pending units, and
    any change fails loudly.

    prompt_authoring 合并 / 拆分 / 增删 unit：unit 数须与待编写单元一致，改动即响亮失败。"""
    gen = ScriptGenerator(
        reference_project, generator=_fake_prompt_authoring_generator(PROMPT_AUTHORING_UNIT_TEXT, "镜头1：多出来的一段")
    )
    with pytest.raises(ValueError, match="units but"):
        await gen.generate(episode=1)


@pytest.mark.asyncio
async def test_script_generator_rejects_prompt_authoring_dialogue_rewrite(reference_project: Path):
    """Canonical dialogue lines stay verbatim: prompt_authoring changing words fails, rather than silently accepting
    dialogue rewritten to "suit the visuals".

    台词规范行逐字不变：prompt_authoring 改词即失败，不静默接受被改成「好配画面」的台词。"""
    _write_formal_units(reference_project, [{**SCRIPT_PLAN_UNIT, "text": "@[主角] 推门\n@[主角]：{我来了。}"}])
    gen = ScriptGenerator(
        reference_project,
        generator=_fake_prompt_authoring_generator("镜头1：中景。@[主角] 推门跨入\n@[主角]：{我到了。}"),
    )
    with pytest.raises(ValueError, match="dialogue line"):
        await gen.generate(episode=1)


@pytest.mark.asyncio
async def test_script_generator_accepts_prompt_authoring_expansion_keeping_dialogue(reference_project: Path):
    """Visual descriptions expanded freely with dialogue kept verbatim -> passes, and dialogue speakers are excluded from
    the reference images.

    画面描述自由展开、台词逐字保留 → 放行，并把台词说话人排除在参考图之外。"""
    _write_formal_units(reference_project, [{**SCRIPT_PLAN_UNIT, "text": "@[主角] 推门\n@[主角]：{我来了。}"}])
    gen = ScriptGenerator(
        reference_project,
        generator=_fake_prompt_authoring_generator(
            "镜头1：中景，平视。@[主角] 推开 @[酒馆] 的门，跨过门槛\n@[主角]：{我来了。}"
        ),
    )
    await gen.generate(episode=1)
    assert extract_mentions(_formal_units(reference_project)["E1U01"]["text"]) == ["主角", "酒馆"]


@pytest.mark.asyncio
async def test_script_generator_rejects_prompt_authoring_unregistered_mention(reference_project: Path):
    """Mentions added by prompt_authoring also go through registration checks: unregistered asset names must not slip
    into the body.

    prompt_authoring 新增的 mention 同样过登记校验：未登记资产名不得混进正文。"""
    gen = ScriptGenerator(
        reference_project, generator=_fake_prompt_authoring_generator("镜头1：@[主角] 与 @[路人乙] 对视")
    )
    with pytest.raises(ValueError, match="unregistered"):
        await gen.generate(episode=1)


@pytest.mark.asyncio
async def test_prompt_authoring_keeps_an_unregistered_speaker_from_the_formal_unit(reference_project: Path):
    """A speaker chosen as "do not register" at confirmation stays in the final body: prompt authoring keeps it verbatim
    and does not refuse it for being unregistered.

    确认时选为「不登记」的说话人留在正式正文里：提示词编写逐字保留它，不因未登记而拒绝。"""
    _write_formal_units(reference_project, [{**SCRIPT_PLAN_UNIT, "text": "路人 拦住 @[主角]\n@[路人]{借过。}"}])
    gen = ScriptGenerator(
        reference_project,
        generator=_fake_prompt_authoring_generator("镜头1：中景。路人 拦住 @[主角] 的去路\n@[路人]{借过。}"),
    )

    await gen.generate(episode=1)

    assert _formal_units(reference_project)["E1U01"]["text"].endswith("@[路人]{借过。}")


@pytest.mark.asyncio
async def test_prompt_authoring_refuses_a_speaker_the_formal_unit_did_not_have(reference_project: Path):
    _write_formal_units(reference_project, [{**SCRIPT_PLAN_UNIT, "text": "@[主角] 推门\n@[主角]{我来了。}"}])
    gen = ScriptGenerator(
        reference_project,
        generator=_fake_prompt_authoring_generator("镜头1：@[主角] 推门\n@[主角]{我来了。}\n@[路人乙]{谁？}"),
    )

    with pytest.raises(ValueError, match="speakers not registered"):
        await gen.generate(episode=1)


@pytest.mark.asyncio
async def test_script_plan_conversion_inherits_drama_content_mode(tmp_path: Path):
    """A reference-to-video script converted under a drama project must have content_mode drama.

    Pydantic's ReferenceVideoScript.content_mode defaults to "narration", and model_dump writes that default into the
    dict; _add_metadata must override explicitly instead of setdefault, or reference-to-video scripts of drama projects
    would be wrongly marked narration.

    drama 项目下转换出的参考生视频剧本 content_mode 必须为 drama。

    Pydantic 的 ReferenceVideoScript.content_mode 默认 "narration"，model_dump 会
    把该默认值写入 dict；_add_metadata 必须显式覆盖而非 setdefault，否则 drama 项目
    的参考生视频剧本会被错误标记成 narration。
    """
    project_dir = _write_reference_project(tmp_path, video_backend="vidu/vidu2.0", content_mode="drama")

    await _materialize(ScriptGenerator(project_dir))

    data = _json.loads(_script_path(project_dir).read_text(encoding="utf-8"))
    assert data["content_mode"] == "drama"
    assert "generation_mode" not in data


@pytest.mark.parametrize(
    ("with_references", "expected"),
    [
        (make_video_request_facts(route="reference_video", generation_type="r2v", max_reference_images=3), 3),
        (make_video_request_facts(route="reference_video", generation_type="r2v", max_reference_images=1), 1),
        # 0 is an explicit limit (an endpoint that accepts no reference images), passed down as is to trim to 0 images
        # 0 是显式上限（不接受参考图的 endpoint），原样下传触发裁剪为 0 张
        (make_video_request_facts(route="reference_video", generation_type="r2v", max_reference_images=0), 0),
        # Facts declare no limit -> None
        # 事实未声明上限 → None
        (make_video_request_facts(route="reference_video", generation_type="r2v", max_reference_images=None), None),
        # r2v bucket cannot be resolved -> treated as undeclared; units with images are blocked separately at each
        # checkpoint for the missing facts
        # r2v 桶解析不出 → 按未声明处理，带图单元另在各检查点因事实缺失被拦
        (VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "r2v"),)), None),
    ],
)
def test_resolve_max_refs_reads_the_r2v_request_facts(with_references, expected):
    """The reference image count limit comes only from the r2v bucket's video request facts, no longer falling back to
    looking up the registry by project.json's self-declared identity.

    参考图数量上限只来自 r2v 桶的视频请求事实，不再回退到 project.json 自报身份查 registry。"""
    facts = PlanningVideoFacts(
        by_bucket={
            "r2v": with_references,
            "i2v": make_video_request_facts(route="reference_video", generation_type="i2v", max_reference_images=9),
        },
    )

    assert ScriptGenerator._resolve_max_refs(facts) == expected


def _write_minimal_reference_project(tmp_path: Path, **overrides: Any) -> Path:
    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    project = {
        "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
        "title": "t",
        "content_mode": "narration",
        "generation_mode": "reference_video",
        "overview": {"synopsis": "s", "genre": "g", "theme": "t", "world_setting": "w"},
        "style": "s",
        "style_description": "d",
        "characters": {},
        "scenes": {},
        "props": {},
        "episodes": [{"episode": 1, "script_file": "scripts/episode_1.json"}],
        **overrides,
    }
    (project_dir / "project.json").write_text(_json.dumps(project, ensure_ascii=False), encoding="utf-8")
    _write_script_plan(project_dir, SCRIPT_PLAN_UNITS_JSON)
    _write_formal_units(project_dir, [SCRIPT_PLAN_UNIT])
    return project_dir


@pytest.mark.asyncio
async def test_generate_without_r2v_facts_fails_with_the_facts_code_before_the_paid_call(
    tmp_path: Path, set_video_request_facts
):
    """A unit with usable reference images lands in r2v: when that bucket's facts cannot be resolved, authoring fails
    with a same-family problem code before calling the model.

    Duration options come only from video request facts, not falling back to the registry via project.json's
    self-declared identity; a resolvable i2v bucket is not borrowed either.

    带可用参考图的单元落 r2v：该桶事实解析不出时编写在调用模型前以同族问题码失败。

    档位只来自视频请求事实，不回退到 project.json 自报身份查 registry；i2v 桶可解析也不借用。
    """
    set_video_request_facts(
        {
            "r2v": VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "r2v"),)),
            "i2v": make_video_request_facts(route="reference_video", generation_type="i2v"),
        }
    )
    project_dir = _write_minimal_reference_project(tmp_path)
    _claim_character_sheet(project_dir, "主角")
    generator = _idle_generator()

    gen = ScriptGenerator(project_dir, generator=generator)
    with pytest.raises(VideoRequestFactsError) as exc:
        await gen.generate(episode=1)

    assert exc.value.code == "reference_capability_unavailable"
    generator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_prompt_follows_project_reference_route(tmp_path: Path):
    """When the project generation mode is reference_video, build_prompt must take the reference branch.

    The generation mode comes from the top-level ``generation_mode`` of ``project.json``, the same for the whole
    project and not varying by episode.

    项目生成模式为 reference_video 时 build_prompt 必须走 reference 分支。

    生成模式取自 ``project.json`` 顶层 ``generation_mode``，全项目相同、不随集号变化。
    """
    project_dir = _write_minimal_reference_project(
        tmp_path, video_backend="vidu/vidu2.0", characters={"主角": {"description": "d"}}
    )

    prompt = await ScriptGenerator(project_dir).build_prompt(episode=1)
    assert "@[主角] 推开 @[酒馆] 的门" in prompt
    assert "（时长 4s）" in prompt


# Content review conversion reads the reference_video script plan
# ---------------------------------------------------------------------------
# 内容确认转换读取 reference_video 脚本规划
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversion_reads_legacy_script_plan_draft_without_source_text(plan_only_reference_project: Path):
    """An existing script_plan draft (no source_text, per-shot durations already consolidated by migration) can still be
    read by the validator and converted.

    ``source_text`` is the source anchor validated and written when the split tool produces output; drafts without
    that field are all treated as existing: the empty-string default lets reading pass as usual, without asking the
    user to rerun the split.

    存量 script_plan 草稿（无 source_text，per-shot 时长已由迁移收编）仍能被校验器读取并转换。

    ``source_text`` 是拆分工具产出时校验后落盘的原文锚，不带该字段的草稿一律视为存量：
    默认空串使读取照常通过，不要求用户重跑拆分。
    """
    project = plan_only_reference_project
    saved = _json.loads((project / "drafts" / "episode_1" / "script_plan_reference_units.json").read_text("utf-8"))
    assert "source_text" not in saved["units"][0]

    receipt = await _materialize(ScriptGenerator(project))

    assert receipt.entry_ids == ("E1U1",)
    assert list(_formal_units(project)) == ["E1U1"]


@pytest.mark.asyncio
async def test_reference_script_plan_legacy_md_prompts_resplit(plan_only_reference_project: Path):
    """When only the pre-structured old .md split table exists, give an explicit "rerun the split" hint rather than a
    generic missing-file error.

    仅存在结构化前的旧 .md 拆分表时，给出明确的「重跑拆分」提示而非笼统缺文件错误。"""
    drafts = plan_only_reference_project / "drafts" / "episode_1"
    (drafts / "script_plan_reference_units.json").unlink()
    (drafts / "script_plan_reference_units.md").write_text("| E1U1 | Shot1(4s) |", encoding="utf-8")

    gen = ScriptGenerator(plan_only_reference_project)
    with pytest.raises(FileNotFoundError, match="generate_script_plan"):
        await _materialize(gen)


@pytest.mark.asyncio
async def test_reference_script_plan_missing_raises(plan_only_reference_project: Path):
    drafts = plan_only_reference_project / "drafts" / "episode_1"
    (drafts / "script_plan_reference_units.json").unlink()

    gen = ScriptGenerator(plan_only_reference_project)
    with pytest.raises(FileNotFoundError, match="video_unit split"):
        await _materialize(gen)


@pytest.mark.asyncio
async def test_reference_script_plan_rejects_out_of_enum_duration(
    plan_only_reference_project: Path, set_video_request_facts
):
    """On conversion, re-verify that unit durations fall within both buckets' effective options: durations that are full
    set members but narrowed out are also refused, guarding against hand-edit drift.

    转换时复验 unit 时长落在两桶生效档位内：仍是全集成员、但已被收窄出局的时长也拒绝，防手工编辑漂移。"""
    set_video_request_facts(
        make_video_request_facts(
            route="reference_video", generation_type="r2v", supported_durations=(4, 6, 8), allowed_durations=(4, 8)
        )
    )
    drafts = plan_only_reference_project / "drafts" / "episode_1"
    (drafts / "script_plan_reference_units.json").write_text(
        _json.dumps({"units": [{"unit_id": "E1U01", "text": "@[主角] 转身", "duration_seconds": 6}]}),
        encoding="utf-8",
    )
    _claim_character_sheet(plan_only_reference_project, "主角")

    gen = ScriptGenerator(plan_only_reference_project)
    with pytest.raises(UnitDurationOffTier, match="video unit 1: confirmed duration 6s is not in the active duration tiers") as exc_info:
        await _materialize(gen)
    assert (exc_info.value.position, exc_info.value.duration_seconds, exc_info.value.tiers) == (1, 6, [4, 8])
    assert "E1U01" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_reference_script_plan_rejects_duplicate_unit_ids(plan_only_reference_project: Path):
    drafts = plan_only_reference_project / "drafts" / "episode_1"
    unit = {"unit_id": "E1U01", "text": "@[主角] 转身", "duration_seconds": 4}
    (drafts / "script_plan_reference_units.json").write_text(
        _json.dumps({"units": [unit, dict(unit)]}), encoding="utf-8"
    )

    gen = ScriptGenerator(plan_only_reference_project)
    with pytest.raises(ValueError, match="duplicate unit_id"):
        await _materialize(gen)


def test_reference_script_plan_migration_carries_confirmation_forward(plan_only_reference_project: Path):
    """Migration write-back makes the script_plan content fingerprint drift; if the episode is confirmed (the fingerprint
    is exactly the pre-migration content), the confirmation fingerprint must be moved to the post-migration value, or
    merely loading the plan once would make a confirmed episode wait for confirmation again.

    迁移回写让 script_plan 内容指纹漂移；若该集已确认（指纹恰是迁移前内容），须把确认指纹
    平移到迁移后的值，否则仅加载一次规划就会让已确认分集重新等待确认。
    """
    drafts = plan_only_reference_project / "drafts" / "episode_1"
    # duration_override is a marker retired together with per-shot durations, stripped by the consolidating migration on
    # load.
    # duration_override 是随 per-shot 时长一同退役的标记，加载时被收编迁移剥掉。
    legacy = {"units": [{"unit_id": "E1U01", "text": "@[主角] 转身", "duration_seconds": 4, "duration_override": True}]}
    script_plan_path = drafts / "script_plan_reference_units.json"
    script_plan_path.write_text(_json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    before = script_review.content_fingerprint(script_plan_path)

    project_path = plan_only_reference_project / "project.json"
    project = _json.loads(project_path.read_text(encoding="utf-8"))
    project["episodes"][0]["script_plan_review"] = {"fingerprint": before, "confirmed_at": "2026-01-01T00:00:00Z"}
    project_path.write_text(_json.dumps(project, ensure_ascii=False), encoding="utf-8")

    gen = ScriptGenerator(plan_only_reference_project)
    gen._load_reference_script_plan(episode=1, supported_durations=[4, 8])

    after_project = _json.loads(project_path.read_text(encoding="utf-8"))
    review = after_project["episodes"][0]["script_plan_review"]
    after = script_review.content_fingerprint(script_plan_path)
    assert review["fingerprint"] == after
    assert review["fingerprint"] != before


@pytest.mark.asyncio
async def test_reference_script_plan_migration_waits_for_prompt_authoring_draft_lock(
    plan_only_reference_project: Path, monkeypatch
) -> None:
    script_plan_path = plan_only_reference_project / "drafts" / "episode_1" / "script_plan_reference_units.json"
    script_plan_path.write_text(
        _json.dumps(
            {
                "units": [
                    {
                        "unit_id": "E1U01",
                        "text": "@[主角] 转身",
                        "duration_seconds": 4,
                        "duration_override": True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    prompt_authoring_path = quarantine_path(plan_only_reference_project, 1, QUARANTINE_KIND_PROMPT_AUTHORING)
    pm = ProjectManager.for_project_dir(plan_only_reference_project)
    held = threading.Event()
    release = threading.Event()

    def hold_prompt_authoring_lock() -> None:
        with pm.file_lock(prompt_authoring_path):
            held.set()
            _ = release.wait()

    holder = asyncio.create_task(asyncio.to_thread(hold_prompt_authoring_lock))
    try:
        assert await asyncio.to_thread(held.wait, 1)
        attempted = threading.Event()
        target_lock = prompt_authoring_path.parent / f".{prompt_authoring_path.name}.lock"
        original_acquire = project_manager_module.portalocker.Lock.acquire

        def tracked_acquire(lock, *args, **kwargs):
            if Path(lock.filename) == target_lock:
                attempted.set()
            return original_acquire(lock, *args, **kwargs)

        monkeypatch.setattr(project_manager_module.portalocker.Lock, "acquire", tracked_acquire)
        loading = asyncio.create_task(
            ScriptGenerator(plan_only_reference_project)._load_plan_entries_for_materialization(1)
        )
        attempted_before_release = await asyncio.to_thread(attempted.wait, 1)
        if attempted_before_release:
            assert not loading.done()
            assert "duration_override" in script_plan_path.read_text(encoding="utf-8")
            ticked = asyncio.Event()
            asyncio.get_running_loop().call_soon(ticked.set)
            await asyncio.wait_for(ticked.wait(), timeout=1)
    finally:
        release.set()
        await asyncio.wait_for(holder, timeout=1)

    await asyncio.wait_for(loading, timeout=1)
    assert attempted_before_release
    assert "duration_override" not in script_plan_path.read_text(encoding="utf-8")


def test_reference_script_plan_migration_carries_confirmation_confirmed_after_construction(
    plan_only_reference_project: Path,
):
    """Confirmation happens after ScriptGenerator construction (e.g. the user confirms concurrently via
    ScriptReviewService.confirm() during the await _fetch_video_request_facts() in generate()): self.project_json is the
    old snapshot from construction and does not see this confirmation, but the migration write-back must still move it
    correctly -- this old snapshot must not be used for an early short-circuit.

    确认发生在 ScriptGenerator 构造之后（如 generate() 内 await _fetch_video_request_facts()
    期间用户经 ScriptReviewService.confirm() 并发确认）：self.project_json 是构造时的旧快照，
    看不到这次确认，但迁移写回仍须正确搬移它——不能用这份旧快照做前置短路。
    """
    drafts = plan_only_reference_project / "drafts" / "episode_1"
    # duration_override is a marker retired together with per-shot durations, stripped by the consolidating migration on
    # load.
    # duration_override 是随 per-shot 时长一同退役的标记，加载时被收编迁移剥掉。
    legacy = {"units": [{"unit_id": "E1U01", "text": "@[主角] 转身", "duration_seconds": 4, "duration_override": True}]}
    script_plan_path = drafts / "script_plan_reference_units.json"
    script_plan_path.write_text(_json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    before = script_review.content_fingerprint(script_plan_path)

    # At construction project.json has no confirmation record yet.
    # 构造时 project.json 尚无确认记录。
    gen = ScriptGenerator(plan_only_reference_project)

    # Confirmation happens only after construction (simulating a concurrent ScriptReviewService.confirm()), and
    # self.project_json is not refreshed.
    # 构造之后才发生确认（模拟并发的 ScriptReviewService.confirm()），self.project_json 不刷新。
    project_path = plan_only_reference_project / "project.json"
    project = _json.loads(project_path.read_text(encoding="utf-8"))
    project["episodes"][0]["script_plan_review"] = {"fingerprint": before, "confirmed_at": "2026-01-01T00:00:00Z"}
    project_path.write_text(_json.dumps(project, ensure_ascii=False), encoding="utf-8")
    # The snapshot at construction really does not have it yet
    assert "script_plan_review" not in gen.project_json["episodes"][0]  # 构造时的快照确实还没有它

    gen._load_reference_script_plan(episode=1, supported_durations=[4, 8])

    after_project = _json.loads(project_path.read_text(encoding="utf-8"))
    review = after_project["episodes"][0]["script_plan_review"]
    after = script_review.content_fingerprint(script_plan_path)
    assert review["fingerprint"] == after
    assert review["confirmed_at"] == "2026-01-01T00:00:00Z"
    assert review["confirmed_at"] == "2026-01-01T00:00:00Z"


def test_reference_script_plan_migration_does_not_carry_confirmation_when_duration_is_clamped(
    plan_only_reference_project: Path,
):
    """A migration with warnings (the summed duration is not among the model's options and was snapped) is not pure
    format consolidation: a confirmed episode must wait for confirmation again, and the confirmation must not be moved
    -- the snapped seconds are not what the user saw when confirming.

    迁移带 warnings（求和时长不在模型档位内，被取档改写）不是纯格式收编：已确认分集
    须重新等待确认，不能平移确认——取档后的秒数不是用户确认时看到的值。
    """
    drafts = plan_only_reference_project / "drafts" / "episode_1"
    # duration_override is a marker retired together with per-shot durations, stripped by the consolidating migration on
    # load.
    # duration_override 是随 per-shot 时长一同退役的标记，加载时被收编迁移剥掉。
    legacy = {"units": [{"unit_id": "E1U01", "text": "@[主角] 转身", "duration_seconds": 4, "duration_override": True}]}
    script_plan_path = drafts / "script_plan_reference_units.json"
    script_plan_path.write_text(_json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    before = script_review.content_fingerprint(script_plan_path)

    project_path = plan_only_reference_project / "project.json"
    project = _json.loads(project_path.read_text(encoding="utf-8"))
    project["episodes"][0]["script_plan_review"] = {"fingerprint": before, "confirmed_at": "2026-01-01T00:00:00Z"}
    project_path.write_text(_json.dumps(project, ensure_ascii=False), encoding="utf-8")

    gen = ScriptGenerator(plan_only_reference_project)
    # The 4s sum is not a model option member and is snapped to 8s -- this step produces a warning.
    # 求和 4s 不是模型档位成员，取档改写为 8s——这一步产生 warning。
    gen._load_reference_script_plan(episode=1, supported_durations=[8])

    assert _json.loads(script_plan_path.read_text(encoding="utf-8"))["units"][0]["duration_seconds"] == 8

    after_project = _json.loads(project_path.read_text(encoding="utf-8"))
    review = after_project["episodes"][0]["script_plan_review"]
    # Not moved; still the pre-migration fingerprint -- judged as awaiting confirmation as usual
    assert review["fingerprint"] == before  # 未被平移，仍是迁移前的旧指纹——照常判定为待确认


@pytest.mark.asyncio
async def test_formal_text_violation_is_caught_before_the_paid_prompt_authoring_call(reference_project: Path):
    """Syntax violations in a pending unit's body are caught before calling the text model, and the error points back to
    the final script and timeline.

    prompt_authoring keeps this body verbatim, so the violation is bound to recur unchanged -- without checking before
    the call, it would fail only after paying for prompt_authoring, with an error blaming prompt_authoring for
    "breaking it".

    待编写单元正文的语法违约在调用文本模型之前就被拦下，且错误指回正式剧本与时间线。

    prompt_authoring 会逐字保留这段正文，违约必然原样复现——不在调用前判，就要付完 prompt_authoring
    的钱才失败，且错误指向 prompt_authoring「改坏了」。
    """
    _write_formal_units(reference_project, [{**SCRIPT_PLAN_UNIT, "text": "@[查无此人} 推门"}])
    generator = _idle_generator()

    with pytest.raises(PromptAuthoringUnitRejected, match=r"comes from the final script.*timeline") as exc_info:
        await ScriptGenerator(reference_project, generator=generator).generate(episode=1)
    generator.generate.assert_not_awaited()
    assert (exc_info.value.code, exc_info.value.params) == (
        "prompt_authoring_unit_invalid",
        {"unit_id": "E1U01", "position": 1},
    )


def test_speech_violation_preserves_canonical_unit_and_locations(reference_project: Path):
    gen = ScriptGenerator(reference_project)
    units = [
        {
            "unit_id": "E1U01",
            "duration_seconds": 4,
            "text": "门被推开\n@[主角]：{快走。}\n{风吹过旷野。}",
        }
    ]

    with pytest.raises(DraftViolation) as exc_info:
        gen._assert_reference_unit_text_valid(units, max_refs=None)

    problem = exc_info.value
    assert problem.code == "mixed_speech"
    assert "unit E1U01 failed the speech admission check" in str(problem)
    assert "unit final script unit" not in str(problem)
    assert problem.locations == (
        {"path": ["text"], "line": 1},
        {"path": ["text"], "line": 2},
    )
    assert problem.reason == "character_and_narrator_mixed"
    assert problem.action == "replan_unit"


async def test_dialogue_overload_is_caught_before_the_paid_prompt_authoring_call(reference_project: Path):
    """Shortening durations / adding dialogue on the timeline bypasses the speech-amount check of the split; the re-check
    before authoring catches it.

    prompt_authoring keeps dialogue verbatim and there is no later speech-amount check: without re-checking here, a
    unit that cannot be spoken in time would go all the way to the final cut.

    在时间线上改短时长 / 补写台词会绕开拆分时的口播量校验，编写前复判把它拦下。

    prompt_authoring 逐字保留台词、之后再无口播量校验：不在这里复判，念不完的 unit 会一路落盘成片。
    """
    long_line = "他站在门口足足看了半晌才缓缓开口说出这句迟到了整整十年的道歉与告别" * 2
    _write_formal_units(
        reference_project,
        [
            {**SCRIPT_PLAN_UNIT, "pending_authoring": False},
            {**SCRIPT_PLAN_UNIT, "unit_id": "E1U04", "text": f"@[主角] 推开 @[酒馆] 的门\n@[主角]：{{{long_line}}}"},
        ],
    )
    generator = _idle_generator()

    with pytest.raises(PromptAuthoringUnitRejected, match="dialogue takes about") as exc_info:
        await ScriptGenerator(reference_project, generator=generator).generate(episode=1)
    generator.generate.assert_not_awaited()
    # Problem codes are classified by input problem, and params name the unit by UI position (the 2nd) rather than by
    # the allocate-only unit ID.
    # 问题码按输入问题归类，参数按界面上的位置称呼单元（第 2 个），而不是只分配不复用的单元 ID。
    rejection = exc_info.value
    assert rejection.code == "prompt_authoring_dialogue_overload"
    assert rejection.params["position"] == 2
    assert rejection.params["unit_id"] == "E1U04"
    assert rejection.params["duration_seconds"] == 4
    assert float(rejection.params["spoken_seconds"]) > 4


async def test_prompt_authoring_missing_title_does_not_fail_the_paid_call(reference_project: Path):
    """A title omitted by an unconstrained decoding channel does not fail the already paid expansion: title is not
    written back, and the final script's title is kept as is.

    非约束解码通道漏写 title 不让已付费的展开失败：title 不参与写回，正式剧本标题原样保留。"""
    generator = MagicMock()
    generator.model = "mock"
    generator.generate = AsyncMock(
        return_value=MagicMock(text=_json.dumps({"units": [{"text": PROMPT_AUTHORING_UNIT_TEXT}]}, ensure_ascii=False))
    )

    out = await ScriptGenerator(reference_project, generator=generator).generate(episode=1)

    data = _json.loads(out.read_text(encoding="utf-8"))
    assert data["title"] == "第1集"
    assert data["video_units"][0]["text"] == PROMPT_AUTHORING_UNIT_TEXT


@pytest.mark.asyncio
async def test_prompt_authoring_runs_while_script_plan_quarantined(reference_project: Path):
    """Authoring reads only the final script: the presence of a script plan invalid draft does not affect authoring.

    编写只读正式剧本：脚本规划的待修复草稿在场不影响编写。"""
    write_quarantine(
        reference_project,
        1,
        QUARANTINE_KIND_SCRIPT_PLAN,
        content={"units": []},
        violations=[DraftViolation("坏", code="empty_text", label="unit E1U01")],
    )
    gen = ScriptGenerator(reference_project, generator=_fake_prompt_authoring_generator(PROMPT_AUTHORING_UNIT_TEXT))

    await gen.generate(episode=1)

    assert _formal_units(reference_project)["E1U01"]["text"] == PROMPT_AUTHORING_UNIT_TEXT


# Invalid drafts from prompt_authoring violations and the repair/promotion loop
# ---------------------------------------------------------------------------
# prompt_authoring 违约的待修复草稿与修复晋升闭环
# ---------------------------------------------------------------------------

#: A violating prompt_authoring expansion: references an unregistered asset name (@[Passerby A], not in the unit
#: body).
#: 违约的 prompt_authoring 展开：引用了未登记的资产名（单元正文里没有的 @[路人甲]）。
BAD_PROMPT_AUTHORING_UNIT_TEXT = "镜头1：中景。@[路人甲] 推开 @[酒馆] 的门。"


def _prompt_authoring_quarantine(project: Path):
    return quarantine_path(project, 1, QUARANTINE_KIND_PROMPT_AUTHORING)


@pytest.mark.asyncio
async def test_prompt_authoring_violation_quarantines_instead_of_discarding(reference_project: Path):
    """A prompt_authoring violation does not discard the already paid expansion: the output lands in an invalid draft, the
    final script is not rewritten, and the report carries handling guidance.

    prompt_authoring 违约不丢弃这次已付费的展开：产物落待修复草稿、正式剧本不被改写、报告带处置指引。"""
    formal_before = _script_path(reference_project).read_bytes()
    baseline = script_review.content_fingerprint(_script_path(reference_project))
    gen = ScriptGenerator(reference_project, generator=_fake_prompt_authoring_generator(BAD_PROMPT_AUTHORING_UNIT_TEXT))

    with pytest.raises(DraftViolation) as excinfo:
        await gen.generate(episode=1)

    report = str(excinfo.value)
    assert "unregistered_asset" in report
    assert "promote_draft" in report
    assert _script_path(reference_project).read_bytes() == formal_before

    envelope = _json.loads(_prompt_authoring_quarantine(reference_project).read_text(encoding="utf-8"))
    assert envelope["kind"] == QUARANTINE_KIND_PROMPT_AUTHORING
    assert envelope["meta"]["base_fingerprint"] == baseline
    assert envelope["meta"]["unit_ids"] == ["E1U01"]
    assert [v["code"] for v in envelope["violations"]] == ["unregistered_asset"]
    # The draft holds the flat draft structure (what the Agent needs to change is the body in it)
    # 草稿装的是扁平草稿结构（Agent 要改的是其中的正文）
    assert envelope["content"]["units"][0]["text"] == BAD_PROMPT_AUTHORING_UNIT_TEXT


@pytest.mark.asyncio
async def test_cancelled_prompt_authoring_quarantine_finishes_without_blocking_event_loop(reference_project: Path):
    generator = ScriptGenerator(
        reference_project, generator=_fake_prompt_authoring_generator(BAD_PROMPT_AUTHORING_UNIT_TEXT)
    )
    started = threading.Event()
    release = threading.Event()
    caller_thread = threading.get_ident()
    worker_threads: list[int] = []

    def before_quarantine_commit() -> None:
        worker_threads.append(threading.get_ident())
        started.set()
        release.wait()

    generation = asyncio.create_task(generator.generate(episode=1, before_quarantine_commit=before_quarantine_commit))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        ticked = asyncio.Event()
        asyncio.get_running_loop().call_soon(ticked.set)
        await asyncio.wait_for(ticked.wait(), timeout=1)
        generation.cancel()
        await asyncio.sleep(0)
        assert not generation.done()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(generation, timeout=1)
    assert _prompt_authoring_quarantine(reference_project).exists()
    assert worker_threads
    assert all(thread != caller_thread for thread in worker_threads)


@pytest.mark.asyncio
async def test_prompt_authoring_generation_preserves_draft_edited_during_model_call(reference_project: Path):
    started = asyncio.Event()
    release = asyncio.Event()
    generator = MagicMock()
    generator.model = "mock"

    async def generate(_request, **_kwargs):
        started.set()
        await release.wait()
        return MagicMock(text=_prompt_authoring_response(BAD_PROMPT_AUTHORING_UNIT_TEXT))

    generator.generate = AsyncMock(side_effect=generate)
    generation = asyncio.create_task(ScriptGenerator(reference_project, generator=generator).generate(episode=1))
    await started.wait()
    write_quarantine(
        reference_project,
        1,
        QUARANTINE_KIND_PROMPT_AUTHORING,
        content={"title": "并发草稿", "units": [{"text": "并发编辑内容"}]},
        violations=[],
    )
    release.set()

    with pytest.raises(DraftViolation) as excinfo:
        await asyncio.wait_for(generation, timeout=1)
    assert excinfo.value.code == "draft_revision_conflict"
    envelope = _json.loads(_prompt_authoring_quarantine(reference_project).read_text(encoding="utf-8"))
    assert envelope["content"]["title"] == "并发草稿"
    assert envelope["content"]["units"][0]["text"] == "并发编辑内容"


@pytest.mark.asyncio
async def test_successful_prompt_authoring_generation_rejects_draft_created_during_model_call(reference_project: Path):
    formal_before = _script_path(reference_project).read_bytes()
    started = asyncio.Event()
    release = asyncio.Event()
    generator = MagicMock()
    generator.model = "mock"

    async def generate(_request, **_kwargs):
        started.set()
        await release.wait()
        return MagicMock(text=_prompt_authoring_response(PROMPT_AUTHORING_UNIT_TEXT))

    generator.generate = AsyncMock(side_effect=generate)
    generation = asyncio.create_task(ScriptGenerator(reference_project, generator=generator).generate(episode=1))
    await asyncio.wait_for(started.wait(), timeout=1)
    write_quarantine(
        reference_project,
        1,
        QUARANTINE_KIND_PROMPT_AUTHORING,
        content={"title": "并发草稿", "units": [{"text": "并发编辑内容"}]},
        violations=[],
        meta={"base_fingerprint": None},
    )
    release.set()

    with pytest.raises(DraftViolation) as excinfo:
        await asyncio.wait_for(generation, timeout=1)
    assert excinfo.value.code == "draft_revision_conflict"
    assert _script_path(reference_project).read_bytes() == formal_before
    envelope = _json.loads(_prompt_authoring_quarantine(reference_project).read_text(encoding="utf-8"))
    assert envelope["content"]["title"] == "并发草稿"


@pytest.mark.asyncio
async def test_prompt_authoring_generation_rejects_existing_draft_before_model_call(reference_project: Path):
    write_quarantine(
        reference_project,
        1,
        QUARANTINE_KIND_PROMPT_AUTHORING,
        content={"title": "现有草稿", "units": [{"text": PROMPT_AUTHORING_UNIT_TEXT}]},
        violations=[],
        meta={"base_fingerprint": None},
    )
    generator = _fake_prompt_authoring_generator(PROMPT_AUTHORING_UNIT_TEXT)

    with pytest.raises(DraftViolation) as excinfo:
        await ScriptGenerator(reference_project, generator=generator).generate(episode=1)

    assert excinfo.value.code == "draft_revision_conflict"
    generator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_prompt_authoring_preserves_output_when_formal_script_changes_during_generation(reference_project: Path):
    formal = _script_path(reference_project)
    concurrent = _json.loads(formal.read_text(encoding="utf-8"))
    concurrent["title"] = "并发正式标题"
    generator = MagicMock()
    generator.model = "mock"

    async def generate(_request, **_kwargs):
        # The concurrent writer is not on the generation's event loop, so the rewrite goes into a worker thread.
        # 并发写方不在生成所在的事件循环上，改写放进工作线程。
        await asyncio.to_thread(formal.write_text, _json.dumps(concurrent, ensure_ascii=False), encoding="utf-8")
        return MagicMock(text=_prompt_authoring_response(PROMPT_AUTHORING_UNIT_TEXT, title="本次生成标题"))

    generator.generate = AsyncMock(side_effect=generate)

    with pytest.raises(DraftViolation) as excinfo:
        await ScriptGenerator(reference_project, generator=generator).generate(episode=1)

    assert "formal_revision_conflict" in str(excinfo.value)
    assert _json.loads(formal.read_text(encoding="utf-8"))["title"] == "并发正式标题"
    envelope = _json.loads(_prompt_authoring_quarantine(reference_project).read_text(encoding="utf-8"))
    assert envelope["content"]["title"] == "本次生成标题"
    assert envelope["meta"]["base_fingerprint"] is not None


@pytest.mark.asyncio
async def test_prompt_authoring_violation_keeps_generation_start_formal_baseline(reference_project: Path):
    formal = _script_path(reference_project)
    baseline = script_review.content_fingerprint(formal)
    assert baseline is not None
    concurrent = _json.loads(formal.read_text(encoding="utf-8"))
    concurrent["title"] = "并发正式标题"
    generator = MagicMock()
    generator.model = "mock"

    async def generate(_request, **_kwargs):
        # The concurrent writer is not on the generation's event loop, so the rewrite goes into a worker thread.
        # 并发写方不在生成所在的事件循环上，改写放进工作线程。
        await asyncio.to_thread(formal.write_text, _json.dumps(concurrent, ensure_ascii=False), encoding="utf-8")
        return MagicMock(text=_prompt_authoring_response(BAD_PROMPT_AUTHORING_UNIT_TEXT, title="本次违约输出"))

    generator.generate = AsyncMock(side_effect=generate)

    with pytest.raises(DraftViolation):
        await ScriptGenerator(reference_project, generator=generator).generate(episode=1)

    assert _json.loads(formal.read_text(encoding="utf-8"))["title"] == "并发正式标题"
    envelope = _json.loads(_prompt_authoring_quarantine(reference_project).read_text(encoding="utf-8"))
    assert envelope["content"]["title"] == "本次违约输出"
    assert envelope["meta"]["base_fingerprint"] == baseline
    assert envelope["meta"]["base_fingerprint"] != script_review.content_fingerprint(formal)


@pytest.mark.asyncio
async def test_promote_prompt_authoring_draft_after_repair(reference_project: Path, monkeypatch):
    """Promote after repairing the invalid draft: bodies are written back to the final script, pending marks cleared, and
    the draft cleared.

    修好待修复草稿后晋升：正文写回正式剧本、待编写标记清除、草稿清除。"""
    gen = ScriptGenerator(reference_project, generator=_fake_prompt_authoring_generator(BAD_PROMPT_AUTHORING_UNIT_TEXT))
    with pytest.raises(DraftViolation):
        await gen.generate(episode=1)

    path = _prompt_authoring_quarantine(reference_project)
    envelope = _json.loads(path.read_text(encoding="utf-8"))
    envelope["content"]["units"][0]["text"] = PROMPT_AUTHORING_UNIT_TEXT
    path.write_text(_json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    caller_thread = threading.get_ident()
    worker_threads: list[int] = []
    original_save = ProjectManager.save_script

    def tracked_save(self, *args, **kwargs):
        worker_threads.append(threading.get_ident())
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(ProjectManager, "save_script", tracked_save)

    out = await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(episode=1)

    assert out.exists()
    assert worker_threads
    assert all(thread != caller_thread for thread in worker_threads)
    assert not path.exists()
    data = _json.loads(out.read_text(encoding="utf-8"))
    unit = data["video_units"][0]
    assert unit["unit_id"] == "E1U01"
    assert unit["duration_seconds"] == 4
    assert "pending_authoring" not in unit
    assert extract_mentions(unit["text"]) == ["主角", "酒馆"]


@pytest.mark.asyncio
async def test_promote_prompt_authoring_draft_merges_only_the_recorded_units(reference_project: Path):
    """The draft corresponds via meta.unit_ids to the pending units at output time: promotion writes back only those
    units, other units stay byte-identical.

    草稿按 meta.unit_ids 对应产出时的待编写单元：晋升只写回这些单元，其余单元逐字节不变。"""
    authored = {
        "unit_id": "E1U01",
        "text": "镜头1：远景。@[主角] 站在 @[酒馆] 门口。",
        "duration_seconds": 4,
        "pending_authoring": False,
    }
    pending = {"unit_id": "E1U02", "text": "@[主角] 走进 @[酒馆]", "duration_seconds": 4}
    _write_formal_units(reference_project, [authored, pending])
    before_first = _json.dumps(_formal_units(reference_project)["E1U01"], sort_keys=True)
    gen = ScriptGenerator(
        reference_project, generator=_fake_prompt_authoring_generator("镜头1：@[路人甲] 走进 @[酒馆]")
    )
    with pytest.raises(DraftViolation):
        await gen.generate(episode=1)

    path = _prompt_authoring_quarantine(reference_project)
    envelope = _json.loads(path.read_text(encoding="utf-8"))
    assert envelope["meta"]["unit_ids"] == ["E1U02"]
    envelope["content"]["units"][0]["text"] = "镜头1：中景。@[主角] 走进 @[酒馆]。"
    path.write_text(_json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

    await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(episode=1)

    after = _formal_units(reference_project)
    assert _json.dumps(after["E1U01"], sort_keys=True) == before_first
    assert after["E1U02"] == {"unit_id": "E1U02", "text": "镜头1：中景。@[主角] 走进 @[酒馆]。", "duration_seconds": 4}


@pytest.mark.asyncio
async def test_promote_whole_script_draft_clears_pending_only_for_units_whose_text_changed(reference_project: Path):
    """A whole-script draft without meta.unit_ids: units whose body changed get their pending mark cleared, units whose
    body did not change are kept byte-identical (mark included).

    无 meta.unit_ids 的整份草稿：正文变了的单元清除待编写标记，正文未变的单元逐字节保留（含标记）。"""
    authored = {
        "unit_id": "E1U01",
        "text": "镜头1：远景。@[主角] 站在 @[酒馆] 门口。",
        "duration_seconds": 4,
        "pending_authoring": False,
    }
    rewritten = {"unit_id": "E1U02", "text": "@[主角] 走进 @[酒馆]", "duration_seconds": 4}
    untouched = {"unit_id": "E1U03", "text": "@[主角] 坐在 @[酒馆] 角落", "duration_seconds": 4}
    _write_formal_units(reference_project, [authored, rewritten, untouched])
    before = _formal_units(reference_project)
    formal = _script_path(reference_project)
    write_quarantine(
        reference_project,
        1,
        QUARANTINE_KIND_PROMPT_AUTHORING,
        content={
            "title": "第1集",
            "units": [
                {"text": authored["text"]},
                {"text": "镜头1：中景。@[主角] 走进 @[酒馆]。"},
                {"text": untouched["text"]},
            ],
        },
        violations=[],
        meta={"base_fingerprint": script_review.content_fingerprint(formal)},
    )

    await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(episode=1)

    after = _formal_units(reference_project)
    assert after["E1U01"] == before["E1U01"]
    assert after["E1U02"] == {"unit_id": "E1U02", "text": "镜头1：中景。@[主角] 走进 @[酒馆]。", "duration_seconds": 4}
    assert after["E1U03"] == before["E1U03"]
    assert after["E1U03"]["pending_authoring"] is True
    assert not _prompt_authoring_quarantine(reference_project).exists()


@pytest.mark.asyncio
async def test_promote_prompt_authoring_draft_rejects_stale_formal_baseline(reference_project: Path):
    formal = _script_path(reference_project)
    baseline = script_review.content_fingerprint(formal)
    assert baseline is not None
    write_quarantine(
        reference_project,
        1,
        QUARANTINE_KIND_PROMPT_AUTHORING,
        content={"title": "修复稿", "units": [{"text": PROMPT_AUTHORING_UNIT_TEXT}]},
        violations=[],
        meta={"base_fingerprint": baseline, "unit_ids": ["E1U01"]},
    )

    pm = ProjectManager.for_project_dir(reference_project)
    concurrent = pm.load_script(reference_project.name, formal.name)
    concurrent["title"] = "并发修改"
    pm.save_script(reference_project.name, concurrent, formal.name)

    with pytest.raises(ScriptWriteConflict):
        await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(
            episode=1,
            expected_fingerprint=baseline,
        )

    assert pm.load_script(reference_project.name, formal.name)["title"] == "并发修改"
    assert _prompt_authoring_quarantine(reference_project).exists()


@pytest.mark.asyncio
async def test_promote_prompt_authoring_draft_requires_formal_baseline(reference_project: Path):
    formal_before = _script_path(reference_project).read_bytes()
    write_quarantine(
        reference_project,
        1,
        QUARANTINE_KIND_PROMPT_AUTHORING,
        content={"title": "修复稿", "units": [{"text": PROMPT_AUTHORING_UNIT_TEXT}]},
        violations=[],
    )

    with pytest.raises(DraftViolation) as excinfo:
        await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(episode=1)

    assert excinfo.value.code == "formal_revision_missing"
    assert _prompt_authoring_quarantine(reference_project).exists()
    assert _script_path(reference_project).read_bytes() == formal_before


@pytest.mark.asyncio
async def test_promote_prompt_authoring_draft_reports_again_without_round_limit(reference_project: Path):
    """A repeated violation refreshes the report and the draft stays in place -- promotion can be retried repeatedly,
    with no convergence round limit.

    再违约则刷新报告、草稿留在原地——可反复晋升，无收敛轮次上限。"""
    formal_before = _script_path(reference_project).read_bytes()
    gen = ScriptGenerator(reference_project, generator=_fake_prompt_authoring_generator(BAD_PROMPT_AUTHORING_UNIT_TEXT))
    with pytest.raises(DraftViolation):
        await gen.generate(episode=1)

    path = _prompt_authoring_quarantine(reference_project)
    for _round in range(3):
        with pytest.raises(DraftViolation, match="unregistered_asset"):
            await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(episode=1)
        assert path.exists()
        assert _script_path(reference_project).read_bytes() == formal_before

    envelope = _json.loads(path.read_text(encoding="utf-8"))
    envelope["content"]["units"][0]["text"] = "门开了\n@[主角]：{我来了"
    path.write_text(_json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DraftViolation):
        await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(episode=1)
    refreshed = _json.loads(path.read_text(encoding="utf-8"))
    assert [v["code"] for v in refreshed["violations"]] == ["dialogue_line_syntax"]


@pytest.mark.asyncio
async def test_promote_prompt_authoring_draft_rejects_schema_breach_with_report(reference_project: Path):
    """A draft whose content breaks the schema layer also only gets a report: same rule as script_plan promotion, and the
    final script is not polluted.

    There is no backend to retry on this path (the content is handwritten by the Agent); raising ValueError directly
    would not refresh the violations snapshot in the draft, and the Agent would only see a pydantic error in the tool
    text.

    草稿的 content 被改坏 schema 层同样只回报告：与 script_plan 晋升同口径，正式剧本不被污染。

    这条路上没有 backend 可重试（content 是 Agent 手写的），走 ValueError 直抛的话草稿里的
    violations 快照不会刷新，Agent 只能从工具文本里看到一段 pydantic 报错。
    """
    formal_before = _script_path(reference_project).read_bytes()
    gen = ScriptGenerator(reference_project, generator=_fake_prompt_authoring_generator(BAD_PROMPT_AUTHORING_UNIT_TEXT))
    with pytest.raises(DraftViolation):
        await gen.generate(episode=1)

    path = _prompt_authoring_quarantine(reference_project)
    envelope = _json.loads(path.read_text(encoding="utf-8"))
    envelope["content"]["units"][0].pop("text")
    path.write_text(_json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(DraftViolation, match="schema_invalid"):
        await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(episode=1)

    assert path.exists()
    assert _script_path(reference_project).read_bytes() == formal_before
    refreshed = _json.loads(path.read_text(encoding="utf-8"))
    assert [(v["code"], v.get("item_index"), v.get("item_id")) for v in refreshed["violations"]] == [
        ("schema_invalid", 0, "E1U01")
    ]


@pytest.mark.asyncio
async def test_prompt_authoring_duration_off_tier_after_merge_quarantines(
    wide_tier_reference_project: Path, set_video_request_facts
):
    """An off-option duration detected only after merging also lands in an invalid draft -- this expansion has already
    been paid for.

    prompt_authoring may add or remove `@` references on a unit, switching its effective options: the 2-second
    no-reference unit in the final script gained a reference with a usable reference image during expansion, narrowing
    the options from 1-16 seconds to 3-16 seconds. Reference image constraints only narrow, so "off-option only after
    expansion" can only happen in the direction of adding usable reference images. This check is in `_add_metadata`,
    after the structure-preserving diff; if not caught, the output exists only in memory while the error makes the
    caller regenerate.

    合并之后才判出的档位越界同样落待修复草稿——这份展开已经付过费了。

    prompt_authoring 可以给 unit 增删 `@` 引用，生效档位随之换一套：正式剧本里那个 2 秒的无引用 unit 在展开时
    加进了带可用参考图的引用，档位就从 1–16 秒收窄到 3–16 秒。参考图约束只做收窄，故「展开后才越界」只可能
    发生在增加可用参考图的方向上。这一判在 `_add_metadata` 里、在保结构 diff 之后，不接住的话产物
    只存在于内存里，错误却让调用方重新生成。
    """
    set_video_request_facts(
        {
            "r2v": make_video_request_facts(
                route="reference_video",
                generation_type="r2v",
                supported_durations=tuple(range(1, 17)),
                allowed_durations=tuple(range(3, 17)),
            ),
            "i2v": make_video_request_facts(
                route="reference_video",
                generation_type="i2v",
                supported_durations=tuple(range(1, 17)),
                allowed_durations=tuple(range(1, 17)),
            ),
        }
    )
    project = wide_tier_reference_project
    _claim_character_sheet(project, "主角")
    _write_formal_units(project, [{"unit_id": "E1U01", "text": "他推门", "duration_seconds": 2}])
    formal_before = _script_path(project).read_bytes()
    with_reference_text = "镜头1：中景，平视。@[主角] 推开门，侧身跨过门槛。"
    gen = ScriptGenerator(project, generator=_fake_prompt_authoring_generator(with_reference_text))

    with pytest.raises(DraftViolation) as excinfo:
        await gen.generate(episode=1)

    assert "available durations" in str(excinfo.value)
    assert _script_path(project).read_bytes() == formal_before
    envelope = _json.loads(_prompt_authoring_quarantine(project).read_text(encoding="utf-8"))
    assert [v["code"] for v in envelope["violations"]] == ["duration_off_tier"]
    # The draft still holds the body layer the Agent needs to change; removing the `@` reference allows promoting again
    # 草稿装的仍是 Agent 要改的那一层正文，去掉 `@` 引用即可重新晋升
    assert envelope["content"]["units"][0]["text"] == with_reference_text


@pytest.mark.asyncio
async def test_promote_prompt_authoring_draft_revalidates_edited_formal_unit(reference_project: Path):
    """Before promotion, re-check the current values of final script units with the same pre-check as the output path:
    breaking a body on the timeline while the draft is present must not get written via promotion.

    The editor only warns on human-written bodies, so an unregistered @[name] can be saved; and the
    structure-preserving diff only compares the dialogue structure of the authored body with the unit's current body,
    without re-checking the validity of the unit body itself.

    晋升前按产出路径同一份预判重判正式剧本单元现值：草稿在场期间在时间线上改坏正文不能借晋升落盘。

    编辑器对人写正文只出 warning，改出未登记的 @[名称] 能存下去；而保结构 diff 只比对编写正文
    与单元现有正文的台词结构，不复判单元正文自身的合法性。
    """
    gen = ScriptGenerator(reference_project, generator=_fake_prompt_authoring_generator(BAD_PROMPT_AUTHORING_UNIT_TEXT))
    with pytest.raises(DraftViolation):
        await gen.generate(episode=1)

    path = _prompt_authoring_quarantine(reference_project)
    envelope = _json.loads(path.read_text(encoding="utf-8"))
    envelope["content"]["units"][0]["text"] = PROMPT_AUTHORING_UNIT_TEXT
    path.write_text(_json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

    formal = _script_path(reference_project)
    script = _json.loads(formal.read_text(encoding="utf-8"))
    script["video_units"][0]["text"] = "@[路人甲] 推开 @[酒馆] 的门"
    formal.write_text(_json.dumps(script, ensure_ascii=False), encoding="utf-8")
    formal_before = formal.read_bytes()

    with pytest.raises(DraftViolation, match="正式脚本") as excinfo:
        await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(episode=1)

    assert path.exists()
    assert formal.read_bytes() == formal_before
    # Body violations name the unit by UI position with the specific reason attached, without the internal unit ID
    # 正文违约按界面位置称呼，具体原因随附，不带内部单元 ID
    assert (excinfo.value.code, excinfo.value.params) == ("draft_formal_unit_invalid", {"position": 1})
    assert "video unit 1 references unregistered asset names" in str(excinfo.value)
    assert "E1U01" not in str(excinfo.value)


async def _quarantine_then_edit_formal_unit(project: Path, **changes: Any) -> tuple[Path, bytes]:
    """First store a promotable prompt authoring draft, then change a final script unit on the timeline: return the draft
    path and the bytes of the changed final script.

    先落一份可晋升的提示词编写草稿，再在时间线上改正式剧本的单元：返回草稿路径与改后的正式剧本字节。"""
    gen = ScriptGenerator(project, generator=_fake_prompt_authoring_generator(BAD_PROMPT_AUTHORING_UNIT_TEXT))
    with pytest.raises(DraftViolation):
        await gen.generate(episode=1)
    path = _prompt_authoring_quarantine(project)
    envelope = _json.loads(path.read_text(encoding="utf-8"))
    envelope["content"]["units"][0]["text"] = PROMPT_AUTHORING_UNIT_TEXT
    path.write_text(_json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    formal = _script_path(project)
    script = _json.loads(formal.read_text(encoding="utf-8"))
    script["video_units"][0].update(changes)
    formal.write_text(_json.dumps(script, ensure_ascii=False), encoding="utf-8")
    return path, formal.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "code", "params", "phrase"),
    [
        (
            {"text": "@[主角] 推开 @[酒馆] 的门\n@[主角]：{" + "这是一段非常长的台词" * 6 + "}"},
            "draft_formal_unit_dialogue_overload",
            {"position": 1, "duration_seconds": 4},
            "视频单元 1 的台词念完约需",
        ),
        (
            {"duration_seconds": 5},
            "draft_formal_unit_duration_off_tier",
            {"position": 1, "duration_seconds": 5, "tiers": "4 / 6 / 8"},
            "视频单元 1 的时长 5 秒",
        ),
    ],
    ids=["dialogue_overload", "duration_off_tier"],
)
async def test_promote_prompt_authoring_draft_names_rejected_formal_unit_by_position(
    reference_project: Path, changes: dict[str, Any], code: str, params: dict[str, Any], phrase: str
):
    """A final script unit failed the pre-check before promotion: still reported as a draft violation, with problem code
    and params for the UI to localize as "video unit N", and the message carries no internal ID.

    晋升前正式剧本单元没过预判：仍报草稿违约，问题码与参数供界面按「视频单元 N」本地化，消息不带内部 ID。"""
    path, formal_before = await _quarantine_then_edit_formal_unit(reference_project, **changes)

    with pytest.raises(DraftViolation) as excinfo:
        await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(episode=1)

    violation = excinfo.value
    assert violation.code == code
    assert {key: value for key, value in (violation.params or {}).items() if key != "spoken_seconds"} == params
    assert phrase in str(violation)
    assert "E1U01" not in str(violation)
    assert path.exists()
    assert _script_path(reference_project).read_bytes() == formal_before


@pytest.mark.asyncio
async def test_promote_prompt_authoring_draft_without_draft(reference_project: Path):
    with pytest.raises(FileNotFoundError, match="has no prompt_authoring draft awaiting repair to promote"):
        await ScriptGenerator(reference_project).promote_reference_prompt_authoring_draft(episode=1)


@pytest.mark.asyncio
async def test_rewrite_completed_unit_draft_can_be_hand_fixed_and_adopted(reference_project: Path, monkeypatch):
    original = "镜头1：远景。@[主角] 站在 @[酒馆] 门口。"
    _write_formal_units(
        reference_project,
        [{"unit_id": "E1U01", "text": original, "duration_seconds": 4, "pending_authoring": False}],
    )
    formal = _script_path(reference_project)
    gen = ScriptGenerator(
        reference_project,
        generator=_fake_prompt_authoring_generator("镜头1：@[路人甲] 走进 @[酒馆]"),
    )
    with pytest.raises(DraftViolation):
        await gen.generate(
            episode=1,
            entry_ids=["E1U01"],
            rewrite=True,
            overwrite_revision=script_review.content_fingerprint(formal),
        )
    service = EpisodeDraftService(ProjectManager.for_project_dir(reference_project))
    view = await service.get_draft(reference_project.name, 1, "reference_prompt_authoring")
    assert view["item_ids"] == ["E1U01"]
    use_text_generator(monkeypatch, AsyncMock(return_value=_idle_generator()))
    fixed = "镜头1：中景。@[主角] 走进 @[酒馆]。"
    result = await service.save_draft(
        reference_project.name,
        1,
        "reference_prompt_authoring",
        {"title": "修复稿", "units": [{"text": fixed}]},
        view["revision"],
    )
    assert result["adopted"] is True
    assert _formal_units(reference_project)["E1U01"]["text"] == fixed
    assert not _prompt_authoring_quarantine(reference_project).exists()
