"""Entry type and report rendering for output violations, shared by the three script_plan routes.

A violation is the general shape of one thing, "machine output failed a content constraint": a ``code`` (machine-readable
identifier of the violation class), a ``label`` (location prefix, e.g. ``unit E1U02`` / ``segment E1S03``), an Agent-facing
message, plus an optional line number and utterance admission location. Reference-to-video, drama and narration judge
differently, but the entry shape and the "collect several in one pass, then render a report" basis are identical, so they live
in this module -- the route-neutral layer (top level of ``lib``).

The draft envelope (``lib.script.draft_quarantine``) is unrelated to all three routes yet must persist and render violation
entries; having the types live in the reference-to-video subpackage would make it depend backwards on one specific route. This
module defines these types; each route's validator and the envelope depend only on it;
``lib.script.reference_video.draft_validation`` re-exports these names unchanged, so existing reference-to-video import paths
stay the same.

产出违约的条目类型与报告渲染，三条 script_plan 路线共用。

违约是「机器产物没过内容约束」这一件事的通用形状：一条 ``code``（违约类的机读标识）、一个
``label``（定位前缀，如 ``unit E1U02`` / ``segment E1S03``）、一段面向 Agent 的消息，外加可选的
行号与发声准入定位。参考生视频、drama、narration 的判定各不相同，但条目形状与「一次判定收齐多条
再渲染成报告」的口径完全相同，故收在本模块——路线中立层（``lib`` 顶层）。

草稿信封（``lib.script.draft_quarantine``）与三条路线都无关，却要落盘与渲染违约条目；类型住在
参考生视频子包里会让它反向依赖一条具体路线。本模块是这些类型的定义处，各路线的校验器与信封
都只依赖它；``lib.script.reference_video.draft_validation`` 原样再导出这些名字，参考生视频的既有导入
路径不变。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from pydantic import ValidationError


class DraftViolation(ValueError):
    """Draft output violation. Misuse of the reference syntax is just one kind -- source anchor, dialogue volume, dialogue fidelity
    and generation-side supplementary checks go through this type too. The message includes the location and the way to fix,
    for the tool error envelope to pass back to the Agent as is.

    ``code`` is the machine-readable identifier of the violation class, ``label`` the location prefix (``unit E1U02`` /
    ``segment E1S03`` and the like): the message itself is Agent-facing and its wording may change; report grouping and per-class
    test assertions should not hang on wording. Both may be empty -- when the exception is built outside a validator (e.g. a
    generation-side supplementary check) there is only a message.

    ``line`` is the 0-based raw line number within the unit body (``text.splitlines()`` coordinates, same as the frontend
    ``toScriptLines`` ``sourceLine``), meaningful only when the check happens on a specific line (e.g. syntax misuse); unit-level
    violations with no natural line (dialogue overload, unregistered reference, etc.) leave it empty, so the display layer can
    distinguish the two paths "anchored inline" and "aggregated in the card area".

    ``item_index`` is the 0-based index of the entry the violation belongs to in the draft body's entry array (``units`` /
    ``scenes`` / ``segments``), ``item_id`` is that entry's ID (reference unit ``unit_id``, shot ``segment_id``, etc.); the display
    layer attaches violations to entries by index without parsing ``label``. Both empty means an episode-level violation.

    Non-empty ``params`` means ``code`` is an issue-code copy key (configuration issues such as video request facts); the display
    layer localizes by issue code and params and does not show the Agent-facing message.

    草稿产出违约。引用语法误用只是其中一类——原文锚、台词量、台词保真与生成侧的补充
    判定同走这个类型。消息含定位与修复出路，供工具错误信封原样回传给 Agent。

    ``code`` 是违约类的机读标识，``label`` 是定位前缀（``unit E1U02`` / ``segment E1S03`` 一类）：
    消息本身面向 Agent、措辞可改，报告的分组与测试的按类断言不该挂在措辞上。两者均可为空——
    异常在校验器外被构造时（如生成侧的补充判定）只有消息。

    ``line`` 是该单元正文内 0-based 的原始行号（``text.splitlines()`` 坐标系，与前端
    ``toScriptLines`` 的 ``sourceLine`` 同一坐标系），仅在校验发生于具体某一行时才有意义
    （如语法误用）；单元级、无自然行归属的违约（台词量超载、引用未登记等）留空，供呈现层
    区分「行内锚定」与「落卡内聚合区」两条路径。

    ``item_index`` 是违约所在条目在草稿正文条目数组（``units`` / ``scenes`` / ``segments``）里的
    0-based 下标，``item_id`` 是该条目的 ID（参考单元 ``unit_id``、分镜 ``segment_id`` 等）；呈现层
    按下标把违约挂到对应条目上，不解析 ``label``。两者都为空即整集层面的违约。

    ``params`` 非空表示 ``code`` 是问题码文案 key（视频请求事实一类的配置问题），呈现层按问题码与
    参数本地化，不展示面向 Agent 的消息。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        label: str = "",
        line: int | None = None,
        locations: tuple[dict[str, object], ...] = (),
        reason: str | None = None,
        action: str | None = None,
        item_index: int | None = None,
        item_id: str | None = None,
        params: dict[str, object] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.label = label
        self.line = line
        self.locations = locations
        self.reason = reason
        self.action = action
        self.item_index = item_index
        self.item_id = item_id
        self.params = params


def schema_violations(
    exc: ValidationError, content: dict[str, Any], root: str, id_field: str | None = None
) -> list[DraftViolation]:
    """Locate schema field errors by their original entry; top-level or whole-array errors stay as episode-level violations.

    将 schema 字段错误按原始条目定位；顶层或数组整体错误保留为整集违约。"""
    items = content.get(root)
    violations: list[DraftViolation] = []
    for error in exc.errors():
        loc = error["loc"]
        index = loc[1] if len(loc) > 1 and loc[0] == root and type(loc[1]) is int else None
        item = items[index] if isinstance(items, list) and index is not None and index < len(items) else None
        item_id = item.get(id_field) if isinstance(item, dict) and id_field is not None else None
        value = error.get("input")
        shown = (
            f"（当前值：{json.dumps(value, ensure_ascii=False)}）"
            if error["type"] != "missing" and isinstance(value, (bool, int, float, str)) and len(str(value)) <= 40
            else ""
        )
        violations.append(
            DraftViolation(
                f"草稿的 content.{'.'.join(str(part) for part in loc)} 不符合产出结构：{error['msg']}{shown}",
                code="schema_invalid",
                item_index=index,
                item_id=item_id if isinstance(item_id, str) else None,
            )
        )
    return violations


class DraftViolations(DraftViolation):
    """Multiple violations collected in one validation pass. The message is the per-item report; ``items`` keeps the structured entries.

    Inherits :class:`DraftViolation` instead of a separate type: existing callers catch and assert on ``DraftViolation``, and only
    if the aggregate takes the same branch do "one" and "many" not fork into two handling paths.

    一次校验收集到的多条违约。消息即逐条报告，``items`` 保留结构化条目。

    继承 :class:`DraftViolation` 而非另立类型：既有调用方按 ``DraftViolation`` 捕获与断言，
    聚合体走同一分支才不会在「一条」与「多条」之间分叉出两套处置路径。
    """

    def __init__(self, items: Sequence[DraftViolation]):
        super().__init__(render_violation_report(items), code="multiple", label="")
        self.items: list[DraftViolation] = list(items)


def violation_items(exc: DraftViolation) -> list[DraftViolation]:
    """Flatten a single or aggregate violation into an entry list, for report rendering and writing invalid drafts.

    把单条或聚合的违约一律摊平成条目列表，供报告渲染与待修复草稿落盘取用。"""
    return list(exc.items) if isinstance(exc, DraftViolations) else [exc]


def collect_violations(
    checks: Iterable[Callable[[], Any]], *, item_index: int | None = None, item_id: str | None = None
) -> list[DraftViolation]:
    """Run each check in turn, collecting :class:`DraftViolation` instead of stopping at the first violation.

    A single check function still raises at its first violation (the checks share one traversal and later checks depend on earlier
    conclusions), so one call contributes at most one; passing each unit's check entry points separately lets the report cover all
    units instead of stopping at the first bad one -- the Agent sees everything to fix in one round.

    Only ``DraftViolation`` is swallowed: other exceptions (parser internal errors, type errors from dirty data) propagate as usual
    and are not disguised as a content violation.

    When ``item_index`` / ``item_id`` are given, the violations collected by this batch of checks are located to that entry (ones
    that already carry a location are left alone).

    依次执行各校验，收集 :class:`DraftViolation` 而不在首个违约处中断。

    单个校验函数内部仍是首个违约即抛（各判定共用一次遍历、后续判定以前面的结论为前提），
    故一次调用最多贡献一条；把每个单元的各个判定入口分别传进来，报告就能覆盖到所有单元而
    不是停在第一个坏单元上——Agent 一轮就能看全要改什么。

    只吞 ``DraftViolation``：其余异常（解析器内部错误、脏数据引发的类型错误）照常上抛，
    不被伪装成一条内容违约。

    ``item_index`` / ``item_id`` 给出时，把这批校验收到的违约定位到该条目（已自带定位的不改）。
    """
    found: list[DraftViolation] = []
    for check in checks:
        try:
            check()
        except DraftViolation as exc:
            found.extend(violation_items(exc))
    if item_index is not None or item_id is not None:
        locate_violations(found, item_index=item_index, item_id=item_id)
    return found


def locate_violations(
    violations: Iterable[DraftViolation], *, item_index: int | None = None, item_id: str | None = None
) -> None:
    """Add entry locations to violations not yet located; those already located keep their values. / 给尚未定位的违约补上条目定位；已带定位的保持原值。"""
    for violation in violations:
        if violation.item_index is None and item_index is not None:
            violation.item_index = item_index
        if violation.item_id is None and item_id is not None:
            violation.item_id = item_id


def locate_violations_by_id(violations: Iterable[DraftViolation], item_ids: Sequence[str]) -> None:
    """Fill in ``item_index`` from the position of ``item_id`` in the entry ID sequence. / 按 ``item_id`` 在条目 ID 序列里的位置补上 ``item_index``。"""
    positions = {item_id: index for index, item_id in enumerate(item_ids)}
    for violation in violations:
        if violation.item_index is None and violation.item_id in positions:
            violation.item_index = positions[violation.item_id]


def render_violation_report(violations: Sequence[DraftViolation]) -> str:
    """Render violation entries as a numbered report text (one per line, with the violation class tagged).

    把违约条目渲染成逐条编号的报告文本（一行一条，带违约类标注）。"""
    lines: list[str] = []
    for index, violation in enumerate(violations, start=1):
        suffix = f"[{violation.code}] " if violation.code else ""
        lines.append(f"{index}. {suffix}{violation}")
    return "\n".join(lines)


__all__ = [
    "DraftViolation",
    "DraftViolations",
    "collect_violations",
    "locate_violations",
    "locate_violations_by_id",
    "render_violation_report",
    "violation_items",
]
