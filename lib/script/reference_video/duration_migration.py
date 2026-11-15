"""Legacy migration of reference-to-video unit durations: fold per-shot durations into the unit level.

Duration used to live on ``shots[*].duration``, with unit duration derived as their sum. After the fold, unit
duration is the single source of truth (``ReferenceVideoUnit.duration_seconds``) and shots no longer carry
duration. Stored data therefore needs a one-time rewrite: write the sum into the unit field, and strip the shot
durations and the ``duration_override`` marker retired along with them.

The migrator is a pure function (mutates the passed dict in place, returns whether it changed + a warning list);
the loading side does the read-modify-write under a lock -- migration is saved once, whoever runs first decides
the final state, and a second load does not trigger it again. Because of this, ``supported_durations`` must have
the same source at every entry point: the two online draft entry points (content review conversion, web content
review) both take the union of the full declared sets of the r2v and i2v video request facts, so saved seconds
are always a tier member. Archive import has no config DB session and can't evaluate video request facts, so it
looks up the union of the registry's full declared sets for the two buckets' identities self-reported by the
archive; saved seconds are only guaranteed to be a tier declared by one of the two buckets.

The two cases where ``supported_durations`` is None only do a structural range clamp: the project has no
resolvable video model configured yet, and the ``migrate_script_unit_durations`` episode script synchronous load
chain. In that case tier deviation is still handled by tier selection at preflight / execution
(``resolve_duration_slot``) with a warning, same semantics as before migration.

**The episode script side does not do tier convergence (enforcement write-back); this is the final division of
labor, not a gap to fill later**:

- At save time it is necessarily on a tier -- ``ScriptGenerator._add_metadata`` selects a tier per unit from the
  final references and errors on out-of-tier. Deviation can only come from later model / resolution changes.
- After deviation no dirty value reaches the provider either -- preflight and execution share the request
  projection (``ReferenceUnitRequestProjector``), both re-select the tier and emit a warning; only tasks actually
  submitted to the provider write the actual requested seconds into the dedicated execution checkpoint for
  resume to read; neither the task payload nor the script itself is rewritten.
- Write-back would conflict with repo-wide policy: out-of-tier is always "fail-loud or warning + prompt to
  reselect", never silently rewriting the user's arrangement; also preflight is a GET read path where writing to
  disk is unacceptable, and writing at execution time would race with user edits.
- Load-time write-back has a hard blocker too: ``load_script`` is a synchronous call without a DB session and
  can't get the tier table.

参考生视频 unit 时长的存量迁移：per-shot 时长收编到 unit 级。

时长曾经挂在 ``shots[*].duration`` 上、unit 时长是它们的求和派生。收编后 unit 时长是唯一
真相（``ReferenceVideoUnit.duration_seconds``），镜头不再承载时长。存量落盘因此需要一次性
改写：求和写进 unit 字段、剥掉镜头上的时长与随之退役的 ``duration_override`` 标记。

迁移器是纯函数（就地改写传入的 dict、返回是否变更 + warning 列表），由加载侧在锁内做
read-modify-write 回写——迁移一次落盘、谁先跑谁定终局，二次加载不再触发。正因如此，各入口的
``supported_durations`` 须同源：草稿的两个在线入口（内容确认转换、web 内容确认）都取 r2v 与 i2v 两桶
视频请求事实声明全集的并集，落盘秒数因而必是档位成员。归档导入没有配置库会话、求值不了视频请求事实，
按归档自报的两桶身份查 registry 声明全集的并集，落盘秒数只保证是两桶之一声明的档位成员。

``supported_durations`` 为 None 的两种情形只做结构区间 clamp：项目尚未配置可解析的视频型号，
以及 ``migrate_script_unit_durations`` 这条剧集脚本同步加载链。此时档位偏移仍由预检 / 执行时的
取档（``resolve_duration_slot``）承担并记 warning，与迁移前语义一致。

**剧集脚本侧不做档位收敛（enforcement 回写），这是终局分工，不是待补的缺口**：

- 落盘时刻已必然在档——``ScriptGenerator._add_metadata`` 按最终 references 逐 unit 取档，出档直接
  报错。偏移只可能来自事后改模型 / 改分辨率。
- 偏移后也没有脏值能到供应商——预检与执行共用请求投影（``ReferenceUnitRequestProjector``），
  都会重新取档并出 warning；只有真正提交 provider 的任务才把实际申请秒数写入
  专用 execution checkpoint 供 resume 读取，task payload 与脚本本身都不改写。
- 回写会与全仓策略冲突：出档一律「fail-loud 或 warning + 引导重选」，从不静默改写用户编排真相；
  且预检是 GET 读路径，在其上写盘不可接受，执行期写盘则会与用户编辑竞争。
- 加载期回写另有硬阻：``load_script`` 是同步调用、无 DB 会话，拿不到档位表。
"""

from __future__ import annotations

from collections.abc import Sequence

from arcreel_market_core.validation_messages import ValidationMessage
from lib.script.reference_video.duration_slots import resolve_duration_slot
from lib.script.script_models import REFERENCE_UNIT_DURATION_RANGE

#: Shot-level fields and retired unit-level fields that the migration strips. / 迁移会剥掉的镜头级字段与 unit 级退役字段。
_LEGACY_SHOT_FIELD = "duration"
_LEGACY_UNIT_FIELD = "duration_override"


def _positive_int(value: object) -> int | None:
    """Only accept genuine positive integers (bool excluded as an int subclass); everything else counts as no value -- same convention as dirty data in script entry durations.

    只认真正的正整数（bool 按 int 子类排除），其余按无值——与剧本条目时长的脏数据口径一致。"""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _strip_legacy_shot_durations(unit: dict) -> tuple[int | None, bool]:
    """Strip each shot's ``duration`` and return ``(sum of durations, whether any field was stripped)``.

    A sum of None means "no usable duration source" -- no shot durations, or all dirty values (sum 0), both fall
    here: 0 seconds is not a valid duration, and in both cases the caller should fall back to the unit's own field
    rather than invent a value. The second item is returned separately: stripping fields is itself a change that
    must be saved, regardless of whether the sum is usable.

    剥掉各镜头的 ``duration``，返回 ``(时长之和, 是否剥掉过字段)``。

    和为 None 表示「没有可用的时长来源」——无镜头时长、或全是脏值（和为 0）都归此类：
    0 秒不是合法时长，两种情形都应让调用方回退到 unit 自身的字段而非杜撰一个值。
    第二项独立回传：剥字段本身就是需要落盘的变更，与和是否可用无关。
    """
    shots = unit.get("shots")
    if not isinstance(shots, list):
        return None, False
    total = 0
    stripped = False
    for shot in shots:
        if not isinstance(shot, dict) or _LEGACY_SHOT_FIELD not in shot:
            continue
        stripped = True
        total += _positive_int(shot.pop(_LEGACY_SHOT_FIELD)) or 0
    return (total or None), stripped


def migrate_unit_durations(
    units: object,
    *,
    supported_durations: Sequence[int] | None = None,
) -> tuple[bool, list[ValidationMessage]]:
    """Fold the units' per-shot durations into the unit level in place. Returns ``(changed, warnings)``.

    warnings are locale-neutral ``ValidationMessage`` objects rendered at the consuming boundary (archive import
    uses the request language, the load chain only logs).

    Each unit's target duration prefers the existing ``duration_seconds`` (before the fold it was already the sum
    or a user-entered value, and in both cases the seconds this unit actually requested); if missing or dirty it
    falls back to the sum of the shot durations. Units where neither is available only get fields stripped and no
    invented duration -- they already lacked a required field before the fold, and migration is not responsible for
    turning invalid data valid.

    When ``supported_durations`` is given, the tier is selected with capacity semantics (above the max tier it
    clamps to the max tier), and deviation is recorded as a warning; otherwise it only clamps to the
    ``REFERENCE_UNIT_DURATION_RANGE`` structural range.

    就地把 units 的 per-shot 时长收编到 unit 级。返回 ``(是否发生变更, warnings)``。

    warnings 是 locale-neutral 的 ``ValidationMessage``，由消费边界渲染（归档导入按请求语言，
    加载链只落日志）。

    每个 unit 的目标时长优先取已有的 ``duration_seconds``（收编前它已是求和结果或用户手填值，
    两种情形下都已是这个 unit 实际申请的秒数），缺失或脏值时回退为各镜头时长之和。两者都取不到
    的 unit 只剥字段、不杜撰时长——它在收编前就缺必填字段，迁移不负责把非法数据补成合法。

    ``supported_durations`` 给定时按容量语义取档（超出最大档位即 clamp 到最大档位），
    偏移记 warning；缺省则只 clamp 到 ``REFERENCE_UNIT_DURATION_RANGE`` 结构区间。
    """
    if not isinstance(units, list):
        return False, []

    changed = False
    warnings: list[ValidationMessage] = []
    low, high = REFERENCE_UNIT_DURATION_RANGE

    for unit in units:
        if not isinstance(unit, dict):
            continue

        legacy_total, stripped = _strip_legacy_shot_durations(unit)
        had_override = _LEGACY_UNIT_FIELD in unit
        unit.pop(_LEGACY_UNIT_FIELD, None)
        if not stripped and not had_override:
            continue
        changed = True

        target = _positive_int(unit.get("duration_seconds")) or legacy_total
        if target is None:
            continue

        clamped = min(max(target, low), high)
        if clamped != target:
            warnings.append(
                ValidationMessage(
                    "val_unit_duration_clamped",
                    {"unit_id": unit.get("unit_id"), "target": target, "low": low, "high": high, "clamped": clamped},
                )
            )
        if supported_durations:
            slot = resolve_duration_slot(clamped, supported_durations)
            if slot.seconds != clamped:
                warnings.append(
                    ValidationMessage(
                        "val_unit_duration_slotted",
                        {
                            "unit_id": unit.get("unit_id"),
                            "duration": clamped,
                            "durations": sorted(set(supported_durations)),
                            "slot": slot.seconds,
                        },
                    )
                )
            clamped = slot.seconds
        unit["duration_seconds"] = clamped

    return changed, warnings


def migrate_script_unit_durations(script: object) -> tuple[bool, list[ValidationMessage]]:
    """Episode script level entry point: only applies to the reference-to-video skeleton (with ``video_units``).

    Decided by data shape rather than the ``generation_mode`` stamp -- dirty data and half-finished scripts may lack
    the stamp, and the fold only cares "are there per-shot durations to strip", which is naturally a no-op for other
    skeletons.

    剧集脚本级入口：仅对参考生视频骨架（含 ``video_units``）生效。

    按数据形状而非 ``generation_mode`` 戳判定——脏数据与半成品剧本可能缺戳，而收编只关心
    「有没有 per-shot 时长要剥」，对其它骨架天然是空操作。
    """
    if not isinstance(script, dict):
        return False, []
    return migrate_unit_durations(script.get("video_units"))
