"""Video generation precheck: fold the whole-batch admission verdict of one request into a per-unit quote, without
creating tasks.

The quote and the actual submission read the same admission: for reference-to-video the requested duration option
and cost come from the admission ticket itself; for storyboard mode the quote uses the same video request facts
admission used, priced at the script's planned duration. After the precheck the user confirms the duration options,
and the actual submission carries the ``confirmed_request_durations`` the quote gave, so admission will not ask to
confirm the same option again; if the projection changed between the two calls (model switched, planned duration
edited), admission asks for confirmation again as usual.

视频生成预检：把一次请求的整批准入结论折成逐单元的报价单，不建任务。

报价单与正式提交读同一份准入：参考生视频的申请档位与费用取自准入票本身，分镜图生视频按
准入所用的同一份视频请求事实、以剧本编排时长报价。预检之后用户确认了档位，正式提交带上
报价单给出的 ``confirmed_request_durations``，准入就不会再要求确认同一档位；投影在两次调用
之间变了（换了模型、改了编排时长），准入照常重新要求确认。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from lib.db import async_session_factory
from lib.generation.batch_admission import DURATION_CONFIRMATION_CODE, BatchAdmission, UnitAdmissionTicket
from lib.generation.generation_result import GenerationProblem
from lib.generation.video_request_facts import VideoRequestCostFacts, VideoRequestFacts, VideoRequestFactsFailure
from server.services.admission.cost_estimation import quote_video_request


class VideoQuoteOutcome(StrEnum):
    """What will happen to this unit on actual submission.

    正式提交时这个单元会怎样。"""

    #: Will be enqueued for generation (units whose duration option changes, after the confirmed option is carried
    #: along).
    #: 会入队生成（档位变化的单元在带上确认档位之后）。
    GENERATE = "generate"
    #: A usable video already exists; actual submission reuses it as is, free of charge. / 已有可用视频，正式提交会原样复用、不收费。
    REUSE = "reuse"
    #: Has a gap other than the duration confirmation; nothing in the batch is enqueued until it is fixed. / 有确认档位之外的缺口，修复前整批都不会入队。
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class VideoUnitQuote:
    """Precheck verdict for one video unit.

    一个视频单元的预检结论。"""

    unit_id: str
    outcome: VideoQuoteOutcome
    script_duration_seconds: int | None = None
    request_duration_seconds: int | None = None
    cost: Mapping[str, object] | None = None
    problems: tuple[GenerationProblem, ...] = ()

    @property
    def priced_cost(self) -> tuple[float, str] | None:
        """``(amount, currency)`` in the quote; ``None`` when no price can be given (no quote, or an unpriced quote
        with amount ``None``).

        报价里的 ``(金额, 币种)``；报不出价（没有报价，或金额为 ``None`` 的未定价报价）时为 ``None``。"""

        amount = self.cost.get("amount") if self.cost is not None else None
        currency = self.cost.get("currency") if self.cost is not None else None
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not isinstance(currency, str):
            return None
        return float(amount), currency

    @property
    def unpriced(self) -> bool:
        """Will be enqueued but cannot be priced: the Agent must truthfully tell the user that this unit's cost is
        unknown and must not treat it as 0.

        会入队、却报不出价：Agent 须如实告知用户这一单元的费用未知，不能当作 0。"""

        return self.outcome is VideoQuoteOutcome.GENERATE and self.priced_cost is None

    @property
    def tier_changed(self) -> bool:
        """The requested duration option differs from the script's planned duration: the result will be longer or
        shorter than planned, and the cost is computed from the requested option.

        申请档位与剧本编排时长不同：成片会比编排的长或短，费用按申请档位算。"""

        return (
            self.script_duration_seconds is not None
            and self.request_duration_seconds is not None
            and self.request_duration_seconds != self.script_duration_seconds
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "outcome": self.outcome.value,
            "script_duration_seconds": self.script_duration_seconds,
            "request_duration_seconds": self.request_duration_seconds,
            "tier_changed": self.tier_changed,
            "unpriced": self.unpriced,
            "estimated_cost": dict(self.cost) if self.cost is not None else None,
            "problems": [problem.model_dump(mode="json") for problem in self.problems],
        }


@dataclass(frozen=True, slots=True)
class VideoQuoteSheet:
    """Quote of one ``generate_videos`` precheck.

    一次 ``generate_videos`` 预检的报价单。"""

    units: tuple[VideoUnitQuote, ...] = field(default_factory=tuple)

    @property
    def submittable(self) -> bool:
        """The whole batch can be enqueued when submitted with the confirmed duration options.

        带上确认档位正式提交时整批能入队。"""

        return all(unit.outcome is not VideoQuoteOutcome.BLOCKED for unit in self.units)

    @property
    def confirmed_request_durations(self) -> dict[str, int]:
        """Duration confirmations carried as is on actual submission: only units whose option changes and that will be
        enqueued.

        正式提交时原样带上的档位确认：只含档位有变化、会入队的单元。"""

        return {
            unit.unit_id: unit.request_duration_seconds
            for unit in self.units
            if unit.outcome is VideoQuoteOutcome.GENERATE and unit.tier_changed and unit.request_duration_seconds
        }

    @property
    def unpriced_count(self) -> int:
        """Number of units that will be enqueued but cannot be priced.

        会入队、却报不出价的单元数。"""

        return sum(1 for unit in self.units if unit.unpriced)

    @property
    def estimated_total(self) -> dict[str, float] | None:
        """Total cost of the priced part of the units to be enqueued, separated by currency.

        When some units cannot be priced, the total only includes the priced part, and the number of unpriced units
        is in :attr:`unpriced_count`; when no unit is priced at all it is ``None`` (no total can be given), not to
        be confused with the empty total of "no units to charge".

        会入队单元里已定价部分的费用合计，按币种分开。

        有单元报不出价时，合计只含已定价的部分，未定价的单元数见 :attr:`unpriced_count`；一个单元都
        没有定价时为 ``None``（无从给出合计），不与「没有要收费的单元」的空合计混同。
        """

        total: dict[str, float] = {}
        for unit in self.units:
            if unit.outcome is not VideoQuoteOutcome.GENERATE or unit.priced_cost is None:
                continue
            amount, currency = unit.priced_cost
            total[currency] = round(total.get(currency, 0.0) + amount, 6)
        if not total and self.unpriced_count:
            return None
        return total

    def to_payload(self) -> dict[str, object]:
        return {
            "submittable": self.submittable,
            "units": [unit.to_payload() for unit in self.units],
            "estimated_total": self.estimated_total,
            "unpriced_count": self.unpriced_count,
            "confirmed_request_durations": self.confirmed_request_durations,
        }

    def summary(self, log: Sequence[str] = ()) -> str:
        lines = [*log, "预检完成，未入队任何任务。", *(f"- {unit.unit_id}：{_unit_line(unit)}" for unit in self.units)]
        total = self.estimated_total
        unpriced = self.unpriced_count
        if total is None:
            lines.append(f"预计合计：{unpriced} 个单元未定价，无法给出合计，须如实告知用户费用未知。")
        else:
            if total:
                amounts = "、".join(f"{amount:g} {currency}" for currency, amount in total.items())
                lines.append(f"预计合计：{amounts}" + ("（仅含已定价的单元）" if unpriced else ""))
            if unpriced:
                lines.append(f"另有 {unpriced} 个单元未定价，上面的合计不含它们的费用，须如实告知用户。")
        if not self.submittable:
            lines.append("有单元受阻：按各单元的 problems 修复后再预检或提交。")
        elif self.confirmed_request_durations:
            lines.append(
                "档位有变化的单元经用户确认后，正式提交时带上 confirmed_request_durations="
                f"{json.dumps(self.confirmed_request_durations, ensure_ascii=False)}，不会再要求确认档位。"
            )
        return "\n".join(lines)


def _unit_line(unit: VideoUnitQuote) -> str:
    if unit.outcome is VideoQuoteOutcome.REUSE:
        return "已有可用视频，复用，不收费"
    if unit.outcome is VideoQuoteOutcome.BLOCKED:
        return "受阻：" + "；".join(f"{problem.code}（{problem.detail}）" for problem in unit.problems)
    request = f"{unit.request_duration_seconds}s" if unit.request_duration_seconds is not None else "档位待定"
    script = f"{unit.script_duration_seconds}s" if unit.script_duration_seconds is not None else "未写"
    tier = f"申请 {request}（编排 {script}，档位有变化）" if unit.tier_changed else f"申请 {request}"
    cost = unit.cost
    if cost is None:
        price = "未定价（无法为这次请求报价）"
    elif cost.get("amount") is None:
        price = f"未定价（原因：{cost.get('unpriced_reason')}）"
    else:
        price = f"预计 {cost.get('amount')} {cost.get('currency')}"
    return f"{tier}，{price}"


def _own_problems(ticket: UnitAdmissionTicket) -> tuple[GenerationProblem, ...]:
    """Gaps other than the duration confirmation: confirming the duration option is exactly what the quote hands to the
    user, so it does not count as blocked.

    确认档位以外的缺口：档位确认正是报价单要交给用户的事，不算受阻。"""

    return tuple(problem for problem in ticket.problems if problem.code != DURATION_CONFIRMATION_CODE)


def _positive_seconds(value: object) -> int | None:
    """Seconds from the script or projection: positive integers are taken as is; anything else (missing, dirty values,
    booleans) counts as unpriceable.

    剧本或投影里的秒数：正整数原样取用，其余（缺失、脏值、布尔）视为报不出。"""

    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _script_duration(ticket: UnitAdmissionTicket) -> int | None:
    return _positive_seconds((ticket.projection or {}).get("planned_duration"))


def quote_sheet(
    admission: BatchAdmission | None,
    *,
    reused_ids: Sequence[str] = (),
    storyboard_durations: Mapping[str, object] | None = None,
    storyboard_costs: Mapping[str, Mapping[str, object] | None] | None = None,
) -> VideoQuoteSheet:
    """Fold the whole-batch admission into a quote.

    The admission ticket of reference-to-video carries its own projection (planned duration, requested option and
    cost); storyboard-mode tickets do not: the requested option is the script's planned duration, and the cost is
    computed beforehand by :func:`quote_storyboard_video_units` and passed in.

    把整批准入折成报价单。

    参考生视频的准入票自带投影（编排时长、申请档位与费用）；分镜图生视频的票不带，申请档位
    即剧本编排时长，费用由 :func:`quote_storyboard_video_units` 事先算好传入。
    """

    units: list[VideoUnitQuote] = [VideoUnitQuote(unit_id, VideoQuoteOutcome.REUSE) for unit_id in reused_ids]
    for ticket in admission.tickets if admission is not None else ():
        problems = _own_problems(ticket)
        if storyboard_durations is not None and ticket.unit_id in storyboard_durations:
            script_duration = _positive_seconds(storyboard_durations[ticket.unit_id])
            request_duration = script_duration
            cost = (storyboard_costs or {}).get(ticket.unit_id)
        else:
            script_duration = _script_duration(ticket)
            request_duration = ticket.request_duration_seconds
            cost = ticket.request_cost
        units.append(
            VideoUnitQuote(
                unit_id=ticket.unit_id,
                outcome=VideoQuoteOutcome.BLOCKED if problems else VideoQuoteOutcome.GENERATE,
                script_duration_seconds=script_duration,
                request_duration_seconds=request_duration,
                cost=cost,
                problems=problems,
            )
        )
    return VideoQuoteSheet(units=tuple(units))


async def quote_storyboard_video_units(
    facts: VideoRequestFacts | VideoRequestFactsFailure | None,
    durations: Mapping[str, object],
    *,
    aspect_ratio: str,
) -> dict[str, dict[str, object] | None]:
    """Per-unit quote for storyboard mode: by the script's planned duration, using the same video request facts as
    admission.

    ``aspect_ratio`` is the aspect ratio the video request is sent with. When the facts do not hold or the unit has
    no planned duration, no price can be given and ``None`` is recorded; units the price table cannot compute get a
    quote amount of ``None`` with an unpriced reason.

    分镜图生视频逐单元报价：按剧本编排时长、用准入同一份视频请求事实。

    ``aspect_ratio`` 是视频请求发出的画面比例。事实不成立或单元没写编排时长时报不出价，记 ``None``；
    价目表算不出的单元报价金额为 ``None``，带未定价原因。
    """

    costs: dict[str, dict[str, object] | None] = {}
    for unit_id, value in durations.items():
        duration = _positive_seconds(value)
        if not isinstance(facts, VideoRequestFacts) or duration is None:
            costs[unit_id] = None
            continue
        quote = await quote_video_request(
            VideoRequestCostFacts(facts, duration, aspect_ratio=aspect_ratio, has_first_frame=True),
            async_session_factory,
        )
        costs[unit_id] = quote.to_payload() if quote is not None else None
    return costs


__all__ = [
    "VideoQuoteOutcome",
    "VideoQuoteSheet",
    "VideoUnitQuote",
    "quote_sheet",
    "quote_storyboard_video_units",
]
