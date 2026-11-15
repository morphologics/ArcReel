"""Test connection: really submit one generation and poll to a terminal state; this incurs cost.

Video and image endpoints share this one path: they differ only in which generation request is built, which
``call_type`` is recorded and which filename the artifact is saved under, while the six things "submit, poll, fetch,
account, write to disk, cancel" do not depend on whether it produces an image or a video.

It runs the production path -- the same backend class, the same ``poll_with_retry`` (on the video side the global
``video_poll_timeout_seconds``, consecutive failure budget, backoff and ``Retry-After``), the same download path.
Only the carrier differs: an in-process asyncio task, not the tasks/worker queue, and the artifact goes into neither
a project nor the asset library. Only this way does "test connection passed" mean "this model row really works"
rather than "another path that only exists in tests works".

State lives in memory only until the terminal state; at the terminal state it is written to disk
(``DataRootLayout.trial_runs_dir/{id}/``) and removed from memory, and the read endpoints always read from disk; the
whole directory is removed after 24 hours. Cancelling stops the local polling and the accounting settles as failed --
whether the remote side can be stopped is decided by the backend on its own cancel path and is unrelated to
settlement: the money may already be spent, and the ledger cannot pretend nothing happened because the user clicked
cancel.

测试连接：真实提交一次生成并轮询到终态，会产生费用。

视频与图像两类端点共用这一条路径：差别只在构造哪种生成请求、记哪种 ``call_type``、产物落成哪个
文件名，而「提交、轮询、取件、记账、写盘、取消」这六件事与产的是图还是片无关。

跑的是生产那一条路——同一个 backend 类、同一个 ``poll_with_retry``（视频侧全局
``video_poll_timeout_seconds``、连续失败预算、退避与 ``Retry-After``）、同一个下载路径。只有承载
不同：进程内 asyncio 任务，不走 tasks/worker 队列，产物不进项目也不进资产库。这样「测试连接通过」
才等价于「这个模型行真的能用」，而不是等价于「另一条只在测试里存在的路径能用」。

状态只在内存里活到终态，终态一到写盘（``DataRootLayout.trial_runs_dir/{id}/``）并从内存移除，读接口
一律读盘；24 小时后整目录清掉。取消停的是本地轮询，记账按失败结算——远端叫不叫得停由 backend 自己在
它的取消路径上决定，与结算无关：钱可能已经花了，账本不能因为用户点了取消就假装没发生。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from arcreel_market_core.endpoint_definition import definition_media_type
from arcreel_market_core.video_backend_contract import (
    ProviderResponseStage,
    VideoCapabilities,
    VideoCapabilityError,
    VideoGenerationRequest,
)
from lib.backends.backend_assembly.specs import apply_user_added_video_overrides
from lib.backends.container_sniff import CONTAINER_HEAD_BYTES, sniff_container
from lib.backends.image_backends.base import (
    ImageCapability,
    ImageCapabilityError,
    ImageGenerationRequest,
    ReferenceImage,
)
from lib.backends.providers import CALL_TYPE_IMAGE, CALL_TYPE_VIDEO, CallPurpose
from lib.backends.video_frame_slots import resolve_first_frame_aspect_ratio, resolve_video_capabilities
from lib.billing.ledger import Ledger
from lib.config.resolver import ConfigResolver
from lib.custom_provider import is_custom_provider
from lib.custom_provider.comfyui.failures import ComfyuiError
from lib.custom_provider.declarative_backend import DeclarativeRuntimeError, DeclarativeVideoBackend
from lib.custom_provider.declarative_image_backend import DeclarativeImageBackend
from lib.db.base import DEFAULT_USER_ID
from lib.db.repositories.usage_repo import bound_provider_response
from lib.generation.task_failure import encode_failure
from lib.infra.async_thread import run_noninterruptible_async, run_noninterruptible_sync, run_sync_transaction
from lib.infra.data_root_layout import DataRootLayout

from .check import STAGES, check_response, stage_report_payload
from .inputs import EndpointTestCredentials, EndpointTestParameters
from .modes import EndpointTestMode, supports_test_mode

logger = logging.getLogger(__name__)

#: Lifetime of result directories. Cleanup happens at process start and on every new run; no timer is scheduled.
#: 结果目录的存活时长。清理时机是进程启动与每次新建，不挂定时器。
TRIAL_RUN_TTL_SECONDS = 24 * 3600

#: Number of poll responses kept in the result body. / 结果体里保留的轮询响应条数。
MAX_POLL_RESPONSES = 20

#: The four stage points the result body gives: the first three are the three stages the runtime traces, the fourth is saving the artifact.
#: 结果体给出的四段状态点：前三段是运行时留痕的三个阶段，第四段是产物落盘。
TRIAL_RUN_STAGES = ("submit", "poll", "result", "artifact")

_RESULT_FILE = "result.json"

#: ``media_type`` -> filename the artifact is saved as. The extension is not decoration: besides read endpoints
#: declaring MIME from the saved bytes, the two artifact kinds must also be distinguishable in the same result directory.
#: ``media_type`` → 产物落盘的文件名。扩展名不是装饰：读接口按落盘字节声明 MIME 之外，两类产物
#: 在同一个结果目录里也要分得开。
_ARTIFACT_FILE_BY_MEDIA_TYPE: Mapping[str, str] = {"video": "artifact.mp4", "image": "artifact.png"}

#: Shape of the run_id generated by ``start`` (``uuid4().hex``). / ``start`` 生成的 run_id 形状（``uuid4().hex``）。
_RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class TrialRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TrialRunBusyError(Exception):
    """This user already has a test connection running. / 该用户已有一个测试连接在跑。"""


@dataclass(frozen=True)
class TrialRunTarget:
    """Target of one test connection: accounting identity + how to build the backend.

    The inline definition and model row entry points converge here into the same value object, after which accounting,
    polling, disk writes and cancellation are identical for both -- "built-in and custom share the same resource, the
    same result body, the same accounting" is a direct consequence of this convergence point. ``definition`` is only
    present when the target is a declarative endpoint (an inline definition, or a model row carrying a custom / built-in
    declarative endpoint); it decides whether the result body can give the rendered request and per-stage extraction;
    for Python-implemented endpoints both sections are empty.

    一次测试连接的目标：记账身份 + backend 构造方式。

    内联定义与模型行两条入口在这里汇合成同一个值对象，之后的记账、轮询、写盘与取消对两者完全
    一致——「内置与自定义同一资源、同一结果体、同一记账」就是这个汇合点的直接结果。
    ``definition`` 在目标是声明式端点（内联定义，或模型行挂着自定义 / 内置声明式端点）时才有，
    它决定结果体里能不能给出渲染后的请求与逐阶段提取；Python 实现的端点两段为空。
    """

    provider: str
    model: str
    build_backend: Callable[[], Awaitable[Any]]
    definition: Mapping[str, Any] | None = None
    #: Whether this endpoint produces video or images: decides which generation request is built, which call_type is
    #: recorded and which filename the artifact gets. Both declarative and ComfyUI endpoints declare it in their definition, read via ``definition_media_type``.
    #: 这个端点产的是视频还是图像：决定构造哪种生成请求、记哪种 call_type、产物落成哪个文件名。
    #: 声明式与 ComfyUI 端点都由定义自己声明，经 ``definition_media_type`` 读取。
    media_type: str = "video"
    #: Whether to run the production capability gate before submitting. Paid channels always run it (declared violations
    #: are rejected before paying); ComfyUI endpoints have a fixed cost of 0, and capabilities are derived from node bindings
    #: which the inline definition entry point cannot get, so running the gate would only turn "the binding missed the first
    #: frame" into a capability rejection -- while the user clicks test connection precisely to see what ComfyUI itself says.
    #: 提交前跑不跑生产那道能力闸。付费通道一律跑（声明的违约在付费前拒绝）；ComfyUI 端点费用
    #: 固定 0，且能力由节点绑定推导而内联定义这条入口拿不到推导结果，跑闸只会把「绑定漏了首帧」
    #: 说成一条能力拒绝——而用户点测试连接正是为了看 ComfyUI 自己怎么说。
    gate_capabilities: bool = True
    #: Whether to overlay, on the resolved video capabilities, the capability overrides of user-added models under a built-in
    #: provider (docs/adr/0104). True only for model rows of built-in providers; custom provider overrides are applied by CustomVideoBackend itself, and inline definitions have no model row.
    #: 解析出的视频能力上要不要叠加内置供应商下用户添加的模型的能力覆盖（docs/adr/0104）。
    #: 只有内置供应商的模型行为真；自定义供应商的覆盖由 CustomVideoBackend 自己叠加，内联定义没有模型行。
    user_added_overrides: bool = False


def _as_float(value: object) -> float | None:
    """Narrow fields read from disk to their declared types: bad values raise and ``get`` routes them into the controlled failure path (read endpoint 404, not 500).

    读盘字段按声明类型收口：坏值抛出后由 ``get`` 归入受控失败路径（读接口 404，不是 500）。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"不是数值：{value!r}")
    return float(value)


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"不是整数：{value!r}")
    return int(value)


def _as_str(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"不是字符串：{value!r}")


@dataclass
class TrialRun:
    """Full result body of one test connection. While running only the first few fields have values. / 一次测试连接的完整结果体。运行中只有前几个字段有值。"""

    id: str
    status: TrialRunStatus
    provider: str
    model: str
    created_at: float
    #: Whether this job produces video or an image. The read side uses it to decide whether to show the artifact in a player or as an image.
    #: 这一笔产的是视频还是图像。读侧据此决定产物按播放器还是按图展示。
    media_type: str = "video"
    finished_at: float | None = None
    api_call_id: int | None = None
    #: The id the provider gave this call (ComfyUI's ``prompt_id``, a declarative endpoint's ``task_id``).
    #: 供应商给这笔调用的 id（ComfyUI 的 ``prompt_id``、声明式端点的 ``task_id``）。
    provider_job_id: str | None = None
    #: Rendered and redacted submit request; None when there is no definition to render (Python-implemented endpoints, ComfyUI endpoints).
    #: 渲染并脱敏后的提交请求；无定义可渲（Python 实现的端点、ComfyUI 端点）为 None。
    request: dict[str, Any] | None = None
    submit_response: object | None = None
    poll_responses: list[object] = field(default_factory=list)
    result_response: object | None = None
    #: Per-stage extraction, keys from :data:`check.STAGES`; empty when there is no definition to read (Python-implemented endpoints).
    #: 逐阶段提取，键取自 :data:`check.STAGES`；无定义可读（Python 实现的端点）为空。
    extractions: dict[str, Any] = field(default_factory=dict)
    video_url: str | None = None
    duration_seconds: int | None = None
    error: str | None = None
    has_artifact: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "provider": self.provider,
            "model": self.model,
            "media_type": self.media_type,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "api_call_id": self.api_call_id,
            "provider_job_id": self.provider_job_id,
            "stages": self.stage_states(),
            "request": self.request,
            "submit_response": self.submit_response,
            "poll_responses": self.poll_responses,
            "result_response": self.result_response,
            "extractions": self.extractions,
            "video_url": self.video_url,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "has_artifact": self.has_artifact,
        }

    def stage_states(self) -> dict[str, str]:
        """The four stage points: stages reached are ``done``; stages not reached are ``skipped`` in a terminal state and ``pending`` while running.

        Don't guess "which stage the failure was in". When submission is rejected the trace already has a submit response,
        and marking "the first stage not reached" red would point the error at polling; which stage failed is told by the
        failure code in ``error`` itself.

        Computed on the fly by ``to_payload`` rather than stored as a field: everything the four stages rely on is in the same
        result body, a stored copy would only add one more place that could disagree with it, and old results read back from
        disk can be recomputed by the current rules.

        四段状态点：到达过的段 ``done``，没到达的段终态上是 ``skipped``、运行中是 ``pending``。

        不猜「失败落在哪一段」。提交被拒时留痕里已经有一条提交响应，按「第一个没到达的段」标红
        会把错指到轮询上；哪一段失败由 ``error`` 的失败码自己说。

        由 ``to_payload`` 现算而不是存字段：四段的依据全在同一个结果体上，存一份只会多出一处
        可能与它不一致的副本，读盘回来的老结果也能照当前口径重算。
        """
        reached = {
            "submit": self.submit_response is not None,
            "poll": bool(self.poll_responses),
            "result": self.result_response is not None,
            "artifact": self.has_artifact,
        }
        settled = self.status in (TrialRunStatus.SUCCEEDED, TrialRunStatus.FAILED)
        return {
            stage: "done" if reached[stage] else ("skipped" if settled else "pending") for stage in TRIAL_RUN_STAGES
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TrialRun:
        return cls(
            id=str(payload["id"]),
            status=TrialRunStatus(payload["status"]),
            provider=str(payload.get("provider", "")),
            model=str(payload.get("model", "")),
            media_type=str(payload.get("media_type") or "video"),
            created_at=_as_float(payload.get("created_at")) or 0.0,
            finished_at=_as_float(payload.get("finished_at")),
            api_call_id=_as_int(payload.get("api_call_id")),
            provider_job_id=_as_str(payload.get("provider_job_id")),
            request=payload.get("request"),
            submit_response=payload.get("submit_response"),
            poll_responses=list(payload.get("poll_responses") or []),
            result_response=payload.get("result_response"),
            extractions=dict(payload.get("extractions") or {}),
            video_url=_as_str(payload.get("video_url")),
            duration_seconds=_as_int(payload.get("duration_seconds")),
            error=_as_str(payload.get("error")),
            has_artifact=bool(payload.get("has_artifact")),
        )


class _ResponseCapture:
    """Collect provider responses by the stage the runtime marks, each capped at the 64 KiB limit of the ledger's diagnostic column.

    按运行时标记的阶段收供应商响应，每条按账本诊断列的 64 KiB 上限收口。"""

    def __init__(self) -> None:
        self.submit: object | None = None
        self.polls: deque[object] = deque(maxlen=MAX_POLL_RESPONSES)
        self.result: object | None = None

    def add(self, stage: ProviderResponseStage, body: object) -> None:
        bounded = bound_provider_response(body)
        if stage == "submit":
            self.submit = bounded
        elif stage == "poll":
            self.polls.append(bounded)
        else:
            self.result = bounded


class TrialRunManager:
    """In-process registry of test connections: concurrency gate, background tasks, terminal disk writes and TTL cleanup.

    进程内测试连接的登记处：并发闸、后台任务、终态写盘与 TTL 清理。"""

    def __init__(
        self,
        *,
        root: Path | None = None,
        ledger: Ledger | None = None,
        read_poll_timeout: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        self._root = root
        self._ledger = ledger or Ledger()
        self._read_poll_timeout = read_poll_timeout
        self._runs: dict[str, TrialRun] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._owners: dict[str, str] = {}  # run_id -> user_id
        self._staging: dict[str, Path] = {}  # run_id -> temporary materials directory / run_id -> 素材临时目录
        self._response_writes: dict[str, set[asyncio.Task[None]]] = {}  # run_id -> in-flight diagnostic writes / run_id -> 在途诊断写入
        # runs that already have an outcome and are wrapping up; cancellation no longer interrupts them
        self._settling: set[str] = set()  # 已拿到结局、正在收尾的 run，取消不再打断它们

    @property
    def root(self) -> Path:
        root = self._root or DataRootLayout.current().trial_runs_dir
        root.mkdir(parents=True, exist_ok=True)
        return root

    def purge_expired(self, *, now: float | None = None) -> int:
        """Remove result directories past their TTL and return the number removed. Scanned once at process start and on every new run.

        清掉过了 TTL 的结果目录，返回清理条数。进程启动与每次新建各扫一遍。"""
        return self._purge_expired(frozenset(self._runs), now if now is not None else time.time())

    def _purge_expired(self, active: Collection[str], moment: float) -> int:
        """The scanning body of ``purge_expired``. The caller snapshots running runs on the event loop and passes them in, for calling in a thread.

        ``purge_expired`` 的扫描本体。在跑的 run 由调用方在事件循环上取快照传入，供线程里调用。"""
        removed = 0
        for entry in self.root.iterdir():
            if not entry.is_dir() or entry.name in active:
                continue
            try:
                if not _expired(entry, moment):
                    continue
                _remove_tree(entry)
            except OSError:
                logger.warning("测试连接结果目录清理失败：%s", entry.name, exc_info=True)
                continue
            removed += 1
        return removed

    def _expired(self, run_id: str) -> bool:
        """Whether a result is past its TTL. Cleanup and reading share this one check.

        Cleanup only scans at process start and on each new run, and the read endpoint judges on its own: on a server with no
        new runs for a long time, "results unreadable after 24 hours" cannot depend on whether someone happens to run another
        test connection.

        结果是否过了 TTL。清理与读取共用这一条判定。

        清理只在进程启动与每次新建时扫，读接口自己判：一台长时间没有新 run 的服务器上，
        「24 小时后结果不可读」不能取决于恰好有没有人再发一次测试连接。
        """
        try:
            return _expired(self.root / run_id, time.time())
        except OSError:
            return False

    async def start(
        self,
        target: TrialRunTarget,
        parameters: EndpointTestParameters,
        *,
        user_id: str = DEFAULT_USER_ID,
        assets: Mapping[str, Path | list[Path] | None] | None = None,
        staging: Path | None = None,
        request_preview: dict[str, Any] | None = None,
    ) -> TrialRun:
        """Dispatch one test connection. Rejected when the same user already has a run in progress.

        Raises:
            TrialRunBusyError: this user already has a test connection running.

        派发一次测试连接。同一用户已有在跑的 run 时拒绝。

        Raises:
            TrialRunBusyError: 该用户已有一个测试连接在跑。
        """
        if user_id in self._owners.values():
            raise TrialRunBusyError(user_id)
        run_id = uuid.uuid4().hex
        run = TrialRun(
            id=run_id,
            status=TrialRunStatus.QUEUED,
            provider=target.provider,
            model=target.model,
            media_type=target.media_type,
            created_at=time.time(),
            request=request_preview,
        )
        self._runs[run_id] = run
        self._owners[run_id] = user_id
        if staging is not None:
            self._staging[run_id] = staging
        # Claim the slot and materials directory first, then create directories: if directory creation fails (disk full,
        # read-only mount) the claim is not returned, later tests are rejected as busy forever, and cancel 404s because the task
        # is not registered yet -- the same deadlock as on the settlement failure path. The claim must also happen before the
        # first await: the concurrency gate relies on there being no yield point between "check busy" and "claim".
        # 名额与素材目录先占后建目录：建目录失败（盘满、只读挂载）时占位不还，后续测试永远
        # 被拒成 busy，而取消又因 task 尚未登记而 404——与结算失败那条路同一种死锁。
        # 占位也必须在第一次 await 之前：并发闸靠的是「查 busy」与「占位」之间没有让出点。
        try:
            run_dir = await run_sync_transaction(self._prepare_run_dir, run_id, frozenset(self._runs))
            task = asyncio.create_task(self._execute(run, run_dir, target, parameters, assets or {}))
        except BaseException:
            await run_sync_transaction(self._discard, run_id, self._release(run_id))
            raise
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))
        return run

    def get(self, run_id: str) -> TrialRun | None:
        """Read a run: from memory while running, from disk in a terminal state. Runs interrupted by cancel or restart have no result file.

        读一次 run：运行中读内存，终态读盘。取消或重启中断的 run 没有结果文件。"""
        if run_id in self._runs:
            return self._runs[run_id]
        # run_id is a path segment passed in by the caller; validate it against the generated format before reading disk, to keep ".." (and backslashes on Windows) from escaping root.
        # run_id 是调用方传入的路径段，读盘前先按生成格式校验，防 ".."（Windows 上还有反斜杠）越出 root。
        if _RUN_ID_PATTERN.fullmatch(run_id) is None:
            return None
        if self._expired(run_id):
            return None
        path = self.root / run_id / _RESULT_FILE
        try:
            # Deserialisation and disk reads belong to the same controlled failure path: anything that cannot yield a usable run is None.
            # 反序列化与读盘同属一条受控失败路径：读不出一个可用的 run 一律 None。
            return TrialRun.from_payload(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, KeyError, TypeError, ValueError):
            return None

    async def wait(self, run_id: str) -> TrialRun | None:
        """Wait for a run to stop and return its terminal state. Runs already terminal or nonexistent return immediately.

        The product path relies on ``GET`` polling, not this; it serves cases that "must wait for this job to finish" --
        draining before process shutdown, and tests.

        等一次 run 停下并返回它的终态。已经终态或不存在的 run 立即返回。

        产品路径靠 ``GET`` 轮询，不靠这里；它服务的是「必须等这一笔跑完」的场合——进程关停前
        排空、以及测试。
        """
        task = self._tasks.get(run_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return await asyncio.to_thread(self.get, run_id)

    def artifact_path(self, run_id: str) -> Path | None:
        """The artifact saved by this run; each kind has its own filename, so the read endpoint need not read the result body first to know which to look for.

        这次 run 落盘的产物；两类各有自己的文件名，读接口不必先读一遍结果体才知道找哪个。"""
        if _RUN_ID_PATTERN.fullmatch(run_id) is None:
            return None
        if self._expired(run_id):
            return None
        for name in _ARTIFACT_FILE_BY_MEDIA_TYPE.values():
            path = self.root / run_id / name
            if path.is_file():
                return path
        return None

    async def cancel(self, run_id: str) -> bool:
        """Stop local polling and settle as cancelled.

        Whether the remote side can be stopped is not this layer's concern: the backend's generation path handles the cancel
        itself (ComfyUI stops once by ``prompt_id``), while for channels that cannot be stopped the remote task keeps running
        and money keeps being spent, so settlement always goes the cancel way.

        A run that already has an outcome (provider returned success, or failure already decided) rejects a late cancel and
        returns False: the ledger is already settled by the outcome, and deleting the directory now would only lose an
        already billed result.

        停本地轮询并按取消结算。

        远端叫不叫得停不在这一层：backend 的生成路径自己接住这次取消（ComfyUI 据 ``prompt_id``
        叫停一次），而停不下来的通道远端任务照跑、钱照花，故结算一律按取消走。

        已拿到结局（供应商成功返回，或已判定失败）的 run 拒绝晚到的取消、返回 False：账本已按
        结局结算，此时再删目录只会丢掉一笔已经计费的结果。
        """
        task = self._tasks.get(run_id)
        run = self._runs.get(run_id)
        if task is None or run is None or run_id in self._settling:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # The backend's artifact save may swallow the cancel and return successfully; recheck after the task ends, and never delete an already settled result.
        # backend 的产物落盘可能吞掉取消并成功返回；等任务结束后重新核验，不能再删掉已结算的结果。
        if run.status in (TrialRunStatus.SUCCEEDED, TrialRunStatus.FAILED):
            return False
        # A shield-protected diagnostic write may still be running after the task stops: wait for it before settling, otherwise
        # resume_cancelled would write the same row concurrently, and the shutdown path might close the DB before the write finishes.
        # 被 shield 保护的诊断写入在任务停下后可能还在跑：结算前等它写完，否则 resume_cancelled
        # 会与它并发写同一行，关停路径还可能在写入未完时就关库。
        for write in list(self._response_writes.get(run_id, ())):
            with contextlib.suppress(Exception):
                await write
        # The accounting bracket itself settles a cancel as cancelled; this is the fallback when it didn't (write failure, loop
        # already shut down): ``resume_cancelled`` has a ``status='pending'`` guard, so settled rows are not rewritten. A
        # pending row must never be left for a call the user already abandoned.
        # Even if settlement raises, the slot and on-disk directories must be returned -- the task has already stopped, a
        # leftover claim would reject later tests as busy forever, and retrying the cancel would 404 because the task was
        # already removed; the deadlock could only be broken by a restart.
        # 记账括号自己会把取消结算成 cancelled，这里是它没结算成（写入失败、循环已关停）时的
        # 兜底：``resume_cancelled`` 带 ``status='pending'`` 守卫，已结算的行不会被改写。pending 行
        # 绝不能留给一笔用户已经放弃的调用。
        # 结算抛错也要把名额与盘上目录还掉——任务已经停了，占位留下去会把后续测试永远拒成 busy，
        # 而重试取消又因 task 已移除而 404，死锁只能重启解。
        try:
            if run.api_call_id is not None:
                await self._ledger.resume_cancelled(call_id=run.api_call_id)
        finally:
            await run_sync_transaction(self._discard, run_id, self._release(run_id))
        return True

    async def _execute(
        self,
        run: TrialRun,
        run_dir: Path,
        target: TrialRunTarget,
        parameters: EndpointTestParameters,
        assets: Mapping[str, Path | list[Path] | None],
    ) -> None:
        capture = _ResponseCapture()
        run.status = TrialRunStatus.RUNNING
        try:
            poll_timeout = await self._poll_timeout()
            # Assemble the backend first, then open the accounting: if assembly fails (model row missing, provider misconfigured)
            # not a single byte was sent, and recording a pending entry and flipping it to failed would add a call to the ledger that was never made.
            # backend 先装配再开账：装配失败（模型行不存在、供应商配错）时一个字节都没发出去，
            # 记一笔 pending 再翻成 failed 会让账本上多一条根本没打过的调用。
            backend = await target.build_backend()
            is_video = target.media_type == "video"
            # The capability gate and aspect ratio override share one set of capabilities resolved by this request's tier, from the
            # same source as production: endpoints declaring ``video_capabilities_for_tier`` narrow by resolution, and reading the
            # un-narrowed properties would be more permissive than production. Test requests carry no ``service_tier``, so the
            # request default tier is what is sent, and resolution also takes the default tier.
            # 能力闸与比例覆盖共用一份按本次请求档位解析的能力，与生产同源：声明了
            # ``video_capabilities_for_tier`` 的端点按分辨率收窄，读未收窄的属性会比生产放得更宽。
            # 测试请求不带 ``service_tier``，下发的就是请求缺省档，解析也取缺省档。
            video_caps = resolve_video_capabilities(backend, resolution=parameters.resolution) if is_video else None
            # Same rule as the production execution gate: the overlaid capabilities both pass the gate and are carried to the backend with the request.
            # 与生产执行闸门同一口径：叠加后的能力既过闸，也随请求带给后端。
            if video_caps is not None and target.user_added_overrides:
                video_caps = apply_user_added_video_overrides(video_caps, target.provider, target.model)
            # The production path runs the same capability gate before the accounting bracket (declared violations are rejected
            # before paying, not sent to the provider); test connection runs the production path, so the gate is the same.
            # Targets exempt from the gate: see ``TrialRunTarget.gate_capabilities``.
            # 生产路径在记账括号前跑同一道能力闸（声明的违约在付费前拒绝，不发给供应商）；
            # 测试连接跑的是生产那条路，闸也一致。免闸的目标见 ``TrialRunTarget.gate_capabilities``。
            if target.gate_capabilities:
                if video_caps is None:
                    _gate_trial_image_request(backend, target, assets)
                else:
                    await _gate_trial_video_request(video_caps, target, parameters, assets)
            # Endpoints declaring first_frame_ratio_adaptive_only only accept adaptive on requests with a first frame; the sent value
            # and the accounted value are separate, and the ledger still records the aspect ratio the user entered as intent (same division as production).
            # 声明 first_frame_ratio_adaptive_only 的端点在带首帧的请求上只接受 adaptive；
            # 下发值与记账值分离，账本记的仍是用户填的比例意图（与生产同一分工）。
            request_aspect_ratio = resolve_first_frame_aspect_ratio(
                caps=video_caps,
                aspect_ratio=parameters.aspect_ratio,
                has_first_frame=_single(assets.get("start_image")) is not None,
            )
            async with self._ledger.record(
                project_name="",
                call_type=CALL_TYPE_VIDEO if is_video else CALL_TYPE_IMAGE,
                model=target.model,
                provider=target.provider,
                prompt=parameters.prompt,
                resolution=parameters.resolution,
                # Duration and final-video audio track belong to the video dimension; an image job records them as "none": recording 5
                # seconds would show a nonexistent duration on the usage page, and the image request shape has no audio track dimension
                # at all (``ImageGenerationRequest`` doesn't carry it), so copying the video switch from the form would only make this row claim it produced sound.
                # 时长与成片音轨是视频这一维的事，图像这一笔按「没有」记：记一个 5 秒会让用量页上
                # 出现一个不存在的时长，而图像请求的形状里根本没有音轨这一维（``ImageGenerationRequest``
                # 不带它），照抄表单上那个视频开关只会让这一行说自己出了声。
                duration_seconds=parameters.duration_seconds if is_video else None,
                aspect_ratio=parameters.aspect_ratio,
                generate_audio=parameters.generate_audio if is_video else False,
                purpose=CallPurpose.ENDPOINT_TRIAL,
            ) as call:
                run.api_call_id = call.call_id
                on_response = self._response_writer(run, capture, call.call_id)
                request = (
                    self._video_request(
                        run_dir,
                        parameters,
                        assets,
                        poll_timeout,
                        aspect_ratio=request_aspect_ratio,
                        video_capabilities=video_caps,
                        on_provider_response=on_response,
                    )
                    if is_video
                    else self._image_request(
                        run_dir, parameters, assets, aspect_ratio=request_aspect_ratio, on_provider_response=on_response
                    )
                )
                result = await backend.generate(request)
                # The provider has returned success: from here on cancel no longer interrupts this run, and it wraps up as a success (see ``cancel``).
                # 供应商已成功返回：从这里起取消不再打断这次 run，结局按成功收尾（见 ``cancel``）。
                self._settling.add(run.id)
                call.success(result)
                run.provider_job_id = getattr(result, "task_id", None)
                # The artifact's address at the provider: the two result kinds call it by different names, landing in the same cell.
                # 产物在供应商那儿的地址：两类结果各叫一个名字，落进同一格。
                run.video_url = getattr(result, "video_uri", None) or getattr(result, "image_uri", None)
                run.duration_seconds = getattr(result, "duration_seconds", None)
                run.has_artifact = await run_noninterruptible_sync(_artifact_file(run_dir, target.media_type).is_file)
            await run_noninterruptible_async(self._finish(run, run_dir, target, capture, TrialRunStatus.SUCCEEDED))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The failure carriers of the two runtimes and the two capability gates have the same shape (stable code + serialisable
            # params); encoded into a failure code, the read side localises them by the same path; other exceptions have no stable code to encode and are kept as strings.
            # 两种运行时与两道能力闸的失败载体同形（稳定 code + 可序列化 params），编码成失败码后
            # 读侧按同一条路径本地化；其余异常没有稳定码可编，原样留字符串。
            structured = isinstance(
                exc, DeclarativeRuntimeError | ComfyuiError | VideoCapabilityError | ImageCapabilityError
            )
            run.error = encode_failure(exc.code, **exc.params) if structured else str(exc)
            self._settling.add(run.id)
            await run_noninterruptible_async(self._finish(run, run_dir, target, capture, TrialRunStatus.FAILED))

    def _response_writer(
        self, run: TrialRun, capture: _ResponseCapture, call_id: int
    ) -> Callable[[ProviderResponseStage, object], Awaitable[None]]:
        """Diagnostic callback for provider responses: merged into the result body and written into the ledger's diagnostic column. Shared by both request shapes.

        供应商响应的诊断回调：并进结果体并写进账本的诊断列。两种请求形状共用这一个。"""

        async def on_provider_response(stage: ProviderResponseStage, body: object) -> None:
            capture.add(stage, body)
            # Merge into the result body as responses arrive: the four stage points read these three fields; merging only once at
            # the terminal state would make a GET while running always see four empty points.
            # 边收边并进结果体：四段状态点读的是这三个字段，只在终态并一次的话，运行中的 GET
            # 看到的永远是四个空点。
            _merge_capture(run, capture)
            # The user may cancel at any time, and the cancel may land right in the middle of this write. A diagnostic trace write
            # cut off midway would leave a half-open transaction, and the subsequent failure settlement would have to run on a broken
            # connection -- shield lets this write finish on its own, while the cancel still propagates to the layer that called it.
            # The write task is registered under the run: shield only protects the coroutine from cancellation; it keeps running
            # after the outer task stops, and the cancelling side must wait for it before settling (see ``cancel``).
            # 用户随时可能取消，而取消可能正落在这次写入中间。诊断留痕的写入被拦腰截断会留下
            # 一个半开的事务，随后的失败结算就得在一条坏掉的连接上做——shield 让这次写入自己跑完，
            # 取消照常传给调用它的那一层。写入任务登记在 run 名下：shield 只保护协程不被取消，
            # 外层任务停下后它还在跑，取消方结算前要等它（见 ``cancel``）。
            write = asyncio.ensure_future(self._ledger.record_provider_response(call_id=call_id, body=body))
            writes = self._response_writes.setdefault(run.id, set())
            writes.add(write)
            write.add_done_callback(writes.discard)
            await asyncio.shield(write)

        return on_provider_response

    def _video_request(
        self,
        run_dir: Path,
        parameters: EndpointTestParameters,
        assets: Mapping[str, Path | list[Path] | None],
        poll_timeout: int,
        *,
        aspect_ratio: str,
        video_capabilities: VideoCapabilities | None,
        on_provider_response: Callable[[ProviderResponseStage, object], Awaitable[None]],
    ) -> VideoGenerationRequest:
        reference_images = assets.get("reference_images")
        reference_audio = assets.get("reference_audio_files")
        return VideoGenerationRequest(
            prompt=parameters.prompt,
            output_path=_artifact_file(run_dir, "video"),
            aspect_ratio=aspect_ratio,
            duration_seconds=parameters.duration_seconds,
            resolution=parameters.resolution,
            start_image=_single(assets.get("start_image")),
            end_image=_single(assets.get("end_image")),
            reference_images=list(reference_images) if isinstance(reference_images, list) else None,
            reference_audio_files=list(reference_audio) if isinstance(reference_audio, list) else None,
            generate_audio=parameters.generate_audio,
            poll_timeout_seconds=poll_timeout,
            on_provider_response=on_provider_response,
            video_capabilities=video_capabilities,
        )

    def _image_request(
        self,
        run_dir: Path,
        parameters: EndpointTestParameters,
        assets: Mapping[str, Path | list[Path] | None],
        *,
        aspect_ratio: str,
        on_provider_response: Callable[[ProviderResponseStage, object], Awaitable[None]],
    ) -> ImageGenerationRequest:
        """The request for an image endpoint job.

        First/end frames and reference audio don't fit this shape: image endpoints have no such semantic keys (see
        ``comfyui.bindings``), and the UI doesn't render these cells for them. The polling limit is not in the request either
        -- the image lane uses its own fixed value (``comfyui_image_backend.IMAGE_POLL_TIMEOUT_SECONDS``) and does not read the
        video setting.

        图像端点这一笔的请求。

        首尾帧与参考音频落不进这个形状：图像端点没有这几个语义键（见 ``comfyui.bindings``），
        界面也不为它渲这几个格子。轮询上限同样不在请求里——图像通道用自己的定值
        （``comfyui_image_backend.IMAGE_POLL_TIMEOUT_SECONDS``），不读视频那个设置项。
        """
        reference_images = assets.get("reference_images")
        return ImageGenerationRequest(
            prompt=parameters.prompt,
            output_path=_artifact_file(run_dir, "image"),
            reference_images=[
                ReferenceImage(path=str(path))
                for path in (reference_images if isinstance(reference_images, list) else [])
            ],
            aspect_ratio=aspect_ratio,
            image_size=parameters.resolution,
            on_provider_response=on_provider_response,
        )

    async def _poll_timeout(self) -> int:
        if self._read_poll_timeout is not None:
            return await self._read_poll_timeout()
        from lib.config.service import read_video_poll_timeout_seconds

        return await read_video_poll_timeout_seconds()

    async def _finish(
        self,
        run: TrialRun,
        run_dir: Path,
        target: TrialRunTarget,
        capture: _ResponseCapture,
        status: TrialRunStatus,
    ) -> None:
        """Mark terminal, write to disk, release the slot. The caller wraps the whole thing in a non-interruptible section with
        ``run_noninterruptible_async``: a cancel landing in the middle would leave a run "terminal in memory, no result on disk".

        终态标记、写盘、让出名额。调用方用 ``run_noninterruptible_async`` 把它整段收进不可中断区间：
        取消落在中间会留下「内存里已终态、盘上没有结果」的 run。"""
        run.status = status
        run.finished_at = time.time()
        _merge_capture(run, capture)
        if run.provider_job_id is None:
            run.provider_job_id = _submitted_prompt_id(capture.submit)
        run.extractions = _stage_reports(target.definition, capture)
        payload = json.dumps(run.to_payload(), ensure_ascii=False, default=str)
        # Write to disk before releasing the in-memory run: the read endpoint reads disk only when the run is not in memory; the reverse order would leave a window reading as 404.
        # 先写盘再让出内存里的 run：读接口在内存里查不到才读盘，倒过来会有一段读成 404。
        await run_sync_transaction(_settle_files, run_dir / _RESULT_FILE, payload, self._staging.get(run.id))
        self._release(run.id)

    async def shutdown(self) -> None:
        """Process shutdown: stop all running runs and settle them as cancelled.

        When tasks die with the event loop no process comes back to fix the ledger, so the shutdown side goes the cancel path:
        first stop tasks (the accounting bracket settles cancelled accordingly), then as a fallback flip pending rows that did
        not get settled.

        A single run's settlement failure (e.g. the DB is already unavailable) does not interrupt this drain: an exception
        propagating out would leave all later runs unstopped, and closing HTTP clients and the DB, which come after this step
        in shutdown, would be skipped too.

        Runs already wrapping up reject the cancel, and shutdown waits for them to write results: once the DB is closed, the
        accounting writes in the wrap-up phase would have nowhere to land.

        进程关停：停掉所有在跑的 run 并按取消结算。

        任务随事件循环消亡时没有进程会回来翻账，故由关停方走取消那条路径：先停任务（记账括号
        据此结算 cancelled），再兜底翻掉没结算成的 pending 行。

        单条 run 结算失败（如库已不可用）不中断这趟排空：异常穿透出去会让后面的 run 一条都
        不停，关停流程里排在本步之后的关 HTTP 客户端与关库也一起跳过。

        已在收尾的 run 拒绝取消，关停等它写完结果：之后关库，收尾段里的记账写入就没处落。
        """
        for run_id in list(self._tasks):
            task = self._tasks.get(run_id)
            try:
                if not await self.cancel(run_id) and task is not None:
                    # ``asyncio.wait`` does not raise the tasks' own exceptions to the shutdown side; if the shutdown side itself is cancelled, that propagates as usual.
                    # ``asyncio.wait`` 不把任务自己的异常抛给关停方，关停方自己被取消时照常传出去。
                    await asyncio.wait([task])
            except Exception:
                logger.warning("关停时结算测试连接失败：%s", run_id, exc_info=True)

    def _release(self, run_id: str) -> Path | None:
        """Release the user's concurrency slot and hand back the temporary materials directory for the caller to delete -- materials are discarded after use, not left on disk.

        让出用户的并发名额，交回素材临时目录由调用方删——素材用完即丢，不留在盘上。"""
        self._runs.pop(run_id, None)
        self._owners.pop(run_id, None)
        self._response_writes.pop(run_id, None)
        self._settling.discard(run_id)
        return self._staging.pop(run_id, None)

    def _prepare_run_dir(self, run_id: str, active: Collection[str]) -> Path:
        """Scan expired directories before creating, then create this run's result directory. Runs in a thread.

        新建前扫一遍过期目录，再建这次 run 的结果目录。线程里跑。"""
        self._purge_expired(active, time.time())
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _discard(self, run_id: str, staging: Path | None) -> None:
        """Delete the temporary materials directory and result directory on disk of a run that did not reach a terminal state. Runs in a thread.

        删掉一次没跑到终态的 run 在盘上的素材临时目录与结果目录。线程里跑。"""
        if staging is not None:
            _remove_tree(staging)
        _remove_tree(self.root / run_id)


def _artifact_file(run_dir: Path, media_type: str) -> Path:
    return run_dir / _ARTIFACT_FILE_BY_MEDIA_TYPE[media_type]


def _settle_files(result_file: Path, payload: str, staging: Path | None) -> None:
    """Terminal disk write: write the result body and delete the temporary materials directory. Runs in a thread.

    终态落盘：写结果体并删掉素材临时目录。线程里跑。"""
    try:
        result_file.write_text(payload, encoding="utf-8")
    except OSError:
        logger.warning("测试连接结果写盘失败 run_id=%s", result_file.parent.name, exc_info=True)
    if staging is not None:
        _remove_tree(staging)


async def _gate_trial_video_request(
    caps: VideoCapabilities,
    target: TrialRunTarget,
    parameters: EndpointTestParameters,
    assets: Mapping[str, Path | list[Path] | None],
) -> None:
    """Run the production video capability gate before submitting; ``caps`` are the capabilities resolved by this request's tier.

    提交前跑生产那道视频能力闸，``caps`` 是按本次请求档位解析好的能力。"""
    from lib.backends.video_frame_slots import gate_video_request, plan_frame_slots
    from lib.speech.audio_utils import probe_reference_audio_total_seconds

    reference_images = assets.get("reference_images")
    reference_audio = assets.get("reference_audio_files")
    audio_files = list(reference_audio) if isinstance(reference_audio, list) else None
    # Same probe as the production path: only probe when the capabilities declare a total duration constraint; if it cannot
    # be probed (bundled ffmpeg unavailable) pass None, and the gate skips that item as unknown rather than rejecting.
    # 与生产路径同一探测：只在能力声明了总时长约束时探；探不出（随包 ffmpeg 不可用）传 None，
    # 闸按未知跳过该项而非拒绝。
    total_seconds = (
        await probe_reference_audio_total_seconds(audio_files)
        if audio_files and caps.max_reference_audio_total_seconds is not None
        else None
    )
    images = list(reference_images) if isinstance(reference_images, list) else None
    end_image = _single(assets.get("end_image"))
    # has_image takes the whole slot plan, same source as production: endpoints driven by reference images (e.g. S2V) have
    # no first frame but carry reference images, and looking only at the first frame would judge every valid request as pure
    # text-to-video and reject it by text_to_video=false.
    # has_image 取整份槽位计划，与生产同源：参考图驱动的端点（如 S2V）没有首帧但带参考图，
    # 只看首帧会把每一次合法请求都判成纯文生、按 text_to_video=false 拒掉。
    slot_plan = plan_frame_slots(
        start_image=_single(assets.get("start_image")),
        end_image=end_image,
        reference_images=images,
    )
    gate_video_request(
        caps=caps,
        provider=target.provider,
        model=target.model,
        prompt=parameters.prompt,
        has_image=bool(slot_plan.specs),
        has_first_frame=slot_plan.start_index is not None,
        end_image=end_image,
        reference_images=images,
        reference_audio_files=audio_files,
        reference_audio_total_seconds=total_seconds,
    )


def _gate_trial_image_request(
    backend: Any,
    target: TrialRunTarget,
    assets: Mapping[str, Path | list[Path] | None],
) -> None:
    """Capability gate for the image job: same criterion as the one ``MediaGenerator`` runs before generating images.

    The image lane has no counterpart to ``gate_video_request``: there is no optional input path besides text-to-image /
    image-to-image, so it judges whether "does this request carry reference images" matches the capabilities the backend
    declares. Image backends have no ``video_capabilities``, and the video gate rejects every request with inputs as
    unsupported when caps is ``None`` -- if image requests went through that gate, every test connection with reference
    images would be judged ``video_reference_images_unsupported``.

    图像这一笔的能力闸：判据与 ``MediaGenerator`` 出图前那道同一条。

    图像通道没有 ``gate_video_request`` 的对应物：文生图 / 图生图之外没有别的可选输入路径，判的
    就是「这次带不带参考图」对不对得上 backend 声明的能力。图像 backend 没有 ``video_capabilities``，
    而视频那道闸在 caps 为 ``None`` 时把带输入的请求一律按不支持拒掉——图像请求走那道闸，每一次
    带参考图的测试连接都会被判成 ``video_reference_images_unsupported``。
    """
    reference_images = assets.get("reference_images")
    has_references = bool(reference_images) if isinstance(reference_images, list) else False
    needed = ImageCapability.IMAGE_TO_IMAGE if has_references else ImageCapability.TEXT_TO_IMAGE
    if needed not in backend.capabilities:
        raise ImageCapabilityError(
            "image_capability_missing_i2i" if has_references else "image_capability_missing_t2i",
            provider=target.provider,
            model=target.model,
        )


def declarative_target(
    definition: Mapping[str, Any],
    credentials: EndpointTestCredentials,
    parameters: EndpointTestParameters,
    *,
    provider: str | None = None,
) -> TrialRunTarget:
    """Target for an inline definition: build a declarative video or image backend directly by the media type the definition declares.

    ``provider`` defaults to the host of ``base_url`` -- inline credentials have no provider identity, and the ledger must
    record a value that lets the user later recognise where this money was spent. A definition with absolute addresses
    hard-coded in all three sections has no base_url, so fall back to the host of the submit address: that is exactly who
    this money is actually paid to.

    内联定义的目标：按定义声明的媒体类型直接构造声明式视频或图片 backend。

    ``provider`` 缺省取 ``base_url`` 的 host——内联凭证没有供应商身份，而账本必须落一个能让用户
    日后认出这笔钱花在哪里的值。三节 URL 都写死绝对地址的定义没有 base_url 可取，退而取提交
    地址的 host：那正是这笔钱实际打给谁。
    """
    label = provider or provider_from_base_url(credentials.base_url) or _definition_host(definition)
    media_type = definition_media_type(definition)
    backend_class = DeclarativeImageBackend if media_type == "image" else DeclarativeVideoBackend

    async def build() -> DeclarativeVideoBackend | DeclarativeImageBackend:
        return backend_class(
            api_key=credentials.api_key,
            base_url=credentials.base_url,
            model=parameters.model,
            definition=definition,
            provider=label,
        )

    return TrialRunTarget(
        provider=label,
        model=parameters.model,
        build_backend=build,
        definition=definition,
        media_type=media_type,
    )


def model_ref_target(
    provider_id: str,
    model_id: str,
    *,
    resolver: ConfigResolver,
    definition: Mapping[str, Any] | None = None,
    media_type: str = "video",
) -> TrialRunTarget:
    """Target for a model row: assemble the backend through the production construction seam; built-in and custom providers share one entry point.

    ``definition`` is passed by the caller when the model row resolves to a declarative definition (a custom call endpoint
    or a built-in declarative endpoint), to give the rendered request and per-stage extraction in the result body; for
    Python-implemented endpoints both sections stay empty.

    ``media_type`` decides which side's registry assembly uses: the model row itself says whether it is an image row or a
    video row, and the assembly layer takes it accordingly.

    模型行的目标：经生产那道构造缝装配 backend，内置与自定义供应商同一入口。

    ``definition`` 在模型行解析出声明式定义（自定义调用端点或内置声明式端点）时由调用方带上，
    用来在结果体里给出渲染后的请求与逐阶段提取；Python 实现的端点两段留空。

    ``media_type`` 决定装配走哪一侧的注册表：模型行自己说它是图像行还是视频行，装配层照它取。
    """

    async def build() -> Any:
        from lib.backends.backend_assembly import assemble_backend

        return await assemble_backend(
            provider_id=provider_id, media_type=media_type, model_id=model_id, resolver=resolver
        )

    return TrialRunTarget(
        provider=provider_id,
        model=model_id,
        build_backend=build,
        definition=definition,
        media_type=media_type,
        user_added_overrides=not is_custom_provider(provider_id),
    )


def artifact_media_type(path: Path) -> str:
    """MIME of a saved artifact, determined from the file header.

    Not by extension: the artifact name is given by this layer according to ``media_type``, while the real container is
    decided by the provider's export settings, and switching within a family (mp4 / mov) doesn't change the name. Bytes
    that cannot be recognised get the generic binary type, leaving recognition to the read side -- declaring a wrong MIME
    would make the browser decode it as that type, which is harder to debug than declaring none.

    一份落盘产物的 MIME，按文件头判。

    不按扩展名：产物名是本层按 ``media_type`` 起的，而真实容器由供应商那一侧的导出设置决定，
    同族里换一个（mp4 / mov）名字不会跟着变。认不出的字节给通用二进制类型，让读侧自己去认——
    声明一个错的 MIME 会让浏览器按那个类型去解，比不声明更难排查。
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(CONTAINER_HEAD_BYTES)
    except OSError:
        return "application/octet-stream"
    return sniff_container(head) or "application/octet-stream"


def provider_from_base_url(base_url: str) -> str:
    return urlsplit(base_url).hostname or base_url


def _definition_host(definition: Mapping[str, Any]) -> str:
    return urlsplit(str(definition.get("submit", {}).get("url", ""))).hostname or ""


def _single(value: Path | list[Path] | None) -> Path | None:
    return value if isinstance(value, Path) else None


def _merge_capture(run: TrialRun, capture: _ResponseCapture) -> None:
    """Merge the provider responses received at this moment into the result body. / 把这一刻收到的供应商响应并进结果体。"""
    run.submit_response = capture.submit
    run.poll_responses = list(capture.polls)
    run.result_response = capture.result


def _submitted_prompt_id(submit: object) -> str | None:
    """When the failure happened after submission, recover the provider's id from the submit response.

    Only the ``prompt_id`` key is recognised (ComfyUI's submit response only has that): the id key of declarative
    endpoints varies by definition and is given by ``result.task_id`` on that side; guessing key names here would only
    take some unrelated field of the same name as the job id.

    失败在提交之后时，从提交响应里捡回供应商的 id。

    只认 ``prompt_id`` 这一个键（ComfyUI 的提交响应只有它）：声明式端点的 id 键随定义而变，
    那一侧由 ``result.task_id`` 给出，在这里猜键名只会把某个同名的无关字段当成 job id。
    """
    if isinstance(submit, Mapping):
        prompt_id = str(submit.get("prompt_id") or "").strip()
        return prompt_id or None
    return None


def _stage_reports(definition: Mapping[str, Any] | None, capture: _ResponseCapture) -> dict[str, Any]:
    """Generate per-section extraction reports by runtime stage. Return empty when there is no readable value path.

    The gate is the same as "check response": per-stage extraction is that mode's machinery; if this kind cannot answer
    "what do I read when the response comes back like this", there is nothing to report here either -- ComfyUI endpoint
    artifact extraction is fixed code, not a configurable path.

    按运行时阶段生成逐节提取报告。没有可读的取值路径时返回空。

    闸门与「验证响应」同一条：逐阶段提取就是那个模式的机器，这种 kind 答不上「响应这样回我时
    我读成了什么」，这里也就没有可报的——ComfyUI 端点的产物提取是固定代码，不是可配路径。
    """
    if definition is None or not supports_test_mode(str(definition.get("kind", "")), EndpointTestMode.CHECK_RESPONSE):
        return {}
    polls = list(capture.polls)
    bodies: dict[str, object] = {}
    if capture.submit is not None:
        bodies["submit"] = capture.submit
    if polls:
        bodies["poll"] = polls[-1]
    if capture.result is not None:
        bodies["result"] = capture.result
    reports: dict[str, Any] = {}
    for stage in STAGES:
        body = bodies.get(stage)
        if body is None:
            continue
        try:
            reports[stage] = stage_report_payload(check_response(definition, stage, body))
        except Exception:
            logger.debug("测试连接的 %s 节提取报告生成失败", stage, exc_info=True)
    return reports


def _expired(entry: Path, now: float) -> bool:
    return now - entry.stat().st_mtime > TRIAL_RUN_TTL_SECONDS


def _remove_tree(path: Path) -> None:
    # endpoint test trial run directory, under the data root rather than inside a project
    shutil.rmtree(path, ignore_errors=True)  # hard-delete: out-of-scope -- 端点测试的试跑目录，在数据根下而非项目内


_manager: TrialRunManager | None = None


def trial_run_manager() -> TrialRunManager:
    """The single in-process registry. On first use it scans expired directories (runs interrupted by restart have no result file and are removed too).

    进程内唯一的登记处。首次取用时扫一遍过期目录（重启中断的 run 没有结果文件，一并清掉）。"""
    global _manager
    if _manager is None:
        _manager = TrialRunManager()
        _manager.purge_expired()
    return _manager


async def shutdown_trial_runs() -> None:
    """Process shutdown entry point: if the registry was never used, don't create one just for shutdown. / 进程关停入口：登记处从未被取用时不为关停凭空建一台。"""
    if _manager is not None:
        await _manager.shutdown()
