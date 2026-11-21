"""
MediaGenerator middle layer

Wraps GeminiClient + VersionManager, providing version management that is "transparent to callers".
Callers only pass project_path and resource_id, and version management happens automatically.

Covered resource types:
- storyboards: storyboard images (scene_E1S01.png)
- videos: videos (scene_E1S01.mp4)
- characters: character sheets (姜月茴.png)
- scenes: scene sheets (庙宇.png)
- props: prop sheets (玉佩.png)
- grids: grid images (grid_xxx.png)

MediaGenerator 中间层

封装 GeminiClient + VersionManager，提供"调用方无感"的版本管理。
调用方只需传入 project_path 和 resource_id，版本管理自动完成。

覆盖的资源类型：
- storyboards: 分镜图 (scene_E1S01.png)
- videos: 视频 (scene_E1S01.mp4)
- characters: 角色资产图 (姜月茴.png)
- scenes: 场景资产图 (庙宇.png)
- props: 道具资产图 (玉佩.png)
- grids: 宫格图 (grid_xxx.png)
"""

import asyncio
import hashlib
import logging
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

from PIL import Image

if TYPE_CHECKING:
    from arcreel_market_core.video_backend_contract import VideoGenerationResult
    from lib.backends.audio_backends.base import AudioBackend
    from lib.backends.image_backends.base import ImageBackend
    from lib.config.resolver import ConfigResolver
    from lib.references.reference_compression import CompressedRef, PayloadLimits, ReferenceSpec

from lib.artifacts.version_manager import (
    GENERATION_CALL_VERSION_FIELD,
    GENERATION_TASK_VERSION_FIELD,
    PaidVersionCommit,
    VersionManager,
)
from lib.backends.gemini_shared import RateLimiter
from lib.backends.providers import CallPurpose, CallType, require_provider_pair
from lib.billing.ledger import Ledger
from lib.config.service import DEFAULT_VIDEO_POLL_TIMEOUT_SECONDS
from lib.db.base import DEFAULT_USER_ID
from lib.generation.deleted_target import trash_result_if_target_deleted
from lib.generation.video_request_facts import DEFAULT_PLANNED_DURATION_SECONDS
from lib.infra.async_thread import run_noninterruptible_sync, run_sync_transaction
from lib.infra.path_safety import PathTraversalError, safe_join
from lib.project.resource_paths import resource_relative_path
from lib.speech.audio_utils import probe_reference_audio_total_seconds

logger = logging.getLogger(__name__)


def task_media_staging_path(output_path: Path, task_id: str) -> Path:
    """Return the bounded, deterministic formal-output path owned by one task."""

    if not task_id:
        raise ValueError("formal media output requires a task_id")
    token = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return output_path.with_name(f".{token}.task-output{output_path.suffix}")


def _is_junction(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction is not None and isjunction(path))


def task_video_staging_path(output_path: Path, task_id: str) -> Path:
    """Compatibility name for video task staging."""

    return task_media_staging_path(output_path, task_id)


def task_image_staging_path(output_path: Path, task_id: str) -> Path:
    """Return the bounded staging path for one formal image task."""

    return task_media_staging_path(output_path, task_id)


def _remove_task_staging_path(path: Path) -> None:
    """Remove a task-owned staging entry without traversing links or arbitrary directories."""

    if path.is_symlink():
        path.unlink(missing_ok=True)  # hard-delete: temp -- staged output of a generation task / 生成任务的暂存输出
    elif _is_junction(path):
        path.rmdir()  # hard-delete: temp -- staging directory of a generation task / 生成任务的暂存目录
    elif os.path.lexists(path):
        if not path.is_file():
            raise ValueError("formal media staging path is not a regular file")
        path.unlink()  # hard-delete: temp -- staged output of a generation task / 生成任务的暂存输出


_remove_task_video_staging_path = _remove_task_staging_path


def cleanup_staged_video_output(
    project_path: Path,
    resource_type: str,
    resource_id: str,
    task_id: str,
) -> None:
    """Remove the deterministic interrupted output owned by one video task."""

    output_path = safe_join(project_path, resource_relative_path(resource_type, resource_id))
    _remove_task_video_staging_path(task_video_staging_path(output_path, task_id))


async def _discard_staged_output(path: Path | None) -> None:
    """Delete the staging file in a worker thread, returning only after the deletion settles.

    Only used on wrap-up and failure paths: it introduces no new cancellation point; the success path returns its result as
    usual and the failure path propagates the original exception as usual.

    在工作线程里删除 staging 文件，删除结算完再返回。

    只用在收尾与失败路径上：不引入新的取消点，成功路径照常返回结果，失败路径照常传播原异常。
    """

    if path is not None:
        await run_noninterruptible_sync(path.unlink, missing_ok=True)  # hard-delete: temp -- staged output of a generation task / 生成任务的暂存输出


@asynccontextmanager
async def _remove_staged_output_on_error(path: Path | None):
    """Remove a formal-output staging file whenever the guarded operation aborts."""

    try:
        yield
    except BaseException:
        await _discard_staged_output(path)
        raise


def _is_413(exc: BaseException) -> bool:
    """Recognise request body too large (HTTP 413).

    First extract the status code from common exception attributes: ``status_code`` (OpenAI/xai SDK + normalised
    vidu/dashscope) / ``response.status_code`` (httpx.HTTPStatusError) / ``code`` (google-genai APIError) -- covering the
    default provider gemini and the various SDK backends, not just httpx (consistent with the status code extraction in
    lib/backends/video_backends/ark.py). When the status code is missing fall back to phrase matching, but not the bare
    "413" substring -- to avoid false hits on byte counts / request IDs (like "41300 bytes").

    识别请求体超限（HTTP 413）。

    先从异常通用属性提取状态码：``status_code``（OpenAI/xai SDK + 规整后的 vidu/dashscope）/
    ``response.status_code``（httpx.HTTPStatusError）/ ``code``（google-genai APIError）——
    覆盖默认 provider gemini 及各 SDK 类后端，而非只认 httpx（与 lib/backends/video_backends/ark.py 的
    状态码提取口径一致）。状态码缺失时退回短语匹配，但不用裸 "413" 子串——避免被字节数 /
    请求 ID（如 "41300 bytes"）误命中。
    """
    status = (
        getattr(exc, "status_code", None)
        or getattr(getattr(exc, "response", None), "status_code", None)
        or getattr(exc, "code", None)
    )
    # Defensive int conversion: some SDKs / mocks may give the status code as the string "413",
    # and a direct ``== 413`` would always be False; non-numeric status codes fall back to the phrase matching below.
    # 防御性 int 转换：个别 SDK / mock 可能把状态码给成字符串 "413"，
    # 直接 ``== 413`` 会恒 False；非数字状态码落回下方短语匹配。
    try:
        if status is not None and int(status) == 413:
            return True
    except (ValueError, TypeError):
        pass
    msg = str(exc).lower()
    return "request entity too large" in msg or "payload too large" in msg


# Accounting segment_id decision: whether resource_id may serve as segment_id is decided at this single point by an
# allowlist of resource_types per call_type; all three accounting paths image/video/audio go through it.
# `SEGMENT_RESOURCE_TYPES` in the frontend's `frontend/src/components/usage/usage-record-view.ts` holds the same allowlist,
# used to decide shot labels on in-progress task rows; when adding or removing resource types here, change that one too,
# otherwise the target labels while the task is running and after it is persisted won't match.
# 记账 segment_id 判定：resource_id 可否作 segment_id 按 call_type 各自的 resource_type
# 白名单收敛于此单点，image/video/audio 三条记账路径均经由它。
# 前端 `frontend/src/components/usage/usage-record-view.ts` 的 `SEGMENT_RESOURCE_TYPES`
# 持有同一份白名单，用于给进行中的任务行判定分镜标签；这里增删资源类型时一并改那一处，
# 否则任务进行中与落库后的目标标签会对不上。
_SEGMENT_ID_ALLOWED_RESOURCE_TYPES: dict[CallType, frozenset[str]] = {
    "image": frozenset({"storyboards", "videos", "grids"}),
    "video": frozenset({"storyboards", "videos", "reference_videos"}),
}


def segment_id_for(call_type: CallType, resource_type: str, resource_id: str) -> str | None:
    """Decide segment_id from the accounting call type and resource type; audio has no allowlist and is passed through unconditionally.

    按记账调用类型与资源类型判定 segment_id；audio 无白名单，无条件透传。"""
    if call_type == "audio":
        return resource_id
    allowed = _SEGMENT_ID_ALLOWED_RESOURCE_TYPES.get(call_type)
    if allowed is None:
        raise ValueError(f"unknown ledger channel: {call_type!r}")
    return resource_id if resource_type in allowed else None


def _project_relative_path(project_path: Path, value: object) -> str | None:
    """Normalise a file path in the call record (input material or artifact) to a project-relative path (POSIX separators).

    The read side uses it to locate the file in the project, and the record must stay valid after the data root moves, so
    it is always relative to the project root; paths outside the project (temporary materials, absolute path references)
    are kept as is. Non-path values (PIL Image etc.) return None, and the call site decides whether to record the item.

    把调用记录里的一个文件路径（输入素材或产物）归一为项目内相对路径（POSIX 分隔符）。

    读侧要拿它在项目里定位文件，且数据根挪位后记录仍须有效，故一律相对项目根；
    项目外的路径（临时素材、绝对路径引用）保留原样。非路径值（PIL Image 等）返回 None，
    由调用点决定是否记这一项。
    """
    if not isinstance(value, (str, Path)):
        return None
    path = Path(value)
    try:
        # Take absolute paths on both sides before computing the relative path: normalisation works even when the project root or material path is relative (local debugging, CLI).
        # 两边都取绝对路径再求相对：项目根或素材路径任一为相对路径（本地调试、CLI）时同样能归一。
        return path.absolute().relative_to(project_path.absolute()).as_posix()
    except ValueError:
        return path.as_posix()


def _ledger_inputs(**sections: object) -> dict[str, Any] | None:
    """The ``inputs`` value with empty groups dropped; None when all are empty, so the accounting column stays empty instead of holding an empty object.

    丢掉空分组后的 ``inputs`` 值；全空时给 None，让记账列留空而不是写一个空对象。"""
    kept = {key: value for key, value in sections.items() if value not in (None, [], {})}
    return kept or None


def _merge_result_provenance(version_metadata: dict[str, Any], result: "VideoGenerationResult") -> None:
    """Merge facts only known after generating once into the version metadata, before saving.

    The rest of the metadata is finalised before submission (it is part of the request), while the seed actually sent and
    the fingerprint of the workflow actually sent only exist after the backend finishes -- ComfyUI's ``policy: "random"`` is
    randomised at the moment of submission, and if not written down this version can never be reproduced.

    ``seed`` is only overwritten when the backend reports the value actually sent: most channels echo the one from the
    request, so overwriting is an identity; for those reporting a value different from the request (the provider decides the
    seed itself), what is written down is the one really used for generation.

    把只有生成过一次才知道的事实并进版本元数据，在落盘之前。

    元数据的其余部分在提交之前就已定稿（它们是请求的一部分），而实发种子与实发 workflow 的指纹
    要到 backend 跑完才存在——ComfyUI 的 ``policy: "random"`` 是提交那一刻现随机的，不写下来这
    一版就再也复现不了。

    ``seed`` 只在 backend 报了实发值时覆盖：多数通道原样回显请求里的那一个，覆盖是恒等；回报了
    与请求不同的那些（供应商自行决定种子），写下来的才是真正生成用的。
    """
    if result.seed is not None:
        version_metadata["seed"] = result.seed
    version_metadata.update(result.provenance or {})


def _collect_result_warnings(warnings: list[dict[str, Any]] | None, result: "VideoGenerationResult") -> None:
    """Merge notes produced by the backend during execution into the caller's collector, which puts them into the task's ``result.warnings``.

    The collector is passed in by the caller rather than returned from here: both video paths return a fixed four-tuple,
    and adding a slot would ripple to every call site and a batch of test cases, while the callers already each hold a
    warning list to be put into the result.

    把 backend 执行期产生的提示并进调用方的收集器，交由它落到任务 ``result.warnings``。

    收集器由调用方传入而不是从这里返回：视频两条路的返回值都是定死的四元组，再加一位会波及每一个
    调用点与一批用例，而调用方本就各自持有一张要落进结果的 warning 列表。
    """
    if warnings is None or not result.warnings:
        return
    warnings.extend(dict(warning) for warning in result.warnings)


def _generation_link(task_id: str | None, call_id: int | None) -> dict[str, Any]:
    """Version metadata linking a version to the queue task and the ledger call that produced it (API G16); empty for
    direct calls without a task, which keep the time-based match.

    把版本与产出它的队列任务和账本调用关联起来的版本元数据（API G16）；无任务的直接调用为空，仍按时间匹配。"""
    if task_id is None:
        return {}
    link: dict[str, Any] = {GENERATION_TASK_VERSION_FIELD: task_id}
    if call_id is not None:
        link[GENERATION_CALL_VERSION_FIELD] = call_id
    return link


def _commit_formal_image_output(
    commit_formal_output: Callable[[Path, Path, Mapping[str, Any]], int],
    staged_output_path: Path,
    output_path: Path,
    metadata: Mapping[str, Any],
) -> int:
    """Validate the staging artifact and commit the final image in a worker thread; delete staging after a successful commit.

    在工作线程里校验 staging 产物、提交正式图片，提交成功后删除 staging。"""

    if not staged_output_path.is_file():
        raise RuntimeError("image backend completed without a regular staged output file")
    new_version = commit_formal_output(staged_output_path, output_path, metadata)
    if type(new_version) is not int or new_version < 1:
        raise RuntimeError("formal image commit did not return a positive version")
    staged_output_path.unlink(missing_ok=True)  # hard-delete: temp -- staged output after commit / 提交后的暂存输出
    return new_version


class MediaGenerator:
    """
    Media generator middle layer

    Wraps GeminiClient + VersionManager, providing automatic version management.

    媒体生成器中间层

    封装 GeminiClient + VersionManager，提供自动版本管理。
    """

    def __init__(
        self,
        project_path: Path,
        rate_limiter: RateLimiter | None = None,
        image_backend: Optional["ImageBackend"] = None,
        video_backend=None,
        audio_backend: Optional["AudioBackend"] = None,
        *,
        config_resolver: Optional["ConfigResolver"] = None,
        user_id: str = DEFAULT_USER_ID,
        image_provider_id: str | None = None,
        video_provider_id: str | None = None,
        audio_provider_id: str | None = None,
    ):
        """
        Initialise MediaGenerator

        Args:
            project_path: project root directory path
            rate_limiter: optional rate limiter instance
            image_backend: optional ImageBackend instance (for image generation)
            video_backend: optional VideoBackend instance (for video generation)
            audio_backend: optional AudioBackend instance (for speech synthesis)
            config_resolver: ConfigResolver instance, for reading config at runtime
            user_id: user ID
            image_provider_id: image registry provider_id. Both the basis for resolving the per-provider limit of
                reference image compression and the single source of truth for the accounting provider of this lane.
                Must be a registry id (e.g. "gemini-aistudio"), not backend.name; provided as a pair with
                image_backend, raises if either is missing
            video_provider_id: video registry provider_id (same as above, for I2V/R2V and video accounting)
            audio_provider_id: audio registry provider_id (for narration voiceover accounting), paired with audio_backend

        初始化 MediaGenerator

        Args:
            project_path: 项目根目录路径
            rate_limiter: 可选的限流器实例
            image_backend: 可选的 ImageBackend 实例（用于图片生成）
            video_backend: 可选的 VideoBackend 实例（用于视频生成）
            audio_backend: 可选的 AudioBackend 实例（用于语音合成）
            config_resolver: ConfigResolver 实例，用于运行时读取配置
            user_id: 用户 ID
            image_provider_id: 图像 registry provider_id。既是解析参考图压缩 per-provider 上限的
                依据，也是该 lane 记账 provider 的单一真相源。须为 registry id（如 "gemini-aistudio"），
                非 backend.name；与 image_backend 成对提供，缺一即抛
            video_provider_id: 视频 registry provider_id（同上，I2V/R2V 与视频记账用）
            audio_provider_id: 音频 registry provider_id（旁白配音记账用），与 audio_backend 成对
        """
        require_provider_pair("image", image_backend, image_provider_id)
        require_provider_pair("video", video_backend, video_provider_id)
        require_provider_pair("audio", audio_backend, audio_provider_id)

        self.project_path = Path(project_path)
        self.project_name = self.project_path.name
        self._rate_limiter = rate_limiter
        self._image_backend = image_backend
        self._video_backend = video_backend
        self._audio_backend = audio_backend
        self._config = config_resolver
        self._user_id = user_id
        self._image_provider_id = image_provider_id
        self._video_provider_id = video_provider_id
        self._audio_provider_id = audio_provider_id
        self.versions = VersionManager(project_path)

        # Initialise the accounting ledger (using the global async session factory) / 初始化记账账本（使用全局 async session factory）
        self.ledger = Ledger()

    @staticmethod
    def _prepare_video_output(
        output_path: Path,
        *,
        formal_output: bool,
        task_id: str | None,
    ) -> tuple[Path | None, Path]:
        """Return an optional same-directory staging path and the path the backend must write."""

        if not formal_output:
            return None, output_path
        if task_id is None:
            raise ValueError("formal video output requires a task_id")
        staged_output_path = task_video_staging_path(output_path, task_id)
        # A process can die after the backend starts writing. Reusing one task-addressable path bounds crash residue
        # and removes the prior attempt before normal generation or resume writes the same provider result again.
        _remove_task_video_staging_path(staged_output_path)
        return staged_output_path, staged_output_path

    @staticmethod
    def _prepare_image_output(
        output_path: Path,
        *,
        formal_output: bool,
        task_id: str | None,
    ) -> tuple[Path | None, Path]:
        """Return the task staging path that the image backend may write."""

        if not formal_output:
            return None, output_path
        # Direct calls (not queue tasks) have no queue identity: give a one-off staging identity, without faking a task_id
        # -- the task_id written by accounting must be findable in tasks, and if one can't be produced leave it empty.
        # 直接调用（非队列任务）没有队列身份：给一次性 staging 身份，不伪造一个 task_id
        # ——记账写的 task_id 必须能在 tasks 里查到，编不出来就留空。
        staging_token = task_id or f"inline-{uuid.uuid4().hex}"
        staged_output_path = task_image_staging_path(output_path, staging_token)
        _remove_task_staging_path(staged_output_path)
        return staged_output_path, staged_output_path

    def _prepare_image_target_sync(
        self,
        *,
        resource_type: str,
        resource_id: str,
        formal_output: bool,
        task_id: str | None,
        prompt: str,
        tracked_metadata: Mapping[str, Any],
    ) -> tuple[Path, Path | None, Path]:
        """Locate the image output in a worker thread: create directories, clean up this task's staging, register untracked old files.

        在工作线程里定位图片输出：建目录、清理本任务 staging、登记未入账的旧文件。"""

        output_path = self._get_output_path(resource_type, resource_id)
        self._ensure_parent_dir(output_path)
        staged_output_path, backend_output_path = self._prepare_image_output(
            output_path,
            formal_output=formal_output,
            task_id=task_id,
        )
        if staged_output_path is None and output_path.exists():
            self.versions.ensure_current_tracked(
                resource_type=resource_type,
                resource_id=resource_id,
                current_file=output_path,
                prompt=prompt,
                **tracked_metadata,
            )
        return output_path, staged_output_path, backend_output_path

    def _prepare_video_target_sync(self, resource_type: str, resource_id: str, *, track_existing: bool) -> Path:
        """Locate the video output and create directories in a worker thread; with ``track_existing``, register untracked old files.

        The prompt / duration / provider options here all belong to the new request about to be made, and must not be written
        onto a legacy current of unknown origin; otherwise after the new artifact is rejected and rolled back, the old video
        would pose as the new request's tier and later be wrongly treated as a reusable final video. Unknown facts stay
        unknown; the new artifact registers the full request metadata at add_version.

        在工作线程里定位视频输出并建目录；``track_existing`` 时登记未入账的旧文件。

        这里的 prompt / duration / provider 选项都属于即将发起的新请求，不能写到来源不明的
        legacy current 上；否则新产物被拒绝回滚后，旧视频会冒充新请求档位，后续被错误地当作
        可复用成片。未知事实保持未知，新产物在 add_version 时再登记完整请求元数据。
        """

        output_path = self._get_output_path(resource_type, resource_id)
        self._ensure_parent_dir(output_path)
        if track_existing and output_path.exists():
            self.versions.ensure_current_tracked(
                resource_type=resource_type,
                resource_id=resource_id,
                current_file=output_path,
                prompt="",
            )
        return output_path

    def _prepare_audio_staging_sync(self, resource_id: str) -> tuple[Path, Path]:
        """Locate the audio output in a worker thread, and reserve a unique staging filename in the same directory (leaving no placeholder file).

        在工作线程里定位音频输出，并保留同目录唯一的 staging 文件名（不留占位文件）。"""

        output_path = self._get_output_path("audio", resource_id)
        self._ensure_parent_dir(output_path)
        fd, staged_name = tempfile.mkstemp(
            prefix=f".{output_path.stem}.",
            suffix=output_path.suffix,
            dir=output_path.parent,
        )
        os.close(fd)
        staged_path = Path(staged_name)
        staged_path.unlink()  # hard-delete: temp -- mkstemp placeholder file / mkstemp 占位文件
        return output_path, staged_path

    def _commit_video_output_version_sync(
        self,
        *,
        resource_type: str,
        resource_id: str,
        prompt: str,
        output_path: Path,
        staged_output_path: Path | None,
        duration_seconds: int,
        version_metadata: Mapping[str, Any],
        commit_formal_output: Callable[[Path, Path, int, Mapping[str, Any]], PaidVersionCommit] | None = None,
    ) -> PaidVersionCommit:
        """Register a normal or resumed video through one identical formal-output commit path."""

        if staged_output_path is None:
            return PaidVersionCommit(
                version=self.versions.add_version(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    prompt=prompt,
                    source_file=output_path,
                    duration_seconds=duration_seconds,
                    **version_metadata,
                ),
                selected=True,
            )
        try:
            if commit_formal_output is not None:
                return commit_formal_output(
                    staged_output_path,
                    output_path,
                    duration_seconds,
                    version_metadata,
                )
            self.versions.commit_staged_paid_version(
                resource_type=resource_type,
                resource_id=resource_id,
                prompt=prompt,
                staged_file=staged_output_path,
                current_file=output_path,
                select_current=False,
                duration_seconds=duration_seconds,
                **version_metadata,
            )
            raise RuntimeError("formal video output requires an artifact commit callback")
        finally:
            staged_output_path.unlink(missing_ok=True)  # hard-delete: temp -- staged output after commit / 提交后的暂存输出

    async def _commit_video_output_version(
        self,
        *,
        resource_type: str,
        resource_id: str,
        prompt: str,
        output_path: Path,
        staged_output_path: Path | None,
        duration_seconds: int,
        version_metadata: Mapping[str, Any],
        commit_formal_output: Callable[[Path, Path, int, Mapping[str, Any]], PaidVersionCommit] | None = None,
    ) -> PaidVersionCommit:
        """Run the shared normal/resume disk transaction without blocking the event loop.

        The sync transaction cannot be interrupted safely: if a process-level
        shutdown cancels this coroutine, wait for its thread before propagating so
        the committed selection is never left half-written.
        """

        return await run_noninterruptible_sync(
            self._commit_video_output_version_sync,
            resource_type=resource_type,
            resource_id=resource_id,
            prompt=prompt,
            output_path=output_path,
            staged_output_path=staged_output_path,
            duration_seconds=duration_seconds,
            version_metadata=version_metadata,
            commit_formal_output=commit_formal_output,
        )

    async def _prepare_formal_video_commit(
        self,
        *,
        resource_type: str,
        resource_id: str,
        prompt: str,
        output_path: Path,
        staged_output_path: Path | None,
        duration_seconds: int,
        version_metadata: Mapping[str, Any],
        before_formal_commit: Callable[[Path, int, Mapping[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """Run paid-output validation without losing history when validation aborts."""

        if before_formal_commit is None:
            return
        if staged_output_path is None:
            raise ValueError("before_formal_commit requires formal video output")
        try:
            await before_formal_commit(staged_output_path, duration_seconds, version_metadata)
        except (Exception, asyncio.CancelledError) as failure:
            try:
                await run_noninterruptible_sync(
                    self.versions.commit_staged_paid_version,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    prompt=prompt,
                    staged_file=staged_output_path,
                    current_file=output_path,
                    select_current=False,
                    duration_seconds=duration_seconds,
                    **version_metadata,
                )
            except (Exception, asyncio.CancelledError) as archive_failure:
                failure.add_note(f"paid video history archival also failed: {archive_failure}")
            raise

    async def _trash_if_target_deleted(
        self,
        *,
        resource_type: str,
        resource_id: str,
        staged_file: Path | None,
        task_id: str | None,
        target: tuple[str, str] | None = None,
    ) -> None:
        """After cost settlement and before the final commit, recheck that a queue task's target is still in the project; if
        deleted, move the artifact to the trash and raise.

        See :mod:`lib.generation.deleted_target`. Direct calls without a task identity or staged file are not rechecked.

        队列任务的产物在费用结算后、正式提交前复核目标仍在项目中，已删除时进回收站并抛出。

        见 :mod:`lib.generation.deleted_target`。没有任务身份或暂存文件的直接调用不复核。
        """
        if task_id is None or staged_file is None:
            return
        await run_noninterruptible_sync(
            trash_result_if_target_deleted,
            self.project_path,
            resource_type=resource_type,
            resource_id=resource_id,
            staged_file=staged_file,
            target=target,
        )

    def _get_output_path(self, resource_type: str, resource_id: str) -> Path:
        """
        Infer the output path from the resource type and ID

        Args:
            resource_type: resource type (storyboards, videos, characters, clues)
            resource_id: resource ID (E1S01, 姜月茴, 玉佩)

        Returns:
            absolute path of the output file

        根据资源类型和 ID 推断输出路径

        Args:
            resource_type: 资源类型 (storyboards, videos, characters, clues)
            resource_id: 资源 ID (E1S01, 姜月茴, 玉佩)

        Returns:
            输出文件的绝对路径
        """
        relative_path = resource_relative_path(resource_type, resource_id)
        try:
            return safe_join(self.project_path, relative_path)
        except PathTraversalError as exc:
            raise ValueError(f"Invalid resource ID: '{resource_id}'") from exc

    def _ensure_parent_dir(self, output_path: Path) -> None:
        """Ensure the output directory exists / 确保输出目录存在"""
        output_path.parent.mkdir(parents=True, exist_ok=True)

    async def _reference_limits(self, provider_id: str | None) -> "PayloadLimits":
        """Resolve the PayloadLimits for reference upload copies.

        Without config_resolver (zero-config scenario) -> conservative generic defaults, no DB access. All other cases go to
        ConfigResolver.reference_payload_limits: when provider_id is None it short-circuits internally to the service-layer
        defaults (also without touching the DB), avoiding a second default source in this layer that could drift from the config layer.

        解析参考上传副本的 PayloadLimits。

        无 config_resolver（零配置场景）→ 保守通用默认，不触 DB。其余情形统一交给
        ConfigResolver.reference_payload_limits：provider_id 为 None 时它内部短路返回 service
        层默认（同样不触 DB），避免在本层再引入第二份默认来源、与配置层漂移。
        """
        from lib.references.reference_compression import PayloadLimits

        if self._config is None:
            return PayloadLimits()
        total, single = await self._config.reference_payload_limits(provider_id)
        return PayloadLimits(total_max_bytes=total, single_max_bytes=single)

    async def _run_with_reference_compression(
        self,
        *,
        specs: "list[ReferenceSpec]",
        provider_id: str | None,
        build_and_call: "Callable[[list[CompressedRef]], Awaitable[Any]]",
        before_submit: Callable[[], Awaitable[None]] | None = None,
        provider_resubmit_is_unsafe: Callable[[], bool] | None = None,
    ) -> Any:
        """Apply proactive compression + precheck downgrade + passive 413 fallback to reference upload copies, then call the backend.

        build_and_call receives the CompressedRef list merged in original order (1:1, count preserved, including pass-through
        items), builds the provider request and returns the backend.generate coroutine. Without reference images it calls once
        directly with no downgrade (a 413 on T2I/T2V has nothing to do with reference images and should not be turned into a floor error).

        对参考上传副本做主动压缩 + 预检降档 + 被动 413 兜底，再调用 backend。

        build_and_call 接收按原序合并好的 CompressedRef 列表（1:1 保数、含透传项），构造 provider
        请求并返回 backend.generate 协程。无参考图时直接单次调用、不做降档（T2I/T2V 的 413 与
        参考图无关，不应被误转成 floor 错误）。
        """
        from lib.references.reference_compression import (
            LADDER_STEPS,
            ReferencePayloadFloorError,
            compressed_reference_payload,
        )

        submit_boundary_crossed = False

        async def _call_once(compressed: "list[CompressedRef]") -> Any:
            nonlocal submit_boundary_crossed
            if not submit_boundary_crossed:
                # Mark before awaiting so cancellation/failure cannot cause a later retry to rewrite an immutable
                # checkpoint. The caller must abort the whole request when this hook fails.
                submit_boundary_crossed = True
                if before_submit is not None:
                    await before_submit()
            return await build_and_call(compressed)

        if not specs:
            return await _call_once([])

        limits = await self._reference_limits(provider_id)
        step = 0
        while True:
            # Compression is CPU-heavy PIL decode/encode + disk writes, and cleaning the temporary directory walks and deletes, so both
            # run in threads, avoiding blocking the worker event loop (heartbeat / SSE / other concurrent channels). The context manager
            # is driven manually: register as soon as __enter__ finishes in the thread, since if a cancel lands during compression the
            # return value is lost, and finally cleans up by the registration. Cleanup is non-interruptible: once the provider has
            # returned, a cancel arriving during cleanup must not lose the paid result. The precheck floor is raised inside __enter__,
            # when the temporary directory has not been created yet (select_ladder_step precedes disk writes), so nothing is registered and it bubbles up directly.
            # 压缩是 CPU 密集的 PIL 解码/编码 + 写盘，清理临时目录要遍历删除，两头都放进线程，
            # 避免阻塞 worker 事件循环（心跳 / SSE / 另一并发通道）。手动驱动上下文管理器：
            # __enter__ 在线程里一完成就登记，取消落在压缩那一程时返回值会丢，finally 按登记清理。
            # 清理不可打断：供应商已返回时，清理期间到达的取消不能丢掉付费结果。
            # 预检 floor 在 __enter__ 内抛出，此时临时目录尚未创建（select_ladder_step 先于写盘），
            # 不登记、直接冒泡。
            cm = compressed_reference_payload(specs, limits=limits, start_step=step)
            entered: list[tuple[int, list[CompressedRef]]] = []

            def _enter(cm=cm, entered=entered) -> None:
                entered.append(cm.__enter__())

            try:
                await run_sync_transaction(_enter)
                landed, compressed = entered[0]
                try:
                    return await _call_once(compressed)
                except Exception as e:
                    if not _is_413(e) or (provider_resubmit_is_unsafe is not None and provider_resubmit_is_unsafe()):
                        raise
                    # Continue the ladder from the "actually landed step landed", not the requested step -- the proactive precheck may already
                    # have downgraded to landed>step because of the byte limit, and landed+1 is needed to be strictly smaller, keeping downgrades monotonic.
                    # 从「实际落定档位 landed」续档，而非请求值 step——主动预检可能已因字节超限
                    # 降到 landed>step，必须 landed+1 才严格更小、保证降档单调。
                    if landed < LADDER_STEPS:
                        step = landed + 1
                        continue
                    # Still 413 at the floor -> exhausted -> user-visible hard error (keeping the 413 cause)
                    # 已在地板仍 413 → 耗尽 → 用户可见硬错误（保 413 cause）
                    raise ReferencePayloadFloorError() from e
            finally:
                if entered:
                    await run_noninterruptible_sync(cm.__exit__, None, None, None)

    async def generate_image_async(
        self,
        prompt: str,
        resource_type: str,
        resource_id: str,
        reference_images=None,
        aspect_ratio: str = "9:16",
        image_size: str | None = None,
        formal_output: bool = False,
        task_id: str | None = None,
        commit_formal_output: Callable[[Path, Path, Mapping[str, Any]], int] | None = None,
        before_submit: Callable[[], Awaitable[None]] | None = None,
        **version_metadata,
    ) -> tuple[Path, int]:
        """
        Generate an image asynchronously (with automatic version management)

        Args:
            prompt: image generation prompt
            resource_type: resource type (storyboards, characters, clues)
            resource_id: resource ID (E1S01, 姜月茴, 玉佩)
            reference_images: list of reference images
            aspect_ratio: aspect ratio, default 9:16 (portrait)
            image_size: image size, not passed by default (decided by the backend/SDK)
            **version_metadata: extra metadata

        Returns:
            (output_path, version_number) tuple

        异步生成图片（带自动版本管理）

        Args:
            prompt: 图片生成提示词
            resource_type: 资源类型 (storyboards, characters, clues)
            resource_id: 资源 ID (E1S01, 姜月茴, 玉佩)
            reference_images: 参考图片列表
            aspect_ratio: 宽高比，默认 9:16（竖屏）
            image_size: 图片尺寸，默认不传（由 backend/SDK 决定）
            **version_metadata: 额外元数据

        Returns:
            (output_path, version_number) 元组
        """
        from lib.backends.image_backends.base import ImageGenerationRequest, ReferenceImage

        if formal_output and commit_formal_output is None:
            raise ValueError("formal image output requires an artifact commit callback")
        # 1. Locate the output and clean up this task's leftover staging; for non-final output, if it already exists, make sure the old file is recorded.
        # 1. 定位输出、清理本任务残留的 staging；非正式输出时若已存在，确保旧文件被记录。
        output_path, staged_output_path, backend_output_path = await run_sync_transaction(
            self._prepare_image_target_sync,
            resource_type=resource_type,
            resource_id=resource_id,
            formal_output=formal_output,
            task_id=task_id,
            prompt=prompt,
            tracked_metadata={"aspect_ratio": aspect_ratio, **version_metadata},
        )

        if self._image_backend is None:
            raise RuntimeError("image_backend not configured")

        # Normalise reference_images first; unsupported types like PIL are dropped here, so the capability decision must be based
        # on the normalised result, avoiding "passed invalid reference images" being misjudged as I2I and then falling through to
        # a T2I call, causing a false image_capability_missing_i2i.
        # 先归一化 reference_images，PIL 等不支持的类型在此被丢弃，
        # 因此 capability 判定要基于归一化后的结果，避免「传了无效引用图」被
        # 误判为 I2I 后又落到 T2I 调用，造成 image_capability_missing_i2i 误报。
        from lib.backends.image_backends.base import ImageCapability, ImageCapabilityError

        ref_images: list[ReferenceImage] = []
        if reference_images:
            for ref in reference_images:
                if isinstance(ref, dict):
                    ref_images.append(ReferenceImage(path=str(ref.get("image", ""))))
                elif hasattr(ref, "__fspath__") or isinstance(ref, (str, Path)):
                    ref_images.append(ReferenceImage(path=str(ref)))
                # Unsupported types like PIL Image are ignored / PIL Image 等不支持的类型忽略

        # Capability gating: only what the call channel implements and the model entry accepts is allowed, the same result as the
        # task type bucket candidates (lib.backends.image_capabilities). The upper resolver already filtered by the same rule when
        # picking the bucket; here it is checked against the actual request shape.
        # Capability gating：调用通道实现了、且模型条目接受的才放行，与任务类型桶的候选同一个结果
        # （lib.backends.image_capabilities）。上层 resolver 选桶时已按同一口径筛过，这里按实际请求形态把关。
        from lib.backends.image_capabilities import effective_image_capabilities
        from lib.custom_provider import is_custom_provider
        from lib.custom_provider.backends import CustomImageBackend

        # The pairing invariant guarantees provider_id is not None when backend is not None. / 成对不变量保证 backend 非 None 时 provider_id 亦非 None。
        provider_id = cast(str, self._image_provider_id)
        if isinstance(self._image_backend, CustomImageBackend):
            # Custom providers: the factory has already injected the model-fact side per model row (entry ⊕ capability overrides; ComfyUI endpoints add no constraints).
            # 自定义供应商：工厂已按模型行注入模型事实一侧（条目 ⊕ 能力覆盖，ComfyUI 端点不加约束）。
            available = self._image_backend.effective_capabilities
        else:
            available = effective_image_capabilities(
                self._image_backend.capabilities,
                model_id=self._image_backend.model,
                builtin_provider=None if is_custom_provider(provider_id) else provider_id,
            )
        needed = ImageCapability.IMAGE_TO_IMAGE if ref_images else ImageCapability.TEXT_TO_IMAGE
        if needed not in available:
            raise ImageCapabilityError(
                "image_capability_missing_i2i"
                if needed == ImageCapability.IMAGE_TO_IMAGE
                else "image_capability_missing_t2i",
                provider=self._image_backend.name,
                model=self._image_backend.model,
            )

        # 2. Accounting bracket: entering writes pending, success hands the backend result object via call.success(result),
        #    Exception is flipped to failed automatically and re-raised, CancelledError is settled as cancelled and re-raised.
        # 2. 记账括号：进入落 pending，成功以 call.success(result) 递交 backend 结果对象，
        #    Exception 自动翻 failed 后重抛，CancelledError 结算 cancelled 后重抛。
        async with _remove_staged_output_on_error(staged_output_path):
            async with self.ledger.record(
                project_name=self.project_name,
                call_type="image",
                model=self._image_backend.model,
                prompt=prompt,
                resolution=image_size,
                aspect_ratio=aspect_ratio,
                # The accounting provider is the resolution layer's provider_id; the pairing invariant guarantees provider_id is not None when backend is not None.
                # 记账 provider 取解析层 provider_id；成对不变量保证 backend 非 None 时 provider_id 亦非 None。
                provider=cast(str, self._image_provider_id),
                user_id=self._user_id,
                segment_id=segment_id_for("image", resource_type, resource_id),
                output_path=_project_relative_path(self.project_path, output_path),
                task_id=task_id,
                purpose=CallPurpose.GENERATION_TASK,
                inputs=_ledger_inputs(
                    reference_images=[
                        {"path": rel, "label": None, "role": "array"}
                        for ref in ref_images
                        if (rel := _project_relative_path(self.project_path, ref.path)) is not None
                    ]
                ),
            ) as call:
                from lib.references.reference_compression import ReferenceSpec, RefRole

                image_backend = self._image_backend
                # All image reference images take the array role (full baseline + downgrade ladder + byte budget).
                # 所有图像参考图都走数组角色（完整基线 + 降档梯子 + 字节预算）。
                specs = [ReferenceSpec(source=Path(r.path), role=RefRole.ARRAY) for r in ref_images]

                def _call_image(compressed: "list[CompressedRef]"):
                    return image_backend.generate(
                        ImageGenerationRequest(
                            prompt=prompt,
                            output_path=backend_output_path,
                            reference_images=[ReferenceImage(path=str(c.path)) for c in compressed],
                            aspect_ratio=aspect_ratio,
                            image_size=image_size,
                            project_name=self.project_name,
                        )
                    )

                result = await self._run_with_reference_compression(
                    specs=specs,
                    provider_id=self._image_provider_id,
                    build_and_call=_call_image,
                    before_submit=before_submit,
                )
                call.success(result)

            await self._trash_if_target_deleted(
                resource_type=resource_type,
                resource_id=resource_id,
                staged_file=staged_output_path,
                task_id=task_id,
            )
            metadata: dict[str, Any] = {
                "aspect_ratio": aspect_ratio,
                **version_metadata,
                **_generation_link(task_id, call.call_id),
            }
            if staged_output_path is None:
                new_version = await run_noninterruptible_sync(
                    self.versions.add_version,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    prompt=prompt,
                    source_file=output_path,
                    **metadata,
                )
            else:
                assert commit_formal_output is not None
                new_version = await run_noninterruptible_sync(
                    _commit_formal_image_output,
                    commit_formal_output,
                    staged_output_path,
                    output_path,
                    metadata,
                )

        return output_path, new_version

    async def generate_audio_async(
        self,
        text: str,
        resource_id: str,
        voice: str,
        language_type: str = "Chinese",
        speed: float | None = None,
        task_id: str | None = None,
        before_submit: Callable[[], Awaitable[None]] | None = None,
        before_commit: Callable[[Path], Awaitable[None]] | None = None,
        commit_staged: Callable[[Path, Path], int | PaidVersionCommit] | None = None,
        target_resource: tuple[str, str] | None = None,
        **version_metadata,
    ) -> tuple[Path, int]:
        """
        Synthesise speech asynchronously (with automatic version management)

        Unlike images/video, the TTS backend is a synchronous call (no submit-poll-resume), so the logic is simplest.

        Args:
            text: text to synthesise (narration source text)
            resource_id: resource ID (segment, e.g. E1S01)
            voice: voice (e.g. Cherry)
            language_type: language, default Chinese
            speed: reserved speech rate (ignored by synchronous models)
            task_id: the generation task this synthesis belongs to; accounting points back to the task with it, empty for direct calls (no queue task)
            before_submit: async admission hook run once right before the first provider submission
            target_resource: the resource the artifact belongs to (resource type, resource ID), defaulting to the artifact itself; if it has been deleted the artifact goes to the trash
            **version_metadata: extra metadata

        Returns:
            (output_path, version_number) tuple

        异步合成语音（带自动版本管理）

        与图片/视频不同，TTS 后端是同步调用（无 submit-poll-resume），逻辑最简。

        Args:
            text: 待合成文本（旁白原文）
            resource_id: 资源 ID（segment，如 E1S01）
            voice: 音色（如 Cherry）
            language_type: 语种，默认 Chinese
            speed: 语速预留（同步模型忽略）
            task_id: 本次合成所属的生成任务；记账据此回指任务，直接调用（无队列任务）留空
            before_submit: 首次 provider 提交紧前执行一次的异步准入钩子
            target_resource: 产物所属的资源（资源类型, 资源 ID），缺省为产物自身；它已被删除时产物进回收站
            **version_metadata: 额外元数据

        Returns:
            (output_path, version_number) 元组
        """
        from lib.backends.audio_backends.base import AudioSynthesisRequest

        resource_type = "audio"
        if self._audio_backend is None:
            raise RuntimeError("audio_backend not configured")

        # The backend only writes a unique staging file in the same directory. Only after synchronous synthesis fully succeeds and
        # really produces a regular file is it promoted to canonical with an atomic replace; failure or cancellation never touches the old paid audio.
        # 后端只写同目录唯一 staging 文件。只有同步合成完整成功且确实产出普通文件后，
        # 才以原子 replace 提升为 canonical；失败或取消均不会触碰旧付费音频。
        output_path, staged_path = await run_sync_transaction(self._prepare_audio_staging_sync, resource_id)

        # The semantic conversion of audio synthesis character count -> billed tokens has moved into the ledger union dispatch (_settlement_from_result);
        # here the backend result object is only handed to call.success.
        # audio 合成字符数 → 计费 token 的语义转写已收进 ledger union 分发（_settlement_from_result），
        # 此处仅把 backend 结果对象递交给 call.success。
        try:
            async with self.ledger.record(
                project_name=self.project_name,
                call_type="audio",
                model=self._audio_backend.model,
                prompt=text,
                # The accounting provider is the resolution layer's provider_id; the pairing invariant guarantees provider_id is not None when backend is not None.
                # 记账 provider 取解析层 provider_id；成对不变量保证 backend 非 None 时 provider_id 亦非 None。
                provider=cast(str, self._audio_provider_id),
                user_id=self._user_id,
                segment_id=segment_id_for("audio", resource_type, resource_id),
                output_path=_project_relative_path(self.project_path, output_path),
                task_id=task_id,
                purpose=CallPurpose.GENERATION_TASK,
                inputs=_ledger_inputs(
                    voice=voice,
                    parameters=_ledger_inputs(language_type=language_type, speed=speed),
                ),
            ) as call:
                request = AudioSynthesisRequest(
                    text=text,
                    output_path=staged_path,
                    voice=voice,
                    language_type=language_type,
                    speed=speed,
                )
                if before_submit is not None:
                    await before_submit()
                result = await self._audio_backend.synthesize(request)
                # The provider has returned: checking the artifact introduces no new cancellation point, so the paid result isn't left unrecorded as success.
                # 供应商已返回：核对产物不引入新的取消点，免得付费结果没记成 success。
                if not await run_noninterruptible_sync(staged_path.is_file):
                    raise RuntimeError("audio backend completed without a regular output file")
                call.success(result)

            await self._trash_if_target_deleted(
                resource_type=resource_type,
                resource_id=resource_id,
                staged_file=staged_path,
                task_id=task_id,
                target=target_resource,
            )
            if before_commit is not None:
                await before_commit(staged_path)
            commit = commit_staged
            selected_current = True
            if commit is None:
                version = await run_noninterruptible_sync(
                    self.versions.commit_staged_version,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    prompt=text,
                    staged_file=staged_path,
                    current_file=output_path,
                    **version_metadata,
                    **_generation_link(task_id, call.call_id),
                )
            else:
                committed = await run_noninterruptible_sync(commit, staged_path, output_path)
                if isinstance(committed, PaidVersionCommit):
                    version = committed.version
                    selected_current = committed.selected
                else:
                    version = committed
            if selected_current and not await run_noninterruptible_sync(output_path.is_file):
                raise RuntimeError("audio commit completed without a regular formal output file")
            return output_path, version
        finally:
            await _discard_staged_output(staged_path)

    async def generate_video_async(
        self,
        prompt: str,
        resource_type: str,
        resource_id: str,
        start_image: str | Path | Image.Image | None = None,
        end_image: Path | None = None,
        reference_images: list[Path] | None = None,
        reference_audio_files: list[Path] | None = None,
        reference_audio_targets: list[int] | None = None,
        aspect_ratio: str = "9:16",
        duration_seconds: str | int = "8",
        resolution: str | None = None,
        poll_timeout_seconds: int = DEFAULT_VIDEO_POLL_TIMEOUT_SECONDS,
        task_id: str | None = None,
        before_submit: Callable[[], Awaitable[Mapping[str, object] | None]] | None = None,
        formal_output: bool = False,
        before_formal_commit: Callable[[Path, int, Mapping[str, Any]], Awaitable[None]] | None = None,
        commit_formal_output: Callable[[Path, Path, int, Mapping[str, Any]], PaidVersionCommit] | None = None,
        warnings: list[dict[str, Any]] | None = None,
        **version_metadata,
    ) -> tuple[Path, int, Any, str | None]:
        """
        Generate a video asynchronously (with automatic version management)

        Args:
            prompt: video generation prompt (including the uniformly textualised negative prompt, assembled upstream by prompt_builders)
            resource_type: resource type (videos)
            resource_id: resource ID (E1S01)
            start_image: start frame image (image-to-video mode)
            end_image: end frame image (first_last mode)
            reference_images: list of reference images (multi-reference mode)
            reference_audio_files: list of reference audio (voice cloning); order is the order "audio N" is referred to in the prompt
            reference_audio_targets: same length and order as reference_audio_files; item i is the index (0-based) into
                reference_images of the image that audio corresponds to; only read when the backend declares
                reference_audio_per_image, see VideoGenerationRequest.reference_audio_targets
            aspect_ratio: aspect ratio, default 9:16 (portrait)
            duration_seconds: video duration, optionally "4", "6", "8"
            resolution: resolution, not passed by default (decided by the backend/SDK)
            before_submit: async persistence hook run once right before the first provider submission;
                its return value is merged into this version's metadata
            formal_output: write the provider artifact to a temporary file in the same directory first, and commit it together with version history on success
            warnings: warning collector held by the caller; notes produced by the backend during execution are appended in place,
                and the caller then puts them into the task's ``result.warnings``
            **version_metadata: extra metadata

        Returns:
            (output_path, version_number, video_ref, video_uri) four-tuple

        异步生成视频（带自动版本管理）

        Args:
            prompt: 视频生成提示词（含统一文本化的反向提示词，由 prompt_builders 在上游拼好）
            resource_type: 资源类型 (videos)
            resource_id: 资源 ID (E1S01)
            start_image: 起始帧图片（image-to-video 模式）
            end_image: 结束帧图片（first_last 模式）
            reference_images: 参考图片列表（multi-reference 模式）
            reference_audio_files: 参考音频列表（音色复刻），顺序即 prompt 中「音频N」的指认顺序
            reference_audio_targets: 与 reference_audio_files 等长同序，第 i 项是该段音频对应
                reference_images 的下标（0-based）；仅 backend 声明 reference_audio_per_image
                时读取，见 VideoGenerationRequest.reference_audio_targets
            aspect_ratio: 宽高比，默认 9:16（竖屏）
            duration_seconds: 视频时长，可选 "4", "6", "8"
            resolution: 分辨率，默认不传（由 backend/SDK 决定）
            before_submit: 首次 provider 提交紧前执行一次的异步持久化钩子；
                返回值并入当次版本元数据
            formal_output: 将 provider 产物先写入同目录临时文件，成功后再与版本历史一起提交
            warnings: 调用方持有的 warning 收集器；backend 执行期产生的提示就地追加进去，
                随后由调用方落进任务 ``result.warnings``
            **version_metadata: 额外元数据

        Returns:
            (output_path, version_number, video_ref, video_uri) 四元组
        """
        # Normalise duration to int first: upstream may pass a float string like "8.0", and a direct int("8.0") raises ValueError,
        # taking the fallback branch and silently dropping the real value ("10.0" would be swallowed into 8). float() then int() keeps the meaning.
        # Moved before all ensure_current_tracked / add_version / VideoGenerationRequest,
        # so duration_seconds has the same type (int) in version metadata and the provider request,
        # avoiding type drift where versions.json stores a string and ApiCall stores an int.
        # 先把 duration 归一为 int：上游可能传 "8.0" 浮点字符串，直接 int("8.0") 会 ValueError
        # 走兜底分支静默掉真实值（"10.0" 会被吞成 8）。先 float() 再 int() 保留语义。
        # 提前到所有 ensure_current_tracked / add_version / VideoGenerationRequest 之前，
        # 让版本元数据与 provider 请求里的 duration_seconds 类型一致（都是 int），
        # 避免 versions.json 落字符串而 ApiCall 落 int 的类型漂移。
        try:
            duration_int = int(float(duration_seconds)) if duration_seconds else DEFAULT_PLANNED_DURATION_SECONDS
        except (ValueError, TypeError):
            duration_int = DEFAULT_PLANNED_DURATION_SECONDS

        # 1. If it already exists, make sure the old file is recorded (only old files of unknown origin are registered, see _prepare_video_target_sync).
        # 1. 若已存在，确保旧文件被记录（只登记未知来源的旧文件，见 _prepare_video_target_sync）。
        output_path = await run_sync_transaction(
            self._prepare_video_target_sync,
            resource_type,
            resource_id,
            track_existing=not formal_output,
        )

        if self._video_backend is None:
            raise RuntimeError("video_backend not configured")

        # Normalise an empty-string end_image to None: legacy/direct Python callers may use "" to mean no end frame
        # (kling _build_payload's truthiness check locks this compatibility semantic, see
        # tests/integration/lib/backends/video_backends/test_kling_video_backend.py::test_image2video_empty_end_frame_is_omitted).
        # The gating below checks emptiness with is not None, so "" left unnormalised would be misjudged as a real end frame.
        # 空串 end_image 归一为 None：遗留/直接 Python 调用者可能以 "" 表示无尾帧
        # （kling _build_payload 的真值判断锁定了这个兼容语义，见
        # tests/integration/lib/backends/video_backends/test_kling_video_backend.py::test_image2video_empty_end_frame_is_omitted）。
        # 下方 gating 用 is not None 判空，"" 若不归一会被误判成真尾帧。
        if not end_image:
            end_image = None

        model_name = self._video_backend.model

        # Capability checks and slot assembly come before the accounting bracket: a hard failure on unsupported capabilities must
        # "not charge"; raising inside the bracket also doesn't settle a charge, but leaves a failed ApiCall row; both are side-effect free, and doing it first is cleanest.
        # 能力校验与槽位组装先于记账括号：能力不被支持时硬失败要"不扣费"，
        # 在括号内抛虽也不结算，却会留一条 failed ApiCall 行；两者均无副作用，前置最干净。
        from lib.backends.backend_assembly.specs import apply_user_added_video_overrides
        from lib.backends.video_frame_slots import (
            gate_video_request,
            plan_frame_slots,
            resolve_first_frame_aspect_ratio,
            resolve_video_capabilities,
        )
        from lib.custom_provider import is_custom_provider

        # The prompt length check applies to every request (unlike end frame/reference images/reference audio, which are optional),
        # so the capability query is not triggered lazily on optional paths. The query is a synchronous pure read of the backend's declaration, with no I/O cost.
        # prompt 长度校验对每个请求都适用（不像尾帧/参考图/参考音频那样可选），故能力查询不
        # 按可选路径惰性触发。查询是纯读后端声明的同步调用，没有 I/O 开销。
        video_caps = resolve_video_capabilities(
            self._video_backend,
            service_tier=version_metadata.get("service_tier", "default"),
            resolution=resolution,
        )
        # Models added by the user under a built-in provider: overlay channel fact overrides on the tier-aware base, same rule as the planning layer (docs/adr/0104).
        # Custom provider overrides are already applied by CustomVideoBackend itself.
        # 内置供应商下用户添加的模型：在档位感知基底上叠加通道事实覆盖，与规划层同一口径（docs/adr/0104）。
        # 自定义供应商的覆盖已由 CustomVideoBackend 自己叠加。
        if self._video_provider_id is not None and not is_custom_provider(self._video_provider_id):
            video_caps = apply_user_added_video_overrides(video_caps, self._video_provider_id, model_name)
        # The total duration check needs to read audio metadata, which can only be probed at this layer (which has the file paths)
        # and then passed to the pure function gate_video_request; if probing fails (bundled ffmpeg unavailable etc.) None is
        # passed, and it skips the check by its existing degradation rule without blocking the request. Only probe when caps
        # declares a total duration constraint -- backends that don't declare it (e.g. wan2.7) need not pay an extra probe subprocess per request.
        # 总时长校验需要读音频元数据，只能在这层（拿得到文件路径）探测好再传给纯函数的
        # gate_video_request；探测失败（随包 ffmpeg 不可用等）按 None 传入，由其按既有降级口径跳过
        # 该项校验，不阻断请求。仅当 caps 声明了总时长约束才探测——未声明该约束的后端
        # （如 wan2.7）不必为每个请求多付一轮探测子进程开销。
        reference_audio_total_seconds = (
            await probe_reference_audio_total_seconds(reference_audio_files)
            if reference_audio_files and video_caps.max_reference_audio_total_seconds is not None
            else None
        )
        slot_plan = plan_frame_slots(
            start_image=start_image,
            end_image=end_image,
            reference_images=reference_images,
        )
        gate_video_request(
            caps=video_caps,
            provider=self._video_backend.name,
            model=model_name,
            prompt=prompt,
            has_image=bool(slot_plan.specs),
            has_first_frame=slot_plan.start_index is not None,
            end_image=end_image,
            reference_images=reference_images,
            reference_audio_files=reference_audio_files,
            reference_audio_total_seconds=reference_audio_total_seconds,
        )
        # Only backends declaring first_frame_ratio_adaptive_only are affected; the sent value is separate from the caller's
        # original aspect_ratio (still used by accounting and storyboard image generation), and the upstream variable is not overwritten.
        # 仅声明 first_frame_ratio_adaptive_only 的后端受影响；下发值与调用方持有的原始
        # aspect_ratio（记账、分镜图生成沿用）分离，不回写覆盖上游变量。
        request_aspect_ratio = resolve_first_frame_aspect_ratio(
            caps=video_caps,
            aspect_ratio=aspect_ratio,
            has_first_frame=slot_plan.start_index is not None,
        )

        if self._config is not None:
            configured_generate_audio = await self._config.video_generate_audio(self.project_name)
        else:
            from lib.config.resolver import ConfigResolver

            configured_generate_audio = ConfigResolver._DEFAULT_VIDEO_GENERATE_AUDIO
        effective_generate_audio = version_metadata.get("generate_audio", configured_generate_audio)

        staged_output_path, backend_output_path = await run_sync_transaction(
            self._prepare_video_output,
            output_path,
            formal_output=formal_output,
            task_id=task_id,
        )
        if before_formal_commit is not None and staged_output_path is None:
            raise ValueError("before_formal_commit requires formal video output")

        # The semantic conversion of overriding the requested duration with the actual billed video duration (result.duration_seconds)
        # has moved into the ledger union dispatch (_settlement_from_result); here only the backend result object is handed over.
        # video 实际计费时长（result.duration_seconds）覆盖请求时长的语义转写已收进 ledger union
        # 分发（_settlement_from_result），此处仅递交 backend 结果对象。
        video_ref = None
        video_uri: str | None = None
        async with (
            _remove_staged_output_on_error(staged_output_path),
            self.ledger.record(
                project_name=self.project_name,
                call_type="video",
                model=model_name,
                prompt=prompt,
                resolution=resolution,
                duration_seconds=duration_int,
                aspect_ratio=aspect_ratio,
                generate_audio=effective_generate_audio,
                # The accounting provider is the resolution layer's provider_id; the pairing invariant guarantees provider_id is not None when backend is not None.
                # 记账 provider 取解析层 provider_id；成对不变量保证 backend 非 None 时 provider_id 亦非 None。
                provider=cast(str, self._video_provider_id),
                user_id=self._user_id,
                segment_id=segment_id_for("video", resource_type, resource_id),
                output_path=_project_relative_path(self.project_path, output_path),
                task_id=task_id,
                purpose=CallPurpose.GENERATION_TASK,
                inputs=_ledger_inputs(
                    reference_images=[
                        {"path": rel, "label": None, "role": "array"}
                        for ref in (reference_images or [])
                        if (rel := _project_relative_path(self.project_path, ref)) is not None
                    ],
                    start_image=_project_relative_path(self.project_path, start_image),
                    end_image=_project_relative_path(self.project_path, end_image),
                    reference_audio=[
                        rel
                        for audio in (reference_audio_files or [])
                        if (rel := _project_relative_path(self.project_path, audio)) is not None
                    ],
                    parameters=_ledger_inputs(service_tier=version_metadata.get("service_tier")),
                ),
            ) as call,
        ):
            from arcreel_market_core.video_backend_contract import VideoGenerationRequest
            from lib.backends.video_backends.registry import backend_metered_usage

            video_backend = self._video_backend
            # FRAME (start/end frames, never downsized) + ARRAY (reference array, full ladder) are organised into specs by known
            # position (see lib/backends/video_frame_slots.py), and after compression restored by index into the three request fields.
            # FRAME（start/end 帧，永不缩尺寸）+ ARRAY（参考数组，完整梯子）按已知序位组织成
            # specs（见 lib/backends/video_frame_slots.py），压缩后按 index 还原回三个请求字段。
            specs = slot_plan.specs
            start_spec_idx = slot_plan.start_index
            end_spec_idx = slot_plan.end_index
            ref_start_idx = slot_plan.reference_start_index
            provider_resubmit_unsafe = False
            # The last request actually sent: compression downgrade retries rebuild the request, and metering takes the one finally sent.
            # 最后一次真正发出的请求：压缩降档重试会重建请求，计量按最终发出的那份取。
            sent_request: list[VideoGenerationRequest] = []

            def _mark_provider_resubmit_unsafe() -> None:
                nonlocal provider_resubmit_unsafe
                provider_resubmit_unsafe = True

            def _call_video(compressed: "list[CompressedRef]"):
                start_arg = compressed[start_spec_idx].path if start_spec_idx is not None else None
                end_arg = compressed[end_spec_idx].path if end_spec_idx is not None else None
                # Array reference images are always at the tail of specs (appended after start/end), so [ref_start_idx:] takes them exactly;
                # with no compressible array items fall back to the original reference_images (keeping None / [] semantics).
                # 数组参考图恒在 specs 末段（append start/end 之后），故 [ref_start_idx:] 精确取它们；
                # 无可压缩数组项时回落原 reference_images（保留 None / [] 语义）。
                ref_arg = (
                    [c.path for c in compressed[ref_start_idx:]] if ref_start_idx is not None else reference_images
                )
                request = VideoGenerationRequest(
                    prompt=prompt,
                    output_path=backend_output_path,
                    aspect_ratio=request_aspect_ratio,
                    duration_seconds=duration_int,
                    resolution=resolution,
                    start_image=start_arg,
                    end_image=end_arg,
                    reference_images=ref_arg,
                    # Audio doesn't go into the compressor (specs only take images), so the original list is passed through directly: order
                    # is the order "audio N" is referred to in the prompt, and any reordering would put character A's voice onto character B.
                    # 音频不进压缩器（specs 只收图片），故直接透传原列表：顺序即 prompt
                    # 「音频N」的指认顺序，任何重排都会把 A 角色的音色安到 B 角色头上。
                    reference_audio_files=reference_audio_files,
                    # Array reference image compression preserves order (count unchanged, no reordering), so targets computed by the caller
                    # from indexes of the uncompressed reference_images are equally valid for the compressed ref_arg.
                    # 数组参考图压缩保序（不改数量、不重排），故调用方按未压缩的
                    # reference_images 下标算出的 targets 对压缩后的 ref_arg 同样有效。
                    reference_audio_targets=reference_audio_targets,
                    generate_audio=effective_generate_audio,
                    poll_timeout_seconds=poll_timeout_seconds,
                    project_name=self.project_name,
                    task_id=task_id,
                    on_provider_resubmit_unsafe=_mark_provider_resubmit_unsafe,
                    on_provider_response=lambda _stage, body: self.ledger.record_provider_response(
                        call_id=call.call_id, body=body
                    ),
                    service_tier=version_metadata.get("service_tier", "default"),
                    seed=version_metadata.get("seed"),
                    video_capabilities=video_caps,
                )
                sent_request[:] = [request]
                return video_backend.generate(request)

            async def _before_first_submit() -> None:
                if before_submit is not None:
                    checkpoint_metadata = await before_submit()
                    if checkpoint_metadata is not None:
                        version_metadata.update(checkpoint_metadata)

            try:
                result = await self._run_with_reference_compression(
                    specs=specs,
                    provider_id=self._video_provider_id,
                    build_and_call=_call_video,
                    before_submit=_before_first_submit if before_submit is not None else None,
                    provider_resubmit_is_unsafe=lambda: provider_resubmit_unsafe,
                )
            except BaseException:
                await _discard_staged_output(staged_output_path)
                raise
            video_uri = result.video_uri
            _merge_result_provenance(version_metadata, result)
            _collect_result_warnings(warnings, result)
            call.success(result, usage=backend_metered_usage(video_backend, sent_request[0], result))
            version_metadata.update(_generation_link(task_id, call.call_id))

        await self._trash_if_target_deleted(
            resource_type=resource_type,
            resource_id=resource_id,
            staged_file=staged_output_path,
            task_id=task_id,
        )
        await self._prepare_formal_video_commit(
            resource_type=resource_type,
            resource_id=resource_id,
            prompt=prompt,
            output_path=output_path,
            staged_output_path=staged_output_path,
            duration_seconds=duration_int,
            version_metadata=version_metadata,
            before_formal_commit=before_formal_commit,
        )

        # 5. Record the new version / 5. 记录新版本
        committed = await self._commit_video_output_version(
            resource_type=resource_type,
            resource_id=resource_id,
            prompt=prompt,
            output_path=output_path,
            staged_output_path=staged_output_path,
            duration_seconds=duration_int,
            version_metadata=version_metadata,
            commit_formal_output=commit_formal_output,
        )

        return output_path, committed.version, video_ref, video_uri

    async def resume_video_async(
        self,
        *,
        job_id: str,
        resource_type: str,
        resource_id: str,
        prompt: str = "",
        aspect_ratio: str = "9:16",
        duration_seconds: str | int = "8",
        resolution: str | None = None,
        task_id: str | None = None,
        submitted_base_url: str | None = None,
        poll_timeout_seconds: int = DEFAULT_VIDEO_POLL_TIMEOUT_SECONDS,
        formal_output: bool = False,
        before_formal_commit: Callable[[Path, int, Mapping[str, Any]], Awaitable[None]] | None = None,
        commit_formal_output: Callable[[Path, Path, int, Mapping[str, Any]], PaidVersionCommit] | None = None,
        warnings: list[dict[str, Any]] | None = None,
        **version_metadata,
    ) -> tuple[Path, int, Any, str | None]:
        """Resume a video job already started on the provider: call backend.resume_video instead of generate.

        Differences from generate_video_async:
        - No accounting bracket (no new pending row) -- the first submit already recorded it; ResumeExpired / crash window
          should not write another ApiCall (prevents double charging). The call row awaiting settlement is looked up by
          ``api_calls.task_id`` and flipped precisely pending -> success/failed via ledger.resume_success / resume_failed;
          if not found (no task identity or the call is already settled) logger.warning without blocking.
        - After a successful resume a new version is always recorded; with ``formal_output=True`` it first downloads to a
          temporary file in the same directory, then commits it together with version history, without overwriting current early.
        - prompt / start_image / reference_images are only used for logs/version metadata and don't affect the provider-side result.
        - ``submitted_base_url`` is a named parameter rather than version_metadata: it is the replayed domain from submission
          time, only fed to backend polling, and putting it into version metadata would pollute versions.json.

        Returns: (output_path, version_number, video_ref, video_uri) four-tuple.

        接续 provider 上已发起的 video job：调 backend.resume_video 而非 generate。

        与 generate_video_async 的差异：
        - 不开记账括号（不落新 pending 行）—— 首次 submit 已记账；ResumeExpired / crash window
          都不应再写 ApiCall（防双重扣费）。待结算的调用行按 ``api_calls.task_id`` 反查，
          经 ledger.resume_success / resume_failed 精准翻 pending → success/failed；
          反查不到（无任务身份或该调用已结算）则 logger.warning 不阻断。
        - resume 成功后总是记录新版本；``formal_output=True`` 时先下载到
          同目录临时文件，再与版本历史一起提交，不提前覆盖 current。
        - prompt / start_image / reference_images 仅用于日志/版本元数据，不影响 provider 端结果。
        - ``submitted_base_url`` 是具名参数而非 version_metadata：它是提交时域名的回放值、
          只喂给 backend 轮询，落进版本元数据会污染 versions.json。

        Returns: (output_path, version_number, video_ref, video_uri) 四元组。
        """
        output_path = await run_sync_transaction(
            self._prepare_video_target_sync,
            resource_type,
            resource_id,
            track_existing=False,
        )

        # Normalise duration to int first: upstream may pass a float string like "8.0", and a direct int("8.0") raises ValueError,
        # taking the fallback branch and silently dropping the real value ("10.0" would be swallowed into 8). float() then int() keeps the meaning.
        # Moved before VideoGenerationRequest / add_version, so duration_seconds has the same type in version metadata
        # and the provider request (both int, avoiding versions.json storing a string).
        # 先把 duration 归一为 int：上游可能传 "8.0" 浮点字符串，直接 int("8.0") 会 ValueError
        # 走兜底分支静默掉真实值（"10.0" 会被吞成 8）。先 float() 再 int() 保留语义。
        # 提前到 VideoGenerationRequest / add_version 之前，让版本元数据
        # 与 provider 请求里的 duration_seconds 类型一致（都是 int，避免 versions.json 落字符串）。
        try:
            duration_int = int(float(duration_seconds)) if duration_seconds else DEFAULT_PLANNED_DURATION_SECONDS
        except (ValueError, TypeError):
            duration_int = DEFAULT_PLANNED_DURATION_SECONDS

        if self._video_backend is None:
            raise RuntimeError("video_backend not configured")

        # The call row awaiting settlement is looked up by task identity -- the only source of truth linking tasks and calls is ``api_calls.task_id``.
        # 待结算的调用行按任务身份反查——任务与调用的关联只有 ``api_calls.task_id`` 一个真相源。
        api_call_id = await self.ledger.pending_call_id_for_task(task_id) if task_id is not None else None

        if self._config is not None:
            configured_generate_audio = await self._config.video_generate_audio(self.project_name)
        else:
            from lib.config.resolver import ConfigResolver

            configured_generate_audio = ConfigResolver._DEFAULT_VIDEO_GENERATE_AUDIO
        effective_generate_audio = version_metadata.get("generate_audio", configured_generate_audio)

        staged_output_path, backend_output_path = await run_sync_transaction(
            self._prepare_video_output,
            output_path,
            formal_output=formal_output,
            task_id=task_id,
        )
        if before_formal_commit is not None and staged_output_path is None:
            raise ValueError("before_formal_commit requires formal video output")

        from arcreel_market_core.video_backend_contract import ResumeExpiredError, VideoGenerationRequest
        from lib.backends.video_backends.registry import backend_metered_usage

        request = VideoGenerationRequest(
            prompt=prompt,
            output_path=backend_output_path,
            aspect_ratio=aspect_ratio,
            duration_seconds=duration_int,
            resolution=resolution,
            generate_audio=effective_generate_audio,
            poll_timeout_seconds=poll_timeout_seconds,
            project_name=self.project_name,
            task_id=task_id,
            on_provider_response=(
                (lambda _stage, body: self.ledger.record_provider_response(call_id=api_call_id, body=body))
                if api_call_id is not None
                else None
            ),
            service_tier=version_metadata.get("service_tier", "default"),
            seed=version_metadata.get("seed"),
            submitted_base_url=submitted_base_url,
        )

        try:
            result = await self._video_backend.resume_video(job_id, request)
        except ResumeExpiredError as exc:
            # Flip the pending ApiCall to failed rather than leaving it pending: keeps /api/v1/usage reports from piling up rows with
            # no terminal state; cost_amount=0 adds no charge (resume doesn't charge again, per the "never actively charge" red line).
            # The expiry exception is passed to back-accounting too: the failure text for this row is only available here, and without it only a bare "failed" would remain.
            # When finalize fails the exception is not swallowed, letting the worker's finally take the mark_failed fallback, so the
            # ApiCall isn't stuck in pending forever, leaving gaps in usage reports/back-accounting.
            # Pending ApiCall 翻 failed 而不是留 pending：让 /api/v1/usage 报表不堆积无终态行；
            # cost_amount=0 不增加计费（resume 不重扣，符合 "不主动扣费" 红线）。
            # 过期异常一并交给补账：这条行的失败原文只有这里拿得到，不传就只剩一个裸「失败」。
            # finalize 失败时不吞异常，让 worker finally 走 mark_failed 兜底，避免 ApiCall
            # 永久卡 pending 导致 usage 报表/补账缺口。
            async with _remove_staged_output_on_error(staged_output_path):
                if api_call_id is not None:
                    await self.ledger.resume_failed(call_id=api_call_id, failure=exc)
                raise
        except asyncio.CancelledError:
            await _discard_staged_output(staged_output_path)
            raise
        except Exception:
            logger.exception("resume 失败 (video) task_id=%s job_id=%s", task_id, job_id)
            await _discard_staged_output(staged_output_path)
            raise

        video_ref = None
        video_uri = result.video_uri
        # Same handling as the first run: the version metadata written by the same task before and after a restart must be the
        # same, otherwise "which seed did this version use" would differ depending on when the process happened to restart.
        # 与首跑同一处置：同一笔任务在重启前后落下的版本元数据必须是同一份，否则「这一版用的
        # 哪个种子」会因为进程什么时候重启过而不一样。
        _merge_result_provenance(version_metadata, result)
        _collect_result_warnings(warnings, result)

        # Resume succeeded: flip precisely pending -> success. ledger.resume_success takes the backend result object, doing the
        # union dispatch from the same source as the video channel's success branch (usage_tokens / generate_audio / actual billed
        # duration); cost is priced by the repo from the metering the call channel reports for the resume request -- equivalent to
        # generate-path accounting, so a generated video is never permanently missing from the ledger. Exceptions from finalize
        # itself are not swallowed, left to the worker's finally fallback; WHERE status='pending' guards idempotency.
        # Resume 成功：精准翻 pending → success。ledger.resume_success 收 backend 结果对象，
        # 与视频通道成功分支同源做 union 分发（usage_tokens / generate_audio / 实际计费时长），
        # cost 由 repo 按调用通道对续跑请求上报的计量计价——与 generate 路径记账等价，避免视频已生成但账本
        # 永久漏记。finalize 自身异常不吞，交 worker finally 兜底；WHERE status='pending' 保护幂等性。
        if api_call_id is not None:
            async with _remove_staged_output_on_error(staged_output_path):
                await self.ledger.resume_success(
                    call_id=api_call_id,
                    result=result,
                    usage=backend_metered_usage(self._video_backend, request, result),
                )
        else:
            logger.warning(
                "resume 未反查到待结算调用行 task_id=%s job_id=%s",
                task_id,
                job_id,
            )

        # Same link as a first run: the resumed version belongs to the same task and the same ledger call.
        # 与首跑同一关联：续跑出的版本属于同一个任务、同一条账本调用。
        version_metadata.update(_generation_link(task_id, api_call_id))

        # backend.resume_video has already downloaded the paid result to the requested path. Final media requests write to staging,
        # and the commit callback then saves history and decides whether to select it within one controlled transaction; non-final requests append a version directly.
        # backend.resume_video 已将付费结果下载到请求路径。正式媒体请求写入 staging，随后由
        # commit callback 在一个受控事务内保存历史并决定是否选中；非正式请求直接追加版本。
        await self._trash_if_target_deleted(
            resource_type=resource_type,
            resource_id=resource_id,
            staged_file=staged_output_path,
            task_id=task_id,
        )
        await self._prepare_formal_video_commit(
            resource_type=resource_type,
            resource_id=resource_id,
            prompt=prompt,
            output_path=output_path,
            staged_output_path=staged_output_path,
            duration_seconds=duration_int,
            version_metadata=version_metadata,
            before_formal_commit=before_formal_commit,
        )

        committed = await self._commit_video_output_version(
            resource_type=resource_type,
            resource_id=resource_id,
            prompt=prompt,
            output_path=output_path,
            staged_output_path=staged_output_path,
            duration_seconds=duration_int,
            version_metadata=version_metadata,
            commit_formal_output=commit_formal_output,
        )

        return output_path, committed.version, video_ref, video_uri
