"""Duration tier selection rules for video requests (capacity semantics) and the duration projection shared by both routes.

A model's ``supported_durations`` are discrete tiers, and the request duration baseline almost never falls
exactly on a tier. Tier selection reads tiers as **capacity**: request the smallest valid tier that fits the
baseline duration; the final cut is not trimmed -- the delivered duration is the tier duration. When the
baseline exceeds the max tier, the max tier is requested (the final cut is shorter than the requested baseline).

Pure functions, no I/O. Quoting, preflight and execution of both the reference-to-video and storyboard routes
first resolve the current provider/model and tier set, then share :func:`project_request_duration`, so the two
routes never reach different tier conclusions for the same state; each route only maps the returned
:data:`DurationProblem` into its own problem envelope.

视频请求的时长取档规则（容量语义）与两条路线共用的时长投影。

模型的 ``supported_durations`` 是离散档位，请求时长基准几乎不会正好落在档位上。
取档按**容量**解读档位：申请能装下基准时长的最小合法档位，成片不做裁剪——交付时长即
档位时长。基准时长超过最大档位时按最大档位申请（成片短于请求基准）。

纯函数，无 I/O。参考生视频与分镜两条路线的报价、预检与执行都先解析当前 provider/model
与档位集，再共用 :func:`project_request_duration`，两条路线因此不会对同一份状态给出不同的
取档结论；各路线只负责把返回的 :data:`DurationProblem` 映射成自己的 problem 信封。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

# Direction of tier deviation relative to the request duration baseline. The literal union in the frontend
# `types/reference-video.ts` matches this; Literal rather than bare str lets the type checker cover branch
# comparisons in `warning()` and the preflight response.
# 取档相对请求时长基准的偏移方向。前端 `types/reference-video.ts` 的字面量联合与此对齐，
# 用 Literal 而非裸 str 让类型检查兜住 `warning()` 与预检响应里的分支判等。
Adjustment = Literal["exact", "up", "down", "unconstrained"]

EXACT: Adjustment = "exact"
"""请求时长基准本身就是档位成员，申请值与基准一致。"""
UP: Adjustment = "up"
"""向上取档：成片长于请求时长基准。"""
DOWN: Adjustment = "down"
"""请求时长基准超过最大档位，按最大档位申请：成片短于请求基准。"""
UNCONSTRAINED: Adjustment = "unconstrained"
"""时长这一维不由 ArcReel 取档：申请值就是规划值，端点自己决定成片多长。"""


@dataclass(frozen=True)
class DurationSlot:
    """Tier selection result. ``seconds`` is the seconds requested from the backend, ``total_seconds`` is the request duration baseline.

    取档结果。``seconds`` 是向 backend 申请的秒数，``total_seconds`` 是请求时长基准。"""

    seconds: int
    total_seconds: int | float
    adjustment: Adjustment

    @property
    def needs_confirmation(self) -> bool:
        """User confirmation is needed when the requested seconds differ from the request duration baseline. / 申请秒数与请求时长基准不一致时需用户确认。"""
        return self.adjustment in (UP, DOWN)

    def warning(self, *, provider: str, model: str) -> dict | None:
        """Task warning (i18n key + params) when tier selection deviated from the request duration baseline; None if no deviation.

        取档偏移了请求时长基准时的任务 warning（i18n key + 参数）；未偏移返回 None。"""
        if not self.needs_confirmation:
            return None
        key = "ref_duration_rounded_up" if self.adjustment == UP else "ref_duration_exceeded"
        return {
            "key": key,
            "params": {"total": self.total_seconds, "duration": self.seconds, "provider": provider, "model": model},
        }


def resolve_duration_slot(total_seconds: int | float, supported_durations: Sequence[int]) -> DurationSlot:
    """Select the requested tier for the request duration baseline with capacity semantics.

    With an empty tier set the total duration is passed through as-is. Executable reference-to-video requests must
    not rely on this branch: ``ReferenceUnitRequestProjector`` first returns a structured blocker for missing, empty
    or invalid tiers. A non-empty tier set need not be ordered and may contain duplicates.

    Non-integer total durations (e.g. 4.5) are compared by "fits" as well, taking the smallest tier ≥ it; no
    truncating normalization, to avoid silently shortening a duration that should round up.

    按容量语义为请求时长基准选择申请档位。

    档位集为空时原样透传总时长。可执行的参考生视频请求不得
    依赖该分支：``ReferenceUnitRequestProjector`` 对缺失、空或无效的档位先返回结构化
    blocker。非空档位集不要求有序、允许重复。

    非整数秒总时长（如 4.5）同样按「能装下」比较，取 ≥ 它的最小档位；不做截断式
    归一化，避免把本该向上取的时长静默缩短。
    """
    slots = sorted({int(d) for d in supported_durations})
    if not slots:
        return DurationSlot(seconds=int(total_seconds), total_seconds=total_seconds, adjustment=UNCONSTRAINED)
    fitting = [d for d in slots if d >= total_seconds]
    adjustment: Adjustment
    if fitting:
        chosen = fitting[0]
        adjustment = EXACT if chosen == total_seconds else UP
    else:
        chosen = slots[-1]
        adjustment = DOWN
    return DurationSlot(seconds=chosen, total_seconds=total_seconds, adjustment=adjustment)


#: Problem categories from the shared duration projection. Callers map them to problem codes in their own envelope:
#: the category is the verdict, while code and copy belong to each route's external contract.
#: 共享时长投影给出的问题类别。调用方按自己的信封映射成 problem code：类别是判定，
#: code 与文案属各路线的对外契约。
DurationProblem = Literal[
    "supported_durations_missing",
    "needs_replan",
    "confirmation_required",
]


@dataclass(frozen=True)
class RequestDurationProjection:
    """Duration conclusion of one request: requested tier, tier deviation and the single blocking category.

    ``slot`` being None means the tier declaration is missing and no requestable seconds can be computed; ``problem``
    is then always non-empty. ``endpoint_fixed`` true means this dimension is not driven by ArcReel, and
    ``slot.seconds`` is the planned seconds passed through as-is rather than a tier member.

    一次请求的时长结论：申请档位、取档偏移与唯一的阻断类别。

    ``slot`` 为 None 表示档位声明缺失、算不出可申请的秒数；此时 ``problem`` 必然非空。
    ``endpoint_fixed`` 为真表示这一维不由 ArcReel 驱动，``slot.seconds`` 是原样透传的规划
    秒数而非档位成员。
    """

    duration_input: int
    slot: DurationSlot | None
    endpoint_fixed: bool
    problem: DurationProblem | None


def project_request_duration(
    *,
    planned_duration_seconds: int,
    supported_durations: Sequence[int],
    duration_endpoint_fixed: bool = False,
    confirmed_request_duration_seconds: int | None = None,
    confirmation_waived: bool = False,
) -> RequestDurationProjection:
    """Project the planned duration and the model tiers into the duration conclusion of one request; the request duration
    baseline is the planned duration.

    When ``duration_endpoint_fixed`` is true, an empty tier set is a valid state (the duration dimension is fixed by
    the endpoint, see ``docs/adr/0082``): no narrowing, no confirmation required, the planned seconds pass through
    as-is, and the endpoint produces at its workflow's own tier.

    An empty set without ``duration_endpoint_fixed`` is a missing tier declaration (``docs/adr/0018``) and still
    blocks as ``supported_durations_missing``.

    把编排时长与模型档位投影成一次请求的时长结论；请求时长基准就是编排时长。

    ``duration_endpoint_fixed`` 为真时档位集是空的合法状态（时长这一维由端点固定，见
    ``docs/adr/0082``）：不收窄、不要求确认，规划秒数原样透传，端点会按 workflow 自己那档出片。

    不带 ``duration_endpoint_fixed`` 的空集是档位声明缺失（``docs/adr/0018``），仍按
    ``supported_durations_missing`` 阻断。
    """

    if isinstance(planned_duration_seconds, bool) or planned_duration_seconds <= 0:
        raise ValueError("planned_duration_seconds must be a positive integer")

    if duration_endpoint_fixed:
        return RequestDurationProjection(
            duration_input=planned_duration_seconds,
            slot=DurationSlot(
                seconds=planned_duration_seconds,
                total_seconds=planned_duration_seconds,
                adjustment=UNCONSTRAINED,
            ),
            endpoint_fixed=True,
            problem=None,
        )

    durations = tuple(
        sorted({duration for duration in supported_durations if not isinstance(duration, bool) and duration > 0})
    )
    if not durations:
        return RequestDurationProjection(
            duration_input=planned_duration_seconds,
            slot=None,
            endpoint_fixed=False,
            problem="supported_durations_missing",
        )

    slot = resolve_duration_slot(planned_duration_seconds, durations)
    problem: DurationProblem | None = None
    if slot.adjustment == DOWN:
        problem = "needs_replan"
    elif (
        slot.seconds != planned_duration_seconds
        and not confirmation_waived
        and confirmed_request_duration_seconds != slot.seconds
    ):
        problem = "confirmation_required"
    return RequestDurationProjection(
        duration_input=planned_duration_seconds,
        slot=slot,
        endpoint_fixed=False,
        problem=problem,
    )
