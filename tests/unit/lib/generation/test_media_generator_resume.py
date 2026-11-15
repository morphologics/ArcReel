"""Unit tests for MediaGenerator.resume_video_async.

Concerns:
- the resume path writes no new pending row (opens no accounting bracket)
- the pending call row is looked up via api_calls.task_id, and ledger.resume_success / resume_failed flip pending precisely
- after a successful resume one paid version is saved; formal media decide current atomically via the staging callback,
  non-formal requests append and select the new version directly
- ResumeExpiredError propagates up the call chain and pending flips to failed

MediaGenerator.resume_video_async 单元测试。

关注点：
- resume 路径不落新 pending 行（不开记账括号）
- 待结算调用行按 api_calls.task_id 反查，ledger.resume_success / resume_failed 精准翻 pending
- resume 成功后保存一个付费版本；正式媒体经 staging callback 原子决定 current，
  非正式请求直接追加并选中新版本
- ResumeExpiredError 沿调用链上抛，pending 翻 failed
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from arcreel_market_core.video_backend_contract import ResumeExpiredError
from lib.generation.media_generator import MediaGenerator
from tests.fakes import FakeConfigResolver, select_formal_video, write_backend_output, write_generation_targets


class _FakeVideoResult:
    def __init__(
        self,
        *,
        seed: int | None = None,
        provenance: dict[str, Any] | None = None,
        warnings: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.video_uri = "video-uri-resume"
        self.usage_tokens = 0
        self.generate_audio = True
        self.duration_seconds = 8
        self.seed = seed
        self.provenance = provenance
        self.warnings = warnings


class _FakeVideoBackend:
    name = "fake-video"
    model = "video-model"

    def __init__(self, *, raises: Exception | None = None, result: _FakeVideoResult | None = None) -> None:
        self.calls: list[Any] = []
        self.raises = raises
        self._result = result

    async def generate(self, request):
        raise AssertionError("generate 不应被 resume 路径调用")

    async def resume_video(self, job_id, request):
        self.calls.append((job_id, request))
        if self.raises is not None:
            raise self.raises
        await write_backend_output(request.output_path, b"fake-resume-video")
        return self._result if self._result is not None else _FakeVideoResult()


class _FakeVersions:
    """Simulates VersionManager's ensure_current_tracked / add_version / get_current_version.

    模拟 VersionManager 的 ensure_current_tracked / add_version / get_current_version。"""

    def __init__(self, *, initial_version: int = 0) -> None:
        self._version = initial_version
        self.ensure_calls: list[dict[str, Any]] = []
        self.add_calls: list[dict[str, Any]] = []

    def ensure_current_tracked(self, **kwargs):
        self.ensure_calls.append(kwargs)
        if self._version <= 0:
            self._version = 1
            self.add_calls.append(kwargs)
            return self._version
        return None

    def add_version(self, **kwargs):
        self.add_calls.append(kwargs)
        self._version += 1
        return self._version

    def get_current_version(self, _resource_type, _resource_id):
        return self._version


class _ProbeVideoBackend(_FakeVideoBackend):
    """Records a snapshot of add_calls when resume_video fires, used to assert that add_version happens only after the download.

    resume_video 触发时记录当时 add_calls 的快照，用来断言 add_version 在下载之后才发生。"""

    def __init__(self, versions: _FakeVersions) -> None:
        super().__init__()
        self._versions = versions
        self.add_calls_at_resume: int | None = None

    async def resume_video(self, job_id, request):
        self.add_calls_at_resume = len(self._versions.add_calls)
        return await super().resume_video(job_id, request)


class _FakeLedgerCall:
    def __init__(self, call_id: int) -> None:
        self.call_id = call_id
        self.declared = False
        self.result: Any = None
        self.usage: Any = None

    def success(self, result: Any, *, usage: Any = None) -> None:
        self.declared = True
        self.result = result
        self.usage = usage


class _FakeLedger:
    """Fake accounting ledger: the resume path should only go through resume_success/resume_failed and open no accounting bracket.

    ``started`` captures brackets opened by mistake (should always be empty); ``resumed`` captures the resume backfill arguments (including the
    backend result object handed over), the new main seam. ``lookups`` captures the arguments of looking up call rows by task.

    记账账本假实现：resume 路径只该走 resume_success/resume_failed，不开记账括号。

    ``started`` 捕获误开的括号（应恒空）；``resumed`` 捕获 resume 补账入参（含递交的
    backend 结果对象）——新主缝。``lookups`` 捕获按任务反查调用行的入参。
    """

    def __init__(self, *, pending_call_id: int | None = 42) -> None:
        self.started: list[dict[str, Any]] = []
        self.resumed: list[dict[str, Any]] = []
        self.lookups: list[str] = []
        self._pending_call_id = pending_call_id
        self._n = 0
        self._resume_affected = 1

    async def pending_call_id_for_task(self, task_id: str) -> int | None:
        self.lookups.append(task_id)
        return self._pending_call_id

    @asynccontextmanager
    async def record(self, **kwargs):
        self._n += 1
        self.started.append(kwargs)
        call = _FakeLedgerCall(self._n)
        try:
            yield call
        except asyncio.CancelledError:
            raise
        except Exception:
            raise

    async def resume_success(
        self, *, call_id: int, result: Any, usage: Any = None, service_tier: str = "default"
    ) -> int:
        self.resumed.append(
            {"status": "success", "call_id": call_id, "result": result, "usage": usage, "service_tier": service_tier}
        )
        return self._resume_affected

    async def resume_failed(self, *, call_id: int, failure: BaseException | str | None = None) -> int:
        self.resumed.append({"status": "failed", "call_id": call_id, "failure": failure})
        return self._resume_affected


def _build_generator(tmp_path: Path, *, initial_version: int = 0) -> MediaGenerator:
    gen = object.__new__(MediaGenerator)
    gen.project_path = tmp_path / "projects" / "demo"
    gen.project_path.mkdir(parents=True, exist_ok=True)
    write_generation_targets(gen.project_path, segments=("E1S01",), video_units=("E1U1",))
    gen.project_name = "demo"
    gen._rate_limiter = None
    gen._image_backend = None
    gen._video_backend = _FakeVideoBackend()
    gen._user_id = "default"
    gen._config = FakeConfigResolver()
    gen.versions = _FakeVersions(initial_version=initial_version)
    gen.ledger = _FakeLedger()
    return gen


@pytest.mark.asyncio
async def test_resume_does_not_open_record_bracket(tmp_path):
    gen = _build_generator(tmp_path)

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
    )

    # Resume writes no new pending row: only resume_success/resume_failed backfill, no accounting bracket opened
    # resume 不落新 pending 行：只走 resume_success/resume_failed 补账，不开记账括号
    assert gen.ledger.started == [], "resume 不应开记账括号"


@pytest.mark.asyncio
async def test_resume_success_flips_pending_apicall_by_call_id(tmp_path):
    gen = _build_generator(tmp_path)

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
    )

    assert len(gen.ledger.resumed) == 1
    call = gen.ledger.resumed[0]
    assert call["call_id"] == 42
    assert call["status"] == "success"
    # The success backfill does not pass cost_amount explicitly, letting the repo auto-calc the actual cost from the ApiCall row fields
    # (equivalent to the generate path's accounting); cost_amount is not among resume_success's backfill arguments
    # 成功补账不显式传 cost_amount，让 repo 按 ApiCall 行字段 auto-calc 算实际 cost
    # （与 generate 路径记账等价）；cost_amount 不进 resume_success 的补账入参
    assert "cost_amount" not in call


@pytest.mark.asyncio
async def test_resume_idempotent_when_finalize_returns_zero(tmp_path, caplog):
    gen = _build_generator(tmp_path)
    gen.ledger._resume_affected = 0  # Simulate the "already success" idempotent case / 模拟「已 success」幂等场景

    output_path, version, _, _ = await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
    )

    assert output_path.exists()
    assert version == 1
    # 0 rows should not raise; it should be logged with logger.info / 0 rows 不应抛异常，应当 logger.info 记录
    assert len(gen.ledger.resumed) == 1


@pytest.mark.asyncio
async def test_resume_expired_flips_pending_to_failed(tmp_path):
    gen = _build_generator(tmp_path)
    expired = ResumeExpiredError(job_id="provider-job-1", provider="openai")
    gen._video_backend = _FakeVideoBackend(raises=expired)

    with pytest.raises(ResumeExpiredError):
        await gen.resume_video_async(
            job_id="provider-job-1",
            resource_type="videos",
            resource_id="E1S01",
            task_id="T-1",
        )

    assert len(gen.ledger.resumed) == 1
    call = gen.ledger.resumed[0]
    assert call["call_id"] == 42
    assert call["status"] == "failed"
    # The expired exception itself is handed to the backfill: encoding the raw failure text and machine code happens inside the ledger; here we only pin the "it was passed" step
    # 过期异常本身递交补账：失败原文与机器码的编码收在 ledger 内部，这里只锁「传了」这一步
    assert call["failure"] is expired
    # The zero-cost no-double-charge semantics live inside ledger.resume_failed (cost_amount=0.0); the backfill arguments do not carry it explicitly
    # 零费用不重扣的语义收在 ledger.resume_failed 内部（cost_amount=0.0），补账入参不显式承载


@pytest.mark.asyncio
async def test_resume_other_exception_does_not_finalize(tmp_path):
    """Anything other than ResumeExpiredError (e.g. download timeout) does not flip pending; it is left to the worker's retry mechanism.

    非 ResumeExpiredError（如下载超时）不翻 pending，留给 worker 重试机制处理。"""
    gen = _build_generator(tmp_path)
    gen._video_backend = _FakeVideoBackend(raises=RuntimeError("network timeout"))

    with pytest.raises(RuntimeError):
        await gen.resume_video_async(
            job_id="provider-job-1",
            resource_type="videos",
            resource_id="E1S01",
            task_id="T-1",
        )

    assert gen.ledger.resumed == [], "非 ResumeExpired 不应 finalize pending"


@pytest.mark.asyncio
async def test_resume_calls_add_version_after_download(tmp_path):
    """After a successful resume, add_version must record the new version, and add_version must happen after backend.resume_video
    (otherwise leftover/old files would be wrongly registered in the versions table). ensure_current_tracked must also not be pre-registered at the start.

    resume 成功后必须 add_version 记录新版本，且 add_version 发生在 backend.resume_video 之后
    （否则会把残留/旧文件错登记到 versions 表）。同时不应在开头预登记 ensure_current_tracked。"""
    gen = _build_generator(tmp_path)
    probe = _ProbeVideoBackend(gen.versions)
    gen._video_backend = probe

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
    )

    # add_version must happen after the download: add_calls is still 0 when entering resume_video
    # add_version 必须发生在下载之后：进入 resume_video 时 add_calls 还是 0
    assert probe.add_calls_at_resume == 0
    assert len(gen.versions.add_calls) == 1
    # No ensure_current_tracked at the start/end (avoids misregistering leftover files) / 开头/末尾不调 ensure_current_tracked（避免错位登记残留文件）
    assert gen.versions.ensure_calls == []


@pytest.mark.asyncio
async def test_resume_after_pre_version_crash_creates_v1(tmp_path):
    """Crash during submit→poll → versions.json empty → after resume downloads the new video, add_version registers v1,
    so the downstream finalize_video_task does not hit IndexError on versions[-1].

    submit→poll 中崩 → versions.json 空 → resume 下载新视频后 add_version 登记 v1，
    避免下游 finalize_video_task 在 versions[-1] 上 IndexError。"""
    gen = _build_generator(tmp_path, initial_version=0)

    _, version, _, _ = await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
    )

    assert version == 1
    assert len(gen.versions.add_calls) == 1


@pytest.mark.asyncio
async def test_resume_after_version_v1_crash_bumps_to_v2(tmp_path):
    """Overwriting regeneration: versions.json already has v1 and generate crashed before add_version.
    After resume downloads the new video over output_path it must bump to v2 so versions.json matches the disk content;
    otherwise versions={v1} still points at the old record while output_path already holds v2 content, and rollback/history become wrong.

    覆盖式重新生成：versions.json 已有 v1，generate 在 add_version 之前崩。
    resume 下载新视频覆盖 output_path 后必须 bump 到 v2，让 versions.json 与磁盘内容一致；
    否则 versions={v1} 仍指向旧记录而 output_path 已是 v2 内容，回滚/历史会失真。"""
    gen = _build_generator(tmp_path, initial_version=1)

    _, version, _, _ = await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
    )

    assert version == 2, "已有 v1 + 覆盖式重新生成 → 必须登记 v2"
    assert len(gen.versions.add_calls) == 1


@pytest.mark.asyncio
async def test_resume_formal_output_uses_same_staged_version_transaction(tmp_path):
    from lib.artifacts.version_manager import VersionManager

    gen = _build_generator(tmp_path)
    gen.versions = VersionManager(gen.project_path)
    current = gen._get_output_path("reference_videos", "E1U1")
    current.parent.mkdir(parents=True)
    current.write_bytes(b"old-current")

    events = []

    async def _prepare(staged_file, duration_seconds, version_metadata):
        assert await asyncio.to_thread(staged_file.read_bytes) == b"fake-resume-video"
        assert duration_seconds == 8
        events.append("prepared")

    def _commit(*args):
        events.append("committed")
        return select_formal_video(gen)(*args)

    output, version, _, _ = await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="reference_videos",
        resource_id="E1U1",
        task_id="T-1",
        formal_output=True,
        before_formal_commit=_prepare,
        commit_formal_output=_commit,
        execution_request_digest="d" * 64,
    )

    assert output.read_bytes() == b"fake-resume-video"
    assert version == 2
    history = gen.versions.get_versions("reference_videos", "E1U1")
    assert [item["prompt"] for item in history["versions"]] == ["", ""]
    assert history["versions"][-1]["execution_request_digest"] == "d" * 64
    assert events == ["prepared", "committed"]


@pytest.mark.asyncio
async def test_resume_formal_prepare_failure_archives_paid_history_without_selecting_it(tmp_path):
    from lib.artifacts.version_manager import VersionManager

    gen = _build_generator(tmp_path)
    gen.versions = VersionManager(gen.project_path)
    current = gen._get_output_path("reference_videos", "E1U1")
    current.parent.mkdir(parents=True)
    current.write_bytes(b"old-current")

    async def _fail_prepare(*_args):
        raise RuntimeError("resume paid output validation failed")

    with pytest.raises(RuntimeError, match="resume paid output validation failed"):
        await gen.resume_video_async(
            job_id="provider-job-1",
            resource_type="reference_videos",
            resource_id="E1U1",
            task_id="T-1",
            formal_output=True,
            before_formal_commit=_fail_prepare,
        )

    assert current.read_bytes() == b"old-current"
    history = gen.versions.get_versions("reference_videos", "E1U1")
    assert history["current_version"] == 1
    assert len(history["versions"]) == 2
    assert history["versions"][-1]["is_current"] is False
    assert (gen.project_path / history["versions"][-1]["file"]).read_bytes() == b"fake-resume-video"


@pytest.mark.asyncio
async def test_resume_handles_float_string_duration(tmp_path):
    """When duration_seconds is passed as a float string (e.g. "10.0") it should be parsed as int(10),
    not silently swallowed by try/except into the fallback value 8 (int("10.0") raises ValueError).
    Both add_version and VideoGenerationRequest should receive the normalized int.

    duration_seconds 传浮点字符串（如 "10.0"）时应解析为 int(10)，
    不能被 try/except 静默吞成兜底值 8（int("10.0") 会 ValueError）。
    add_version 与 VideoGenerationRequest 都应收到归一后的 int。"""
    gen = _build_generator(tmp_path, initial_version=1)

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        duration_seconds="10.0",
        task_id="T-1",
    )

    # add_version should receive duration_seconds=10 (int), neither "10.0" nor 8 / add_version 应该收到 duration_seconds=10（int），不是 "10.0" 也不是 8
    assert len(gen.versions.add_calls) == 1
    add_call = gen.versions.add_calls[0]
    assert add_call["duration_seconds"] == 10
    assert isinstance(add_call["duration_seconds"], int)

    # duration_seconds in the provider request should also be int(10) / provider 请求里的 duration_seconds 也应是 int(10)
    backend = gen._video_backend
    assert len(backend.calls) == 1
    _, request = backend.calls[0]
    assert request.duration_seconds == 10
    assert isinstance(request.duration_seconds, int), "归一后类型必须是 int 而非 str/float"


@pytest.mark.asyncio
async def test_resume_passes_usage_tokens_to_finalize(tmp_path):
    """Ark video is billed by usage_tokens, and defaulting to 0 would make the cost always 0;
    the resume path must pass the usage_tokens returned by backend.resume_video through to finalize_pending_by_call_id,
    equivalent to the generate path's finish_call(..., usage_tokens=...) accounting.

    Ark 视频按 usage_tokens 计费，缺省为 0 会导致 cost 永远为 0；
    resume 路径必须把 backend.resume_video 返回的 usage_tokens 透传到 finalize_pending_by_call_id，
    与 generate 路径 finish_call(..., usage_tokens=...) 等价记账。"""

    class _ArkLikeResult:
        def __init__(self) -> None:
            self.video_uri = "video-uri-resume"
            self.usage_tokens = 12345  # Simulate the completion_tokens returned by Ark / 模拟 Ark 返回的 completion_tokens
            self.generate_audio = True
            self.duration_seconds = 8
            self.seed = None
            self.provenance = None
            self.warnings = ()

    class _ArkLikeBackend:
        name = "ark"
        model = "doubao-seedance-1-0-pro"

        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def generate(self, request):
            raise AssertionError("generate 不应被 resume 路径调用")

        async def resume_video(self, job_id, request):
            self.calls.append((job_id, request))
            await write_backend_output(request.output_path, b"fake-resume-video")
            return _ArkLikeResult()

    gen = _build_generator(tmp_path)
    gen._video_backend = _ArkLikeBackend()

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
    )

    assert len(gen.ledger.resumed) == 1
    call = gen.ledger.resumed[0]
    assert call["call_id"] == 42
    assert call["status"] == "success"
    assert call["result"].usage_tokens == 12345, "backend 结果对象必须递交，usage_tokens 由 ledger 分发透传"


@pytest.mark.asyncio
async def test_resume_without_settleable_call_row_warns_does_not_crash(tmp_path, caplog):
    """No pending call row found by task_id → resume still succeeds, with only a warning.

    按 task_id 反查不到待结算调用行 → resume 仍成功，仅 warning。"""
    gen = _build_generator(tmp_path)
    gen.ledger = _FakeLedger(pending_call_id=None)

    output_path, version, _, _ = await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
    )

    assert output_path.exists()
    assert version == 1
    assert gen.ledger.resumed == [], "反查不到调用行时不应 finalize"


@pytest.mark.asyncio
async def test_resume_looks_up_settleable_call_by_task_id(tmp_path):
    """The call row is looked up by task identity rather than passed through by the caller; task and call have a single source of truth, api_calls.task_id.

    调用行按任务身份反查，不再由 caller 透传 —— 任务与调用只有 api_calls.task_id 一个真相源。"""
    gen = _build_generator(tmp_path)

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
    )

    assert gen.ledger.lookups == ["T-1"]
    assert gen.ledger.resumed[0]["call_id"] == 42


class _FailingResumeLedger(_FakeLedger):
    """Simulates the case where the resume backfill (finalize) itself fails.

    模拟 resume 补账（finalize）自身失败的场景。"""

    def __init__(self, *, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    async def resume_success(
        self, *, call_id: int, result: Any, usage: Any = None, service_tier: str = "default"
    ) -> int:
        self.resumed.append({"status": "success", "call_id": call_id})
        raise self._exc

    async def resume_failed(self, *, call_id: int, failure: BaseException | str | None = None) -> int:
        self.resumed.append({"status": "failed", "call_id": call_id, "failure": failure})
        raise self._exc


@pytest.mark.asyncio
async def test_resume_success_propagates_finalize_exception(tmp_path):
    """An exception from finalize_pending_by_call_id(success) must bubble up and not be swallowed.

    Swallowing it would leave the corresponding ApiCall stuck in pending forever (the success branch has no later retry,
    and the expired branch is terminal), leaving a permanent gap in usage reports and backfill. Fail fast
    and let the worker's finally mark_failed act as the fallback.

    finalize_pending_by_call_id(success) 异常必须冒泡，不能被吞掉。

    吞掉会让对应的 ApiCall 永远卡在 pending（success 分支无后续重试，
    expired 分支又是终态），usage 报表和补账会出现永久缺口。fail-fast
    交给 worker finally 的 mark_failed 兜底。"""
    gen = _build_generator(tmp_path)
    gen.ledger = _FailingResumeLedger(exc=RuntimeError("db down"))

    with pytest.raises(RuntimeError, match="db down"):
        await gen.resume_video_async(
            job_id="provider-job-1",
            resource_type="videos",
            resource_id="E1S01",
            task_id="T-1",
        )

    # finalize was called once (see the arguments before the exception propagated) / finalize 被调过一次（看到异常上抛前的入参）
    assert len(gen.ledger.resumed) == 1


@pytest.mark.asyncio
async def test_resume_formal_output_cleans_staging_when_finalize_fails(tmp_path):
    from lib.artifacts.version_manager import VersionManager

    gen = _build_generator(tmp_path)
    gen.versions = VersionManager(gen.project_path)
    gen.ledger = _FailingResumeLedger(exc=RuntimeError("db down"))
    current = gen._get_output_path("reference_videos", "E1U1")
    current.parent.mkdir(parents=True)
    current.write_bytes(b"old-current")

    with pytest.raises(RuntimeError, match="db down"):
        await gen.resume_video_async(
            job_id="provider-job-1",
            resource_type="reference_videos",
            resource_id="E1U1",
            task_id="T-1",
            formal_output=True,
        )

    assert current.read_bytes() == b"old-current"
    assert list(current.parent.glob(".E1U1.*.mp4")) == []


@pytest.mark.asyncio
async def test_resume_expired_propagates_finalize_exception(tmp_path):
    """The ResumeExpiredError branch likewise must not swallow the finalize exception: let the worker's finally mark it
    failed as a fallback, so the ApiCall does not stay stuck in pending forever.

    ResumeExpiredError 分支同样不能吞 finalize 异常：让 worker finally 兜底标记
    失败，避免 ApiCall 永远卡 pending。"""
    gen = _build_generator(tmp_path)
    gen._video_backend = _FakeVideoBackend(raises=ResumeExpiredError(job_id="provider-job-1", provider="openai"))
    gen.ledger = _FailingResumeLedger(exc=RuntimeError("db down"))

    # The RuntimeError raised by finalize should override the ResumeExpiredError that would otherwise propagate
    # finalize 抛 RuntimeError 应该覆盖原本要抛的 ResumeExpiredError 上抛
    with pytest.raises(RuntimeError, match="db down"):
        await gen.resume_video_async(
            job_id="provider-job-1",
            resource_type="videos",
            resource_id="E1S01",
            task_id="T-1",
        )

    assert len(gen.ledger.resumed) == 1


@pytest.mark.asyncio
async def test_resume_expired_cleans_formal_staging_when_finalize_fails(tmp_path):
    from lib.artifacts.version_manager import VersionManager

    class _WriteThenExpireBackend(_FakeVideoBackend):
        async def resume_video(self, job_id, request):
            await write_backend_output(request.output_path, b"partial-download")
            raise ResumeExpiredError(job_id=job_id, provider="openai")

    gen = _build_generator(tmp_path)
    gen.versions = VersionManager(gen.project_path)
    gen._video_backend = _WriteThenExpireBackend()
    gen.ledger = _FailingResumeLedger(exc=RuntimeError("db down"))
    current = gen._get_output_path("reference_videos", "E1U1")
    current.parent.mkdir(parents=True)
    current.write_bytes(b"old-current")

    with pytest.raises(RuntimeError, match="db down"):
        await gen.resume_video_async(
            job_id="provider-job-1",
            resource_type="reference_videos",
            resource_id="E1U1",
            task_id="T-1",
            formal_output=True,
        )

    assert current.read_bytes() == b"old-current"
    assert list(current.parent.glob(".E1U1.*.mp4")) == []


@pytest.mark.asyncio
async def test_resume_cancellation_cleans_partial_formal_staging(tmp_path):
    from lib.artifacts.version_manager import VersionManager

    class _WriteThenCancelBackend(_FakeVideoBackend):
        async def resume_video(self, job_id, request):
            await write_backend_output(request.output_path, b"partial-download")
            raise asyncio.CancelledError

    gen = _build_generator(tmp_path)
    gen.versions = VersionManager(gen.project_path)
    gen._video_backend = _WriteThenCancelBackend()
    current = gen._get_output_path("reference_videos", "E1U1")
    current.parent.mkdir(parents=True)
    current.write_bytes(b"old-current")

    with pytest.raises(asyncio.CancelledError):
        await gen.resume_video_async(
            job_id="provider-job-1",
            resource_type="reference_videos",
            resource_id="E1U1",
            task_id="T-1",
            formal_output=True,
        )

    assert current.read_bytes() == b"old-current"
    assert list(current.parent.glob(".E1U1.*.mp4")) == []


@pytest.mark.asyncio
async def test_resume_passes_generate_audio_to_finalize(tmp_path):
    """A provider may degrade/turn off audio after submit; resume must pass the generate_audio returned by the backend
    through to finalize_pending_by_call_id, so the cost does not reuse the requested value and bill wrongly
    (equivalent to the generate path's finish_call(generate_audio=result.generate_audio)).

    provider 在 submit 后可能降级/关闭音频；resume 必须把 backend 返回的
    generate_audio 透传到 finalize_pending_by_call_id，避免 cost 沿用请求值误计费
    （与 generate 路径 finish_call(generate_audio=result.generate_audio) 等价）。"""

    class _AudioDowngradeResult:
        def __init__(self) -> None:
            self.video_uri = "video-uri-resume"
            self.usage_tokens = 1234
            self.generate_audio = False  # The provider actually degraded to no audio / provider 实际降级到无音频
            self.duration_seconds = 8
            self.seed = None
            self.provenance = None
            self.warnings = ()

    class _AudioDowngradeBackend:
        name = "fake"
        model = "video-model"

        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def generate(self, request):
            raise AssertionError("generate 不应被 resume 路径调用")

        async def resume_video(self, job_id, request):
            self.calls.append((job_id, request))
            await write_backend_output(request.output_path, b"fake-resume-video")
            return _AudioDowngradeResult()

    gen = _build_generator(tmp_path)
    gen._video_backend = _AudioDowngradeBackend()

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
    )

    assert len(gen.ledger.resumed) == 1
    call = gen.ledger.resumed[0]
    assert call["result"].generate_audio is False, "backend 结果对象必须递交，generate_audio 由 ledger 分发透传"


@pytest.mark.asyncio
async def test_resume_passes_billed_duration_to_finalize(tmp_path):
    """DashScope's resume and generate share the same poll; result.duration_seconds may be
    the actual billed duration including the input reference video's length; resume must pass it through to finalize_pending_by_call_id,
    equivalent to the generate path's finish_call(billed_duration_seconds=...) accounting.

    DashScope 的 resume 与 generate 走同一段 poll，result.duration_seconds 可能是
    含输入参考视频时长的实际计费时长；resume 必须把它透传到 finalize_pending_by_call_id，
    与 generate 路径 finish_call(billed_duration_seconds=...) 等价记账。"""

    class _BilledDurationResult:
        def __init__(self) -> None:
            self.video_uri = "video-uri-resume"
            self.usage_tokens = 0
            self.generate_audio = True
            self.duration_seconds = 15  # Requested 8 seconds, the provider bills 15 seconds / 请求 8 秒，provider 按 15 秒计费
            self.seed = None
            self.provenance = None
            self.warnings = ()

    class _BilledDurationBackend:
        name = "dashscope"
        model = "wan2.7-r2v"

        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def generate(self, request):
            raise AssertionError("generate 不应被 resume 路径调用")

        async def resume_video(self, job_id, request):
            self.calls.append((job_id, request))
            await write_backend_output(request.output_path, b"fake-resume-video")
            return _BilledDurationResult()

    gen = _build_generator(tmp_path)
    gen._video_backend = _BilledDurationBackend()

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        duration_seconds="8",
        task_id="T-1",
    )

    assert len(gen.ledger.resumed) == 1
    call = gen.ledger.resumed[0]
    assert call["result"].duration_seconds == 15, "backend 结果对象必须递交，实际计费时长由 ledger 分发透传"


@pytest.mark.asyncio
async def test_resume_forwards_submitted_base_url_to_request(tmp_path):
    """The domain used at submit goes into the request handed to the backend, and not into the version metadata.

    It is a replay value fed to backend polling; mixing it into versions.json would record a connection parameter as a version attribute.

    提交时的域名装进递交 backend 的 request，且不落进版本元数据。

    它是喂给 backend 轮询的回放值，混进 versions.json 会把一个连接参数写成版本属性。
    """
    gen = _build_generator(tmp_path)
    backend = gen._video_backend

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
        submitted_base_url="https://maas-a.example.com/ws-1/api/v1",
    )

    _, request = backend.calls[0]
    assert request.submitted_base_url == "https://maas-a.example.com/ws-1/api/v1"
    assert all("submitted_base_url" not in kwargs for kwargs in gen.versions.add_calls)


@pytest.mark.asyncio
async def test_resume_records_the_actual_seed_and_provenance_like_a_first_run(tmp_path):
    """The version metadata written for the same task before and after a restart must be the same.

    The rest of the metadata is finalized before submit, while the actual seed and the fingerprint of the actual workflow only exist once the
    backend finishes; merging them only on the first-run path would make "which seed this version used" depend on when the process restarted.

    同一笔任务在重启前后落下的版本元数据必须是同一份。

    元数据的其余部分在提交之前就已定稿，而实发种子与实发 workflow 的指纹要到 backend 跑完才
    存在——只在首跑那条路上并一次，「这一版用的哪个种子」就会取决于进程什么时候重启过。
    """
    gen = _build_generator(tmp_path)
    gen._video_backend = _FakeVideoBackend(result=_FakeVideoResult(seed=4242, provenance={"workflow_sha256": "abc123"}))

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
        seed=7,
    )

    recorded = gen.versions.add_calls[-1]
    assert recorded["seed"] == 4242
    assert recorded["workflow_sha256"] == "abc123"


@pytest.mark.asyncio
async def test_resume_hands_backend_warnings_to_the_caller(tmp_path):
    """Hints produced during a resume go through the same channel as the first run; otherwise only first-run users would see them.

    续跑期产生的提示与首跑走同一条通道，否则同一件事只有首跑的用户看得到。"""
    warning = {"key": "comfyui_multiple_outputs", "params": {"count": 2, "filename": "final.mp4"}}
    gen = _build_generator(tmp_path)
    gen._video_backend = _FakeVideoBackend(result=_FakeVideoResult(warnings=(warning,)))
    collected: list[dict[str, Any]] = []

    await gen.resume_video_async(
        job_id="provider-job-1",
        resource_type="videos",
        resource_id="E1S01",
        task_id="T-1",
        warnings=collected,
    )

    assert collected == [warning]
