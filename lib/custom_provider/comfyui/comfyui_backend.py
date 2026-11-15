"""Video call channel of ComfyUI endpoints.

Not in ``lib.backends.video_backends``: this backend's input is a ComfyUI endpoint definition (workflow + node bindings),
and reading it needs this package's construction layer, while the layering contract (``pyproject.toml``
``[tool.importlinter]``) does not allow the backend layer to depend back on ``lib.custom_provider``; the direction matches
the declarative runtime: upper layers consume lower layers, and lower layers don't know endpoint definitions exist.

Four stages of one generation: upload assets and get back reference names the server knows → build the workflow to send on a
deep copy of the base → ``POST /prompt`` to get ``prompt_id`` → poll ``/history`` to a terminal state, then fetch the
artifact per the ``output`` binding, download and store it. Asset upload comes before construction because the reference
names must be filled into the workflow; persisting ``provider_job_id`` comes before polling, because if the process
restarts mid-poll, a task that wasn't stored can never be found again.

The fourth stage itself is media-agnostic and lives in :mod:`.comfyui_execution`, shared with the image channel; this
module only handles what is unique to video: capability declaration derived from the binding table, upload of first/end
frames and reference images, resume, and packing the fetched artifact into ``VideoGenerationResult``.

Resume picks up at the fourth stage: ``provider_job_id`` is the ``prompt_id``, and the first three stages already happened in
the previous process. Cancellation and timeout go the other way: once the local side no longer wants this execution, the
remote is stopped too, otherwise it would keep occupying the user's GPU.

ComfyUI 端点的视频调用通道。

不在 ``lib.backends.video_backends``：本 backend 的输入是一份 ComfyUI 端点定义（workflow + 节点绑定），
读它要用本包的构造层，而分层契约（``pyproject.toml`` ``[tool.importlinter]``）不允许 backend 层反向依赖
``lib.custom_provider``；方向与声明式运行时一致——上层消费下层，下层不知道端点定义的存在。

一次生成的四段：上传素材换回服务端认的引用名 → 在底稿深拷贝上构造实发 workflow → ``POST /prompt``
拿 ``prompt_id`` → 轮询 ``/history`` 到终态后按 ``output`` 绑定取产物下载入库。素材上传排在构造
之前，因为引用名要填进 workflow；``provider_job_id`` 的持久化排在轮询之前，因为进程在轮询中途重启
时，没落库的那笔任务就再也找不回来了。

第四段本身与媒体类型无关，落在 :mod:`.comfyui_execution`，与图像通道共用；本模块只管视频这一侧
独有的部分——由绑定表推出的能力声明、首尾帧与参考图的上传、续跑，以及把取回的产物装成
``VideoGenerationResult``。

续跑接的是第四段：``provider_job_id`` 就是 ``prompt_id``，前三段已经在上一个进程里发生过。取消与
超时则反过来——本地这一侧不要这次执行了，就顺手把远端也停掉，否则它会一直占着用户的显卡。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from arcreel_market_core.video_backend_contract import (
    ResumeExpiredError,
    VideoAudioMode,
    VideoCapabilities,
    VideoGenerationRequest,
    VideoGenerationResult,
)
from lib.backends.artifact_download_guard import artifact_http_client
from lib.backends.backend_runtime import ProviderJobIdPersistenceMixin, notify_provider_response
from lib.custom_provider.comfyui.capabilities import derive_video_capabilities
from lib.custom_provider.comfyui.comfyui_client import ComfyuiClient, client_id_for, upload_filename
from lib.custom_provider.comfyui.comfyui_execution import HTTP_TIMEOUT_SECONDS, ComfyuiExecution, PickedArtifact
from lib.custom_provider.comfyui.request_builder import BuiltWorkflow, MediaInputs, build_workflow

logger = logging.getLogger(__name__)

#: This endpoint produces video: the artifact extension whitelist and the failure message read this one value
#: (see ``comfyui.artifacts.ARTIFACT_SUFFIXES_BY_MEDIA_TYPE``).
#: 本端点产出的是视频：产物扩展名白名单与失败文案共读这一个值
#: （见 ``comfyui.artifacts.ARTIFACT_SUFFIXES_BY_MEDIA_TYPE``）。
MEDIA_TYPE = "video"


class ComfyuiVideoBackend(ProviderJobIdPersistenceMixin):
    """Run a ComfyUI endpoint definition into a shot video. / 把一份 ComfyUI 端点定义跑成一个分镜视频。"""

    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        base_url: str,
        api_key: str,
        definition: Mapping[str, Any],
        job_label: str | None = None,
    ) -> None:
        """``job_label`` is the task identifier on non-worker paths, used in upload file names and ``client_id``.

        Not passed on the worker path: there ``request.task_id`` exists, and that is this job's identity on the ArcReel side. When
        neither exists it falls back to a random hex string: unrecognizable in ComfyUI's queue UI, but at least it won't collide with other callers.

        ``job_label`` 是非 worker 路径的任务标识，进上传文件名与 ``client_id``。

        worker 路径不传：那里有 ``request.task_id``，它才是这一笔在 ArcReel 这一侧的身份。两者
        都没有时回落到一串随机 hex——在 ComfyUI 的队列界面上认不出是谁发的，但至少不会与别的
        调用方撞名。
        """
        self._provider = provider_id
        self._model = model
        self._definition = definition
        self._job_label = job_label
        self._base_url = base_url
        self._api_key = api_key
        self._client = ComfyuiClient(base_url=base_url, api_key=api_key, definition=definition)
        self._execution = ComfyuiExecution(client=self._client, definition=definition, media_type=MEDIA_TYPE)

    @property
    def name(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    @property
    def video_capabilities(self) -> VideoCapabilities:
        """What this workflow's binding table says it can do.

        This is not a fallback declaration read only when bypassing the factory: the wrapper's tier query
        (``CustomVideoBackend.video_capabilities_for_tier``) deliberately does not short-circuit to the merged result injected by
        the factory, but uses the wrapped backend's declaration here as the base and layers user overrides on top. The
        pre-generation capability gate goes exactly that way: claim one bit less here and the gate blocks it before the request reaches :meth:`generate`.

        这份 workflow 的绑定表说它能做什么。

        这不是一份只在绕过工厂时才读的兜底声明：包装层的档位查询
        （``CustomVideoBackend.video_capabilities_for_tier``）刻意不短路回工厂注入的合成结果，而是
        以被包装 backend 的这份声明为基底再叠加用户覆盖。生成前的能力闸门走的正是那条路——这里
        少宣称一位，闸门就会在请求到达 :meth:`generate` 之前把它挡掉。
        """
        return binding_video_capabilities(self._definition)

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        job_label = request.task_id or self._job_label or uuid4().hex
        async with artifact_http_client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as http:
            media = await self._upload_media(http, request, job_label=job_label)
            built = build_workflow(
                self._definition,
                prompt=request.prompt,
                aspect_ratio=request.aspect_ratio,
                # Duration is passed on as is: when ``frames`` is unbound or the frame rate can't be read, the construction layer skips the
                # frame count conversion itself; adding a default here would only turn "this workflow's duration is not driven by ArcReel" into a specific number of seconds.
                # 时长原样转交：``frames`` 未绑定或读不到帧率时，构造层自己跳过帧数换算——在这里
                # 补一个缺省只会把「这份 workflow 的时长不由 ArcReel 驱动」写成一个具体秒数。
                duration_seconds=request.duration_seconds,
                resolution=request.resolution,
                media=media,
                seed=request.seed,
            )
            prompt_id = await self._client.submit_prompt(
                http,
                built.workflow,
                client_id=client_id_for(job_label),
                record=lambda stage, body: notify_provider_response(request, stage, body),
            )
            await self._persist_provider_job_id(
                request, prompt_id, provider=self._provider, endpoint=self._client.base_url
            )
            picked = await self._execution.fetch_artifact(
                http,
                prompt_id,
                output_path=request.output_path,
                poll_timeout_seconds=request.poll_timeout_seconds,
                record=lambda stage, body: notify_provider_response(request, stage, body),
            )
            return self._result(request, prompt_id, picked, built=built)

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        """Continue an already submitted execution: go straight to polling, neither re-uploading assets nor resubmitting the workflow.

        ``prompt_id`` is the ``provider_job_id``, and ComfyUI execution happens entirely on the server; resubmitting would run the
        same graph again on the user's GPU. The ``output`` binding is read from the **current** endpoint definition: if the user
        changed the bindings before resuming, fetching artifacts by the new binding is the only sensible rule, and not finding one is ``comfyui_output_missing``.

        The actual seed and workflow fingerprint don't come back with resume: both exist only in the construction at submit time, and this path doesn't construct.

        The domain is the one from submit time (``submitted_base_url``, replayed from the task row by resume_executor), the same rule
        as the declarative runtime: the provider's base_url may change after submit, while this job lives on the original ComfyUI
        machine. Asking the current domain would ask a different machine, which answers "no such prompt_id": an execution still
        producing output would be judged lost while the user's GPU is still spinning for it. The domain is a connection dimension, not a protocol dimension.

        接续一次已经提交过的执行：直接进轮询，既不重传素材也不重新提交 workflow。

        ``prompt_id`` 就是 ``provider_job_id``，而 ComfyUI 的执行完全在服务端，重新提交会在用户
        的显卡上把同一张图再跑一遍。``output`` 绑定读的是**当前**这份端点定义——用户在续跑之前改
        过绑定时，按新绑定去取产物是唯一说得通的口径，取不到即 ``comfyui_output_missing``。

        实发种子与 workflow 指纹不随续跑回来：两者只在提交那一次的构造里存在，而这条路不构造。

        域名取提交那一次的（``submitted_base_url``，由 resume_executor 从任务行回放），与声明式
        运行时同一口径：供应商的 base_url 可以在提交之后被改，而这一笔活在原来那台 ComfyUI 上。
        照当前域名去问，问的是另一台机器，它答「没有这个 prompt_id」——一次仍在出片的执行会被
        判成丢失，用户的显卡还在为它转。域名是连接维度，不是协议维度。
        """
        submitted = request.submitted_base_url
        if submitted and submitted != self._base_url:
            return await self._bound_to(submitted).resume_video(job_id, request)
        async with artifact_http_client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as http:
            picked = await self._execution.fetch_artifact(
                http,
                job_id,
                output_path=request.output_path,
                poll_timeout_seconds=request.poll_timeout_seconds,
                record=lambda stage, body: notify_provider_response(request, stage, body),
                # "Execution not found" during resume counts as resume_expired: the worker then marks it failed and settles that pending
                # call row, rather than treating it as a generation failure that can be retried on the spot.
                # 续跑期的「执行找不着了」归 resume_expired：worker 据此标失败并结算那条 pending
                # 的调用行，而不是把它当成一次可以就地重试的生成失败。
                lost_error=lambda prompt_id: ResumeExpiredError(job_id=prompt_id, provider=self._provider),
            )
            return self._result(request, job_id, picked, built=None)

    def _bound_to(self, base_url: str) -> ComfyuiVideoBackend:
        """Another instance with the same definition and a different domain: polling, fetching and stopping must all land on the same machine.

        同一份定义、换一个域名的另一个实例：轮询、取件与叫停这一路都得落在同一台机器上。"""
        return ComfyuiVideoBackend(
            provider_id=self._provider,
            model=self._model,
            base_url=base_url,
            api_key=self._api_key,
            definition=self._definition,
            job_label=self._job_label,
        )

    def _result(
        self,
        request: VideoGenerationRequest,
        prompt_id: str,
        picked: PickedArtifact,
        *,
        built: BuiltWorkflow | None,
    ) -> VideoGenerationResult:
        """Pack the fetched artifact into a result; ``built`` being ``None`` means this job is a resume (never constructed).

        把取回的产物装成结果；``built`` 为 ``None`` 即这一笔是续跑（没有构造过）。"""
        warnings: tuple[Mapping[str, Any], ...] = ()
        if picked.count > 1:
            warnings = (
                {"key": "comfyui_multiple_outputs", "params": {"count": picked.count, "filename": picked.filename}},
            )
        return VideoGenerationResult(
            video_path=request.output_path,
            provider=self._provider,
            model=self._model,
            duration_seconds=request.duration_seconds,
            video_uri=self._execution.view_url(picked.artifact),
            task_id=prompt_id,
            # The actual seed and workflow fingerprint exist only in the construction at submit time: together they tell "which graph and
            # which seed produced this version", and the resume path constructed neither, so both are absent together rather than each given a fake value.
            # 实发种子与 workflow 指纹只在提交那一次的构造里存在：两者一起才说得清「这一版是照
            # 哪份图、用哪个种子出的」，而续跑这条路一样都没构造，故一起缺席而不是各给一个假值。
            seed=built.seed if built is not None else None,
            generate_audio=request.generate_audio,
            provenance={"workflow_sha256": built.workflow_sha256} if built is not None else None,
            warnings=warnings,
        )

    # Upload / ------------------------------------------------------------------ 上传

    async def _upload_media(
        self, http: httpx.AsyncClient, request: VideoGenerationRequest, *, job_label: str
    ) -> MediaInputs:
        """Upload the assets needed this time and get back the reference names the image-reading nodes know.

        Only those "bound and given this time" are uploaded: extra images would never be read by any node and waste the user's
        bandwidth and disk, and the number of reference image slots is exactly how many the workflow can take; extras would have no use in the construction layer anyway.

        把这次要用到的素材传上去，换回读图节点认的引用名。

        只传「绑定了、且这次给了」的那些：多传的图不会被任何节点读到，白占用户的带宽与磁盘，而
        参考图格子数就是这份 workflow 能收几张，多出来的那几张在构造层本来就用不上。
        """
        bindings: Mapping[str, Any] = self._definition.get("bindings") or {}
        start = await self._upload_one(http, request.start_image, bindings, "start_image", job_label=job_label)
        end = await self._upload_one(http, request.end_image, bindings, "end_image", job_label=job_label)
        slots = len(_targets(bindings.get("reference_images")))
        references: list[str] = []
        for index, path in enumerate((request.reference_images or [])[:slots]):
            references.append(
                await self._client.upload_image(
                    http, path, filename=upload_filename(job_label, "reference_images", path, index)
                )
            )
        return MediaInputs(start_image=start, end_image=end, reference_images=tuple(references))

    async def _upload_one(
        self,
        http: httpx.AsyncClient,
        path: Path | None,
        bindings: Mapping[str, Any],
        key: str,
        *,
        job_label: str,
    ) -> str | None:
        if path is None or not _targets(bindings.get(key)):
            return None
        return await self._client.upload_image(http, path, filename=upload_filename(job_label, key, path))


def _targets(raw: object) -> list[Mapping[str, Any]]:
    return [target for target in raw if isinstance(target, Mapping)] if isinstance(raw, list) else []


def binding_video_capabilities(definition: Mapping[str, Any]) -> VideoCapabilities:
    """Pack a video endpoint definition's binding table into the backend layer's capability type.

    The derivation itself is in the ``comfyui`` subpackage (it only knows the binding table and workflow); packing lives in this
    module because the subpackage is bound by the forbidden contract of "not depending on the declarative runtime" and can't
    reach ``VideoCapabilities``. The endpoint projection (``endpoints.comfyui_endpoint_spec``) and the backend's own declaration
    share this one, rather than each writing its own: they feed different parts of the capability gate, but must say the same thing.

    The three reference audio items and ``max_prompt_chars`` stay default: the binding table has no matching semantic keys.

    把一份视频端点定义的绑定表装进 backend 层的能力类型。

    推导本身在 ``comfyui`` 子包里（它只认绑定表与 workflow）；装箱落在本模块，因为子包受
    「不依赖声明式运行时」的 forbidden 契约约束，够不到 ``VideoCapabilities``。端点投影
    （``endpoints.comfyui_endpoint_spec``）与 backend 自己的声明共用这一份，两处不各写一份——
    它们各自喂给能力闸门的不同一段，说的却必须是同一件事。

    参考音频三项与 ``max_prompt_chars`` 保持默认：绑定表里没有对应的语义键。
    """
    bound = derive_video_capabilities(definition)
    return VideoCapabilities(
        text_to_video=bound.text_to_video,
        first_frame=bound.first_frame,
        last_frame=bound.last_frame,
        max_reference_images=bound.max_reference_images,
        audio_track=VideoAudioMode(bound.audio_track),
    )
