"""Tests for split_reference_video_units."""

from __future__ import annotations

import json
from typing import Any

import pytest

from lib.generation.video_request_facts import VideoRequestFactsError, VideoRequestFactsFailure
from lib.script.reference_video.unit_capabilities import evaluate_reference_unit_capabilities
from server.services.tasks.video_caps import reference_request_facts_lookup
from tests.factories import make_video_request_facts, seed_endpoint_fixed_video_model
from tests.fakes import use_text_generator
from tests.integration.server.agent_tool_support import (
    _RV_NOVEL,
    ToolHarness,
    derived_reference_names,
    read_rv_quarantine,
    run_declared_tool,
    run_rv_split,
    rv_generator_returning,
    rv_script_plan_path,
    rv_source,
    rv_unit,
    said,
)


def rv_character_sheet(fake_ctx: ToolHarness, name: str, *, claimed: bool) -> None:
    """Put an asset sheet on a registered character; with ``claimed``, artifact activation makes the manifest claim it, and
    only then is the unit bucketed as r2v.

    给已登记角色落一张资产图；``claimed`` 时经产物激活让清单认领它，单元据此才按 r2v 定桶。"""
    from lib.artifacts.artifact_activation import activate_artifact_target_state

    sheet = fake_ctx.project_path / "characters" / f"{name}.png"
    sheet.parent.mkdir(parents=True, exist_ok=True)
    sheet.write_bytes(b"png")
    fake_ctx.pm.project_payload["characters"][name]["character_sheet"] = f"characters/{name}.png"
    (fake_ctx.project_path / "project.json").write_text(
        json.dumps(fake_ctx.pm.project_payload, ensure_ascii=False), encoding="utf-8"
    )
    if claimed:
        activate_artifact_target_state(fake_ctx.project_path, bump_schema=False)


def _reference_facts(generation_type: str, **overrides: Any):
    """Video request facts for one bucket of the reference-to-video route; fields overridden as needed.

    参考生视频路线某一桶的视频请求事实；字段按需覆盖。"""
    return make_video_request_facts(route="reference_video", generation_type=generation_type, **overrides)


def _veo_720p_facts(set_video_request_facts) -> None:
    """Veo at 720p 'with reference images, 8 seconds only': the r2v bucket narrows to [8], the i2v bucket is still
    [4, 6, 8]; the two per-unit tier sets diverge.

    Veo 在 720p 下「带参考图仅 8 秒」：r2v 桶收窄到 [8]，i2v 桶仍是 [4, 6, 8]，两套逐 unit 档位分叉。"""
    set_video_request_facts(
        {
            "r2v": _reference_facts("r2v", supported_durations=(4, 6, 8), allowed_durations=(8,)),
            "i2v": _reference_facts("i2v", supported_durations=(4, 6, 8), allowed_durations=(4, 6, 8)),
        }
    )


# ---------------------------------------------------------------------------
# split_reference_video_units
# ---------------------------------------------------------------------------


async def test_fetch_reference_split_caps_returns_declared_slots(set_video_request_facts) -> None:
    """The unit duration is exactly the value sent to the provider; tiers are taken verbatim from the facts of both buckets
    (not intersected with any static range).

    unit 时长就是发给供应商的那个值，档位原样取自两桶事实（不与任何静态区间求交）。"""
    from server import text_generation as mod

    set_video_request_facts(
        _reference_facts(
            "r2v", supported_durations=(1, 8, 16, 18), allowed_durations=(1, 8, 16, 18), max_reference_images=None
        )
    )

    caps = await mod.fetch_reference_split_caps({"default_duration": 16})

    assert caps.durations == [1, 8, 16, 18]
    assert caps.reference_durations == [1, 8, 16, 18]
    assert caps.text_durations == [1, 8, 16, 18]
    assert caps.max_duration == 18
    assert caps.default_duration == 16  # Is a tier member, accepted as usual / 是档位成员，照常采信
    assert caps.max_refs is None
    assert caps.text_problem is None


async def test_fetch_reference_split_caps_splits_tiers_by_bucket(set_video_request_facts) -> None:
    """The 'reference image <-> duration' constraint applies per unit: under Veo 720p units with references only have 8 seconds,
    units without still have 4/6/8.

    Enum and prompt candidates take the union -- always narrowing as if images were attached would also remove the short
    tiers that unreferenced units could ask for.

    「参考图↔时长」约束逐 unit 生效：Veo 720p 下带引用只剩 8 秒，无引用仍有 4/6/8。

    枚举与 prompt 候选取并集——一律按带图收窄会把无引用 unit 本可申请的短档也收掉。
    """
    from server import text_generation as mod

    _veo_720p_facts(set_video_request_facts)

    caps = await mod.fetch_reference_split_caps({})
    assert caps.reference_durations == [8]
    assert caps.text_durations == [4, 6, 8]
    assert caps.durations == [4, 6, 8]
    assert caps.max_duration == 8
    assert caps.tiers_for(has_references=True) == [8]
    assert caps.tiers_for(has_references=False) == [4, 6, 8]


async def test_fetch_reference_split_caps_narrows_unit_duration_cap(set_video_request_facts) -> None:
    """The cap follows the narrowed tiers: Hailuo at 1080p only accepts 6 seconds, the full set is [6, 10].

    Without narrowing, script_plan would split units at 10 seconds and prompt_authoring's enum schema would then judge them
    invalid.

    上限随收窄后的档位走：海螺在 1080p 下只接受 6 秒，全集是 [6, 10]。

    不收窄的话 script_plan 会按 10 秒拆出 unit，prompt_authoring 的枚举 schema 再把它判非法。
    """
    from server import text_generation as mod

    set_video_request_facts(_reference_facts("r2v", supported_durations=(6, 10), allowed_durations=(6,)))

    caps = await mod.fetch_reference_split_caps({})
    assert caps.durations == [6]
    assert caps.max_duration == 6


async def test_fetch_reference_split_caps_drops_out_of_range_default(set_video_request_facts) -> None:
    """A saved default_duration that falls outside the union after narrowing becomes None, avoiding a self-contradictory
    prompt.

    收窄后落在并集外的已保存 default_duration 归 None，避免 prompt 自相矛盾。"""
    from server import text_generation as mod

    set_video_request_facts(_reference_facts("r2v", supported_durations=(4, 6, 8), allowed_durations=(8,)))

    assert (await mod.fetch_reference_split_caps({"default_duration": 4})).default_duration is None
    assert (await mod.fetch_reference_split_caps({"default_duration": 8})).default_duration == 8


async def test_fetch_reference_split_caps_reports_unavailable_i2v_as_text_problem(set_video_request_facts) -> None:
    """When the i2v bucket cannot be resolved, the no-image tiers are empty and the failure is carried through verbatim;
    image tiers work as usual; r2v tiers do not stand in for no-image units.

    i2v 桶解析不出时无图档位为空、失败原样带出，带图档位照常；不用 r2v 档位顶替无图 unit。"""
    from server import text_generation as mod

    set_video_request_facts(
        {
            "r2v": _reference_facts("r2v", supported_durations=(6, 10), allowed_durations=(6, 10)),
            "i2v": VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "i2v"),)),
        }
    )

    caps = await mod.fetch_reference_split_caps({})
    assert caps.reference_durations == [6, 10]
    assert caps.text_durations == []
    assert caps.durations == [6, 10]
    assert caps.text_problem is not None
    assert caps.text_problem.code == "reference_capability_unavailable"
    assert caps.text_problem.parameters() == {"capability": "i2v"}


async def test_fetch_reference_split_caps_raises_typed_code_when_r2v_facts_fail(set_video_request_facts) -> None:
    """r2v is this route's main bucket: if it cannot be resolved, raise with the problem code, without falling back to any
    tier set.

    r2v 是本路线的主桶：它解析不出即按问题码抛错，不回退到任何档位集合。"""
    from server import text_generation as mod

    set_video_request_facts(
        {
            "r2v": VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "r2v"),)),
            "i2v": _reference_facts("i2v"),
        }
    )

    with pytest.raises(VideoRequestFactsError) as exc:
        await mod.fetch_reference_split_caps({})

    assert exc.value.code == "reference_capability_unavailable"
    assert exc.value.params == {"capability": "r2v"}


async def test_fetch_reference_split_caps_takes_voice_settings_from_r2v_facts(set_video_request_facts) -> None:
    """Voice input tiers come from the same evaluation of the r2v bucket as the duration tiers; the episode's silent intent
    follows the facts and does not fall back to voiced.

    声音输入档与档位同源于 r2v 桶的这一次求值，本集的无声意图随事实走、不回退成有声。"""
    from server import text_generation as mod

    set_video_request_facts(
        _reference_facts(
            "r2v",
            voice_consistency="native",
            requested_generate_audio=False,
            max_reference_audio_count=2,
            model_id="m",
            reference_audio_per_image=True,
        )
    )

    voice = (await mod.fetch_reference_split_caps({"video_generate_audio": False})).voice
    assert voice.voice_consistency == "native"
    assert voice.requested_generate_audio is False
    assert voice.max_reference_audio == 2
    assert voice.model_id == "m"
    assert voice.requires_reference_image is True
    assert voice.is_silent


async def test_reference_split_planning_reports_empty_intersection_as_incompatible(monkeypatch, db_factory) -> None:
    """When the intersection of image constraints and resolution constraints is empty, the r2v bucket raises with the
    'incompatible' failure code. The model data is replaced with self-contradictory declarations; narrowing still uses the
    real rules.

    带图约束与分辨率约束交集为空时 r2v 桶按「不相容」失败码抛出。型号数据替换成自相矛盾的声明，
    收窄仍走真实规则。"""
    from arcreel_catalog_core import DurationConstraints
    from lib.catalog.query import factory_snapshot
    from lib.config.resolver import ConfigResolver
    from server.text_generation import fetch_reference_split_caps
    from tests.catalog_support import install_catalog, with_entry_facts

    provider_id, model_id = "gemini-aistudio", "veo-3.1-generate-preview"
    constraints = DurationConstraints(by_resolution={"1080p": (6,)}, with_reference_images=(8,))
    install_catalog(
        monkeypatch, with_entry_facts(factory_snapshot(), provider_id, model_id, duration_constraints=constraints)
    )
    project = {
        "generation_mode": "reference_video",
        "video_provider_r2v": f"{provider_id}/{model_id}",
        "video_provider_i2v": f"{provider_id}/{model_id}",
        "model_settings": {f"{provider_id}/{model_id}": {"resolution": "1080p"}},
    }

    with pytest.raises(VideoRequestFactsError) as exc:
        await fetch_reference_split_caps(project, config_resolver=ConfigResolver(db_factory))

    assert exc.value.code == "reference_supported_durations_incompatible"
    assert exc.value.params["capability"] == "r2v"


async def test_reference_split_planning_reports_unavailable_i2v_instead_of_r2v_fallback(db_factory) -> None:
    """A project configured only with an r2v model: no-image units fall into the i2v bucket at execution; planning does not
    substitute r2v tiers and reports i2v unavailable as is.

    只配了 r2v 模型的项目：无图 unit 执行期落 i2v 桶，规划侧不用 r2v 档位顶替，原样报 i2v 不可用。"""
    from lib.config.resolver import ConfigResolver
    from server.text_generation import fetch_reference_split_caps

    project = {"generation_mode": "reference_video", "video_provider_r2v": "dashscope/happyhorse-1.1-r2v"}
    caps = await fetch_reference_split_caps(project, config_resolver=ConfigResolver(db_factory))

    assert caps.reference_durations == list(range(3, 16))
    assert caps.text_durations == []
    assert caps.text_problem is not None
    assert caps.text_problem.code == "reference_capability_unavailable"
    assert caps.text_problem.parameters() == {"capability": "i2v"}
    assert caps.text_problem.action == "configure_video_model"


async def test_reference_split_planning_accepts_five_seconds_from_i2v_facts(db_factory) -> None:
    """No-image tiers are evaluated with the i2v bucket's model: unreferenced units fall into the i2v bucket at execution, so
    the seconds the authoring side allows must match that bucket model's declaration, otherwise r2v-only tiers would be
    allowed and i2v-only tiers missed.

    不带图档位按 i2v 桶模型求值：无引用 unit 执行期落 i2v 桶，创作侧放行的秒数须与该桶模型的
    声明一致，否则会放行 r2v 独有档位、漏掉 i2v 独有档位。"""
    from lib.config.resolver import ConfigResolver
    from server.text_generation import fetch_reference_split_caps

    project = {
        "generation_mode": "reference_video",
        "video_provider_r2v": "gemini-aistudio/veo-3.1-generate-preview",
        "video_provider_i2v": "ark/doubao-seedance-2-0-260128",
    }
    caps = await fetch_reference_split_caps(project, config_resolver=ConfigResolver(db_factory))

    assert caps.reference_durations == [8]
    assert 5 in caps.text_durations
    assert 5 in caps.durations
    assert caps.text_problem is None


@pytest.mark.parametrize("fixed_buckets", [("r2v",), ("i2v",), ("r2v", "i2v")])
async def test_reference_split_planning_borrows_planning_tiers_for_endpoint_fixed_buckets(
    db_factory, fixed_buckets: tuple[str, ...]
) -> None:
    """A bucket with a fixed endpoint has no tiers to borrow; the split still sizes by the shared planning tiers; the other
    bucket narrows by its own facts as usual.

    端点固定的桶没有档位可借，拆分仍按共享的规划档位出篇幅；另一桶照常按自己的事实收窄。"""
    from lib.config.resolver import ConfigResolver
    from lib.generation.video_request_facts import ENDPOINT_FIXED_PLANNING_DURATIONS
    from server.text_generation import fetch_reference_split_caps

    fixed_model = await seed_endpoint_fixed_video_model(db_factory, reference_images=True)
    veo = "gemini-aistudio/veo-3.1-generate-preview"
    project = {
        "generation_mode": "reference_video",
        "video_provider_r2v": fixed_model if "r2v" in fixed_buckets else veo,
        "video_provider_i2v": fixed_model if "i2v" in fixed_buckets else veo,
        "model_settings": {veo: {"resolution": "720p"}},
    }

    caps = await fetch_reference_split_caps(project, config_resolver=ConfigResolver(db_factory))

    assert caps.reference_durations == (ENDPOINT_FIXED_PLANNING_DURATIONS if "r2v" in fixed_buckets else [8])
    assert caps.text_durations == (ENDPOINT_FIXED_PLANNING_DURATIONS if "i2v" in fixed_buckets else [4, 6, 8])
    assert caps.durations == sorted(set(caps.reference_durations) | set(caps.text_durations))
    assert caps.max_duration == max(caps.durations)
    assert caps.text_problem is None


async def test_split_reference_video_units_dry_run(fake_ctx: ToolHarness, video_request_facts) -> None:
    rv_source(fake_ctx)

    out = await run_declared_tool("generate_script_plan", fake_ctx, {"episode_id": 1, "dry_run": True})
    assert out.problem is None, out
    prompt_text = said(out)
    assert "DRY RUN" in prompt_text
    # Episode number, asset candidates and capability constraints go into the prompt; the writing syntax spec is injected with them
    # 集号、资产候选与能力约束进 prompt；引用语法规范随之注入
    assert "第 1 集" in prompt_text
    assert "张三" in prompt_text
    # The cap is the maximum of the union of the two per-unit tier sets, derived from capability resolution
    # 上限是两套逐 unit 档位并集的最大值，随能力解析派生
    assert "8 秒" in prompt_text
    assert "紧跟它所对应的那句动作" in prompt_text


async def test_split_reference_video_units_happy_derives_structure(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """happy path: the LLM only writes flat text, the text is persisted verbatim, only unit_id is mechanically derived by
    the tool.

    happy path：LLM 只写扁平正文，正文逐字落盘，只有 unit_id 由工具机械派生。"""
    from server import text_generation as mod

    rv_source(fake_ctx)
    captured: dict[str, Any] = {}
    text = "@[张三] 走向 @[村口]\n@[张三] 停下脚步"
    units = [rv_unit(text)]
    use_text_generator(monkeypatch, rv_generator_returning(units, captured))

    out = await run_declared_tool("generate_script_plan", fake_ctx, {"episode_id": 1})
    assert out.problem is None, out
    assert "drafts/episode_1/script_plan_reference_units.json" in said(out)
    assert str(fake_ctx.project_path) not in said(out)

    saved = json.loads(rv_script_plan_path(fake_ctx).read_text(encoding="utf-8"))
    unit = saved["units"][0]
    # The fixture script already used E1S01: numbers in this episode are only allocated, never reused; units start at 2
    # 夹具剧本已用过 E1S01：本集编号只分配、不复用，单元从 2 号起
    assert unit["unit_id"] == "E1U2"
    assert unit["text"] == text
    # Reference images are not persisted: the read side always derives them from @[name] in the text
    # 参考图不落盘：读侧一律从正文的 @[名称] 派生
    assert "references" not in unit
    assert derived_reference_names(fake_ctx, unit["text"]) == ["张三", "村口"]
    assert unit["source_text"] == _RV_NOVEL
    assert captured["task_type"] is mod.TextTaskType.SCRIPT
    assert captured["create_project_name"] == "demo"
    assert captured["generate_project_name"] == "demo"


async def test_split_reference_video_units_issues_unit_ids_after_the_highest_used_number(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """unit_id is issued by the server in array order, after the highest number used in this episode: the LLM writes no id,
    so duplicates / wrong episode numbers / reuse are impossible.

    unit_id 由服务端按数组顺序发号，取本集用过的最大号之后：LLM 不写 id，也就不存在重复 / 错集号 / 复用可写。"""
    rv_source(fake_ctx)
    units = [rv_unit("@[张三] 起身"), rv_unit("@[张三] 出门")]
    out = await run_rv_split(fake_ctx, monkeypatch, units)
    assert out.problem is None, out
    saved = json.loads(rv_script_plan_path(fake_ctx).read_text(encoding="utf-8"))
    assert [u["unit_id"] for u in saved["units"]] == ["E1U2", "E1U3"]
    assert fake_ctx.pm.project_payload["episodes"][0]["item_id_high_water"] == 3


async def test_split_reference_video_units_derives_dialogue_without_reference_image(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """The speaker slot of dialogue markup does not go into reference images (attaching a reference image for an off-screen
    speaker would lure them into the frame).

    台词记号的说话人位不进参考图（画外说话的角色附参考图会诱导入画）。"""
    rv_source(fake_ctx)
    units = [rv_unit("门开了\n@[张三]：{我来了。}")]
    out = await run_rv_split(fake_ctx, monkeypatch, units)
    assert out.problem is None, out
    saved = json.loads(rv_script_plan_path(fake_ctx).read_text(encoding="utf-8"))
    assert derived_reference_names(fake_ctx, saved["units"][0]["text"]) == []


async def test_split_reference_video_units_rejects_unregistered_asset(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """Text references an unregistered asset name -> fail-loud, nothing written (asset name reference integrity).

    正文引用未登记资产名 → fail-loud，不写盘（资产名引用完整性）。"""
    rv_source(fake_ctx)
    out = await run_rv_split(fake_ctx, monkeypatch, [rv_unit("@[不存在的人] 出场")])
    assert out.problem is not None
    assert "unregistered asset names" in said(out)
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_split_reference_video_units_rejects_unregistered_speaker(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """An unregistered speaker slot also blocks: the speaker decides which reference audio the line is bound to.

    说话人位未登记同样阻断：说话人决定该句台词绑哪段参考音频。"""
    rv_source(fake_ctx)
    out = await run_rv_split(fake_ctx, monkeypatch, [rv_unit("门开了\n@[无名氏]：{我来了。}")])
    assert out.problem is not None
    assert "speakers not registered" in said(out)
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_split_reference_video_units_rejects_over_max_refs(
    fake_ctx: ToolHarness, monkeypatch, set_video_request_facts
) -> None:
    """The per-unit `@` mention cap is taken from the reference image cap in the r2v bucket facts.

    单 unit 的 `@` 提及上限取 r2v 桶事实的参考图上限。"""
    rv_source(fake_ctx)
    set_video_request_facts(_reference_facts("r2v", max_reference_images=2))
    out = await run_rv_split(fake_ctx, monkeypatch, [rv_unit("@[张三] 与 @[李四] 在 @[村口]")])
    assert out.problem is not None
    assert "above the model limit" in said(out)
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_split_reference_video_units_rejects_duration_off_reference_tier(
    fake_ctx: ToolHarness, monkeypatch, set_video_request_facts
) -> None:
    """A unit with usable reference images takes a duration only valid for no-image units -> violation, the official file is
    not written.

    The enum enforces the union of both tier sets, so this kind of overrun passes the schema; if not blocked here, it
    would fail to be requested only at execution.

    带可用参考图的 unit 取了只有无图 unit 才合法的时长 → 判违约、不写正式文件。

    枚举卡的是两套档位的并集，这类越界过得了 schema；不在此拦，执行期才会申请不到。
    """
    rv_source(fake_ctx)
    _veo_720p_facts(set_video_request_facts)
    rv_character_sheet(fake_ctx, "张三", claimed=True)
    out = await run_rv_split(fake_ctx, monkeypatch, [rv_unit("@[张三] 起身", duration=4)])
    assert out.problem is not None
    text = said(out)
    assert "available durations" in text
    assert "[8]" in text
    # Goes to the draft like the other violation classes: an off-tier duration is also a content violation the Agent can fix
    # by editing the draft
    # 与其余违约类同口径落草稿：档位越界同样是 Agent 改一改草稿就能修好的内容违约
    assert not rv_script_plan_path(fake_ctx).exists()
    assert [v["code"] for v in read_rv_quarantine(fake_ctx)["violations"]] == ["duration_off_tier"]


async def test_split_reference_video_units_locates_the_unit_when_its_i2v_tiers_are_unknown(
    fake_ctx: ToolHarness, monkeypatch, set_video_request_facts
) -> None:
    """When a no-image unit's i2v facts cannot be resolved, the violation carries that unit's location instead of landing in
    the episode-level aggregate.

    无图 unit 的 i2v 事实解析不出时，违约带该 unit 的定位，而不是落到集级聚合区。"""
    rv_source(fake_ctx)
    set_video_request_facts(
        {
            "r2v": _reference_facts("r2v", supported_durations=(6, 10), allowed_durations=(6, 10)),
            "i2v": VideoRequestFactsFailure("reference_capability_unavailable", (("capability", "i2v"),)),
        }
    )
    out = await run_rv_split(fake_ctx, monkeypatch, [rv_unit("门开了", duration=6)])
    assert out.problem is not None
    [violation] = read_rv_quarantine(fake_ctx)["violations"]
    assert violation["code"] == "reference_capability_unavailable"
    assert violation["label"]


@pytest.mark.parametrize("sheet", ["absent", "unclaimed"])
async def test_split_reference_video_units_buckets_a_reference_without_usable_image_as_i2v(
    fake_ctx: ToolHarness, monkeypatch, set_video_request_facts, sheet: str
) -> None:
    """When the character referenced with `@` has no usable reference image (no asset sheet generated, or the image is on
    disk but not claimed by the artifact manifest), the unit falls into i2v like the content review panel and execution:
    4 seconds is valid in the i2v tiers and is not judged off-tier against the image tiers [8].

    `@` 引用的角色没有可用参考图（未生成资产图，或图在盘上但产物清单未认领）时，单元与内容确认
    面板、执行一样落 i2v：4 秒在 i2v 档位内合法，不按带图档位 [8] 判越档。
    """
    rv_source(fake_ctx)
    _veo_720p_facts(set_video_request_facts)
    if sheet == "unclaimed":
        rv_character_sheet(fake_ctx, "张三", claimed=False)
    out = await run_rv_split(fake_ctx, monkeypatch, [rv_unit("@[张三] 起身", duration=4)])
    assert out.problem is None, out
    saved = json.loads(rv_script_plan_path(fake_ctx).read_text(encoding="utf-8"))
    assert saved["units"][0]["duration_seconds"] == 4
    project = json.loads((fake_ctx.project_path / "project.json").read_text(encoding="utf-8"))
    (capability,) = await evaluate_reference_unit_capabilities(
        project, fake_ctx.project_path, saved["units"], request_facts=reference_request_facts_lookup(project)
    )
    assert capability.generation_type == "i2v"


async def test_split_reference_video_units_accepts_wide_tier_without_references(
    fake_ctx: ToolHarness, monkeypatch, set_video_request_facts
) -> None:
    """Units without `@` references are not subject to the 'reference image <-> duration' constraint and can still take
    shorter tiers.

    无 `@` 引用的 unit 不受「参考图↔时长」约束，仍可取更短的档位。"""
    rv_source(fake_ctx)
    _veo_720p_facts(set_video_request_facts)
    out = await run_rv_split(fake_ctx, monkeypatch, [rv_unit("门被风吹开", duration=4)])
    assert out.problem is None, out
    saved = json.loads(rv_script_plan_path(fake_ctx).read_text(encoding="utf-8"))
    assert saved["units"][0]["duration_seconds"] == 4
    assert derived_reference_names(fake_ctx, saved["units"][0]["text"]) == []


async def test_split_reference_video_units_rejects_out_of_enum_duration(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """Local validation reuses the dynamic schema: unit durations outside supported_durations are blocked, not persisted.

    本地校验复用动态 schema：超出 supported_durations 的 unit 时长被拦截，不落盘。"""
    rv_source(fake_ctx)
    out = await run_rv_split(fake_ctx, monkeypatch, [rv_unit("@[张三] 起身", duration=5)])
    assert out.problem is not None
    assert "script_plan split content: structure validation failed" in said(out)
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_split_reference_video_units_rejects_empty_units(fake_ctx: ToolHarness, monkeypatch) -> None:
    rv_source(fake_ctx)
    out = await run_rv_split(fake_ctx, monkeypatch, [])
    assert out.problem is not None
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_split_reference_video_units_rejects_non_verbatim_source_text(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """source_text not a verbatim substring of the source -> loud failure (model paraphrases / fabricates source).

    source_text 非源文逐字子串 → 响亮失败（模型转述 / 杜撰原文）。"""
    rv_source(fake_ctx)
    units = [rv_unit("@[张三] 起身", source_text="张三在城里等人")]
    out = await run_rv_split(fake_ctx, monkeypatch, units)
    assert out.problem is not None
    assert "not a verbatim excerpt of the novel" in said(out)
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_split_reference_video_units_accepts_source_text_substring(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """The anchor only needs to be a substring of the source: a unit is a visual unit and need not cover the whole passage.

    锚只需是源文子串：unit 是画面单元，不必覆盖整段原文。"""
    rv_source(fake_ctx)
    units = [rv_unit("@[张三] 起身", source_text="张三在村口")]
    out = await run_rv_split(fake_ctx, monkeypatch, units)
    assert out.problem is None, out


async def test_split_reference_video_units_rejects_dialogue_overload(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """Dialogue amount estimated by speech rate exceeds the unit duration (beyond the tolerance factor) -> blocked.

    台词量按语速估算超过 unit 时长（宽容系数外）→ 阻断。"""
    rv_source(fake_ctx)
    long_line = "这是一段非常长的台词" * 6  # 60 characters, zh speech rate 5 chars/s -> about 12 seconds / 60 字，zh 语速 5 字/秒 → 约 12 秒
    units = [rv_unit(f"@[张三] 起身\n@[张三]：{{{long_line}}}", duration=4)]
    out = await run_rv_split(fake_ctx, monkeypatch, units)
    assert out.problem is not None
    assert "longer than the unit" in said(out)
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_split_reference_video_units_rejects_braces_in_description(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """Scene description misuses the reserved brace syntax -> blocked (braces not recognized as utterance markup must fail
    loudly).

    画面描述误用花括号保留语法 → 阻断（没被识别成发声记号的花括号须响亮失败）。"""
    rv_source(fake_ctx)
    out = await run_rv_split(fake_ctx, monkeypatch, [rv_unit("@[张三] 推门，音量 {}，转身离开")])
    assert out.problem is not None
    assert "curly braces" in said(out)
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_split_reference_video_units_no_source(fake_ctx: ToolHarness) -> None:
    out = await run_declared_tool("generate_script_plan", fake_ctx, {"episode_id": 1})
    assert out.problem is not None


async def test_split_reference_video_units_injects_instructions(fake_ctx: ToolHarness, video_request_facts) -> None:
    rv_source(fake_ctx)

    out = await run_declared_tool(
        "generate_script_plan",
        fake_ctx,
        {"episode_id": 1, "dry_run": True, "instructions": "单 unit 出场人物尽量不超过两人"},
    )
    assert out.problem is None, out
    prompt_text = said(out)
    assert "# 附加指令" in prompt_text
    assert "单 unit 出场人物尽量不超过两人" in prompt_text


async def test_split_reference_video_units_surfaces_tolerated_voice_warnings(
    fake_ctx: ToolHarness, monkeypatch, set_video_request_facts
) -> None:
    """Three classes of voice degradation warning do not block persisting but are shown with the artifact -- otherwise the
    voice degradation would only be audible after generation.

    三类声音降级 warning 不阻断落盘，但随产物呈现——否则直到生成后才听得出声音打了折。"""
    rv_source(fake_ctx)
    set_video_request_facts(
        _reference_facts("r2v", voice_consistency="native", max_reference_audio_count=2, model_id="m")
    )
    out = await run_rv_split(fake_ctx, monkeypatch, [rv_unit("@[张三] 起身\n@[张三]：{我来了。}")])

    assert out.problem is None, out
    assert rv_script_plan_path(fake_ctx).exists()
    text = said(out)
    assert "降级提示" in text
    assert "未设置参考音频" in text


async def test_split_reference_video_units_names_units_without_scene_reference(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """Units not referencing a scene are named one by one: the location is left to the model, and adjacent units alternating
    indoor/outdoor would not match.

    未引用场景的 unit 逐条指名：地点由模型自由决定，室内外交替的相邻 unit 会对不上。"""
    rv_source(fake_ctx)
    fake_ctx.pm.project_payload["scenes"] = {"酒馆": {"description": "木质吧台"}}
    out = await run_rv_split(
        fake_ctx,
        monkeypatch,
        [rv_unit("@[酒馆] 内景，@[张三] 推门。"), rv_unit("@[张三] 起身。")],
    )

    assert out.problem is None, out
    text = said(out)
    # The receipt locates by the numbers issued on persist (the fixture script already used number 1)
    # 回执按落盘时发出的编号定位（夹具剧本已用过 1 号）
    assert "unit E1U3：" in text
    assert "unit E1U2：" not in text
    assert "未引用场景" in text


async def test_split_reference_video_units_counts_this_episodes_new_scenes_as_scene_references(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """Units that only reference scenes newly added in this episode do not count as unreferenced: consistent with the
    content review page overlaying new items on the asset table.

    只引用本集新增场景的 unit 不算未引用场景：与内容确认页把新增项叠加在资产表上的结论一致。"""
    rv_source(fake_ctx)
    fake_ctx.pm.project_payload["scenes"] = {"酒馆": {"description": "木质吧台"}}
    new_assets = [
        {"type": "scene", "name": "码头", "decision": "register", "reason": "原文第一次出现", "description": "夜雾"}
    ]
    out = await run_rv_split(
        fake_ctx,
        monkeypatch,
        [rv_unit("@[码头] 夜雾，@[张三] 上岸。"), rv_unit("@[张三] 起身。")],
        new_assets=new_assets,
    )

    assert out.problem is None, out
    text = said(out)
    # The receipt locates by the numbers issued on persist (the fixture script already used number 1)
    # 回执按落盘时发出的编号定位（夹具剧本已用过 1 号）
    assert "unit E1U3：" in text
    assert "unit E1U2：" not in text


async def test_split_reference_video_units_reports_soft_violations_alongside_the_violation_report(
    fake_ctx: ToolHarness, monkeypatch, video_request_facts
) -> None:
    """A report of output that violates on production also carries the soft violation section: the Agent should see
    degradation notes in this round of draft repair, not wait until promotion.

    产出即违约的报告同样带软违约段：Agent 修草稿这一轮就该看到降级提示，而非等到晋升。"""
    rv_source(fake_ctx)
    fake_ctx.pm.project_payload["scenes"] = {"酒馆": {"description": "木质吧台"}}
    out = await run_rv_split(
        fake_ctx,
        monkeypatch,
        [rv_unit("@[酒馆] 内景，@[张三] 推门。"), rv_unit("@[不存在的人] 出场")],
    )

    assert out.problem is not None
    text = said(out)
    assert "unregistered asset names" in text
    assert "降级提示" in text
    assert "未引用场景" in text
    assert "unit E1U02：" in text
    assert not rv_script_plan_path(fake_ctx).exists()


async def test_split_reference_video_units_keeps_voice_warnings_on_per_image_backend(
    fake_ctx: ToolHarness, monkeypatch, set_video_request_facts
) -> None:
    """Warnings are still shown under a per-image mounting backend: at split time there are no reference images yet, so that
    slot should not take part in the judgment.

    With ``reference_audio_per_image`` on but no reference image set given, every speaker would be judged 'no frame to
    attach to'; that warning is not in the tolerated list and gets dropped -- and hints like 'over the segment cap' would
    vanish instead.

    逐图挂载型 backend 下 warning 照常呈现：拆分阶段还没有参考图，那一位不该参与判定。

    开着 ``reference_audio_per_image`` 而不给参考图集合，会把每个说话人都判成「无画面可挂」，
    那条 warning 不在容忍列表内会被丢弃——超出段数上限这类提示反而不见了。
    """
    rv_source(fake_ctx)
    fake_ctx.pm.project_payload["characters"] = {
        "张三": {"description": "主角", "reference_audio": "characters/refs_audio/张三.wav"},
        "李四": {"description": "", "reference_audio": "characters/refs_audio/李四.wav"},
    }
    set_video_request_facts(
        _reference_facts(
            "r2v",
            voice_consistency="native",
            max_reference_audio_count=1,
            model_id="m",
            reference_audio_per_image=True,
        )
    )
    out = await run_rv_split(
        fake_ctx, monkeypatch, [rv_unit("@[张三] 起身\n@[张三]：{我来了。}\n@[李四]：{你终于来了。}")]
    )

    assert out.problem is None, out
    assert "参考音频最多 1 段" in said(out)
