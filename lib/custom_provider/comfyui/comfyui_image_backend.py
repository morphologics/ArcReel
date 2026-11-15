"""Image call channel of ComfyUI endpoints.

Shares the same client (:mod:`.comfyui_client`), the same request body construction layer (:mod:`.request_builder`) and the
same execution layer (:mod:`.comfyui_execution`) with the video channel (:mod:`.comfyui_backend`). This module only keeps
three things unique to images: reference image upload, the capability set and reference image limit derived from ``bindings``,
and packing the fetched artifact into ``ImageGenerationResult``.

Two substantive differences from the video channel:

* **No resume.** The ``ImageBackend`` protocol has no ``resume`` slot, so ``prompt_id`` has nowhere to be persisted
  (``ImageGenerationRequest`` carries no ``task_id``). If the process restarts mid-poll, the worker's orphan handling marks
  image tasks as ``restart_lost_image``: the submit already happened, and resubmitting would make the user's GPU run the same
  image again. Cancellation and timeout still stop the remote: that needs no persistence, ``prompt_id`` is at hand.
* **Multiple artifacts only go to the log.** A workflow with ``batch_size > 1`` produces several images at once, and this
  channel takes the first; ``ImageGenerationResult`` has no notice slot like ``result.warnings`` (the video channel does), so
  this notice only goes to the log. Binding inference at import time has a separate ``batch_size_above_one`` UI notice that
  the user sees before saving the endpoint.

ComfyUI 端点的图像调用通道。

与视频通道（:mod:`.comfyui_backend`）共用同一个客户端（:mod:`.comfyui_client`）、同一个请求体
构造层（:mod:`.request_builder`）与同一份执行层（:mod:`.comfyui_execution`）。本模块只剩图像这一侧独有的三件事：参考图的上传、``bindings``
推出的能力集与参考图上限、把取回的产物装成 ``ImageGenerationResult``。

与视频通道的两处实质差别：

* **不续跑。** ``ImageBackend`` 协议没有 ``resume`` 这一格，``prompt_id`` 也就无处持久化
  （``ImageGenerationRequest`` 不带 ``task_id``）。进程在轮询中途重启时，worker 的孤儿处置把
  image 任务一律标 ``restart_lost_image``——提交本身已经发生过，重新提交等于让用户的显卡把同一
  张图再跑一遍。取消与超时仍然叫停远端：那一格不需要持久化，``prompt_id`` 就在手里。
* **多产物只进日志。** ``batch_size > 1`` 的 workflow 一次出多张，本通道取第一张；
  ``ImageGenerationResult`` 没有 ``result.warnings`` 那样的提示位（视频通道有），故这句提示只落
  日志。导入期的绑定推断另有一条 ``batch_size_above_one`` 的界面提示，用户在保存端点之前就看得到。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from arcreel_market_core.comfyui.bindings import targets_of
from arcreel_market_core.video_backend_contract import ProviderResponseStage
from lib.backends.artifact_download_guard import artifact_http_client
from lib.backends.backend_runtime import notify_provider_response_to
from lib.backends.image_backends.base import (
    ImageCapability,
    ImageGenerationRequest,
    ImageGenerationResult,
)
from lib.custom_provider.comfyui.capabilities import takes_reference_images
from lib.custom_provider.comfyui.comfyui_client import ComfyuiClient, client_id_for, upload_filename
from lib.custom_provider.comfyui.comfyui_execution import HTTP_TIMEOUT_SECONDS, ComfyuiExecution
from lib.custom_provider.comfyui.request_builder import MediaInputs, build_workflow

logger = logging.getLogger(__name__)

#: This endpoint produces images: the artifact extension whitelist and the failure message read this one value
#: (see ``comfyui.artifacts.ARTIFACT_SUFFIXES_BY_MEDIA_TYPE``).
#: 本端点产出的是图像：产物扩展名白名单与失败文案共读这一个值
#: （见 ``comfyui.artifacts.ARTIFACT_SUFFIXES_BY_MEDIA_TYPE``）。
MEDIA_TYPE = "image"

#: Wall-clock limit for one image execution to reach a terminal state.
#:
#: Does not read the global ``video_poll_timeout_seconds``: that setting is about video, and quietly extending its meaning to
#: images would leave users not knowing how many things they are tuning. Images have no configurable item for this: built-in
#: image providers are all a single synchronous call, while ComfyUI image generation both queues and runs on the user's own
#: GPU, so a fixed value wide enough for the latter's worst case is used.
#: 一次图像执行等到终态的墙钟上限。
#:
#: 不读全局的 ``video_poll_timeout_seconds``：那个设置项说的是视频，把它的含义悄悄扩到图像上，
#: 用户调它的时候就不知道自己在调几件事。图像这一维没有可配项——内置图像供应商都是一次同步调用，
#: 而 ComfyUI 的出图既要排队又跑在用户自己的显卡上，故按后者的最坏情形取一个够宽的定值。
IMAGE_POLL_TIMEOUT_SECONDS = 1800


class ComfyuiImageBackend:
    """Run a ComfyUI endpoint definition into an image.

    Implements the ``lib.backends.image_backends.base.ImageBackend`` protocol. The four stages of one generation mirror the video
    channel: upload reference images and get back reference names the server knows → build the workflow to send on a deep copy
    of the base → ``POST /prompt`` to get ``prompt_id`` → poll ``/history`` to a terminal state, then fetch the artifact per the
    ``output`` binding, download and store it.

    把一份 ComfyUI 端点定义跑成一张图。

    实现 ``lib.backends.image_backends.base.ImageBackend`` 协议。一次生成的四段与视频通道同构：上传参考图
    换回服务端认的引用名 → 在底稿深拷贝上构造实发 workflow → ``POST /prompt`` 拿 ``prompt_id`` →
    轮询 ``/history`` 到终态后按 ``output`` 绑定取产物下载入库。
    """

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
        """``job_label`` goes into upload file names and ``client_id``, so the user can tell who sent it in ComfyUI's queue UI.

        Image requests carry no ``task_id`` (``ImageGenerationRequest`` has no such dimension), so there is no worker vs non-worker
        path distinction here: use the label if the caller gave one, otherwise fall back to a random hex string: unrecognizable, but
        it won't collide with other callers and overwrite their uploads.

        ``job_label`` 进上传文件名与 ``client_id``，让用户在 ComfyUI 的队列界面上认出是谁发的。

        图像请求不带 ``task_id``（``ImageGenerationRequest`` 没有这一维），故这里没有 worker 路径
        与非 worker 路径之分：调用方给了标签就用它，没给就回落到一串随机 hex——认不出是谁发的，
        但不会与别的调用方撞名、覆盖掉对方的上传。
        """
        self._provider = provider_id
        self._model = model
        self._definition = definition
        self._job_label = job_label
        self._client = ComfyuiClient(base_url=base_url, api_key=api_key, definition=definition)
        self._execution = ComfyuiExecution(client=self._client, definition=definition, media_type=MEDIA_TYPE)

    @property
    def name(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[ImageCapability]:
        """What this workflow's binding table says it can do.

        The endpoint projection (``endpoints.comfyui_endpoint_spec``) and this share one implementation,
        :func:`binding_image_capabilities`: the projection's copy decides which bucket the model row goes into, this one is the
        fallback gate before ``media_generator`` sends the request, and they must say the same thing; otherwise an endpoint that
        gets into the i2i bucket would be blocked by itself at the gate.

        这份 workflow 的绑定表说它能做什么。

        端点投影（``endpoints.comfyui_endpoint_spec``）与这里共用 :func:`binding_image_capabilities`
        一份实现：投影那一份决定这个模型行进哪个桶，这一份是 ``media_generator`` 发请求之前的兜底
        闸门，两者说的必须是同一件事——否则一个进得了 i2i 桶的端点会在闸门上被自己挡掉。
        """
        return set(binding_image_capabilities(self._definition))

    async def reference_image_limit(self) -> int:
        return self._reference_slots

    @property
    def _reference_slots(self) -> int:
        """The number of reference image slots is exactly how many images this workflow can take.

        Does not return ``0`` ("this backend doesn't trim by count"): extra images have nowhere to go in the construction layer
        anyway, and letting the orchestration layer trim by this number first with a notice is more honest than silently dropping
        some reference images the user picked.

        参考图格子数就是这份 workflow 能收几张。

        不返回 ``0``（「本后端不按数量裁剪」）：多出来的图在构造层本来就没有落点，让编排层先按这个
        数裁剪并提示，比静默丢掉几张用户挑过的参考图诚实。
        """
        return len(targets_of((self._definition.get("bindings") or {}).get("reference_images")))

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        job_label = self._job_label or uuid4().hex
        async with artifact_http_client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as http:
            media = await self._upload_media(http, request, job_label=job_label)
            built = build_workflow(
                self._definition,
                prompt=request.prompt,
                aspect_ratio=request.aspect_ratio,
                resolution=request.image_size,
                media=media,
                seed=request.seed,
            )

            async def record(stage: ProviderResponseStage, body: object) -> None:
                await notify_provider_response_to(request.on_provider_response, stage, body)

            prompt_id = await self._client.submit_prompt(
                http,
                built.workflow,
                client_id=client_id_for(job_label),
                record=record,
            )
            picked = await self._execution.fetch_artifact(
                http,
                prompt_id,
                output_path=request.output_path,
                poll_timeout_seconds=IMAGE_POLL_TIMEOUT_SECONDS,
                record=record,
            )
            return ImageGenerationResult(
                image_path=request.output_path,
                provider=self._provider,
                model=self._model,
                image_uri=self._execution.view_url(picked.artifact),
                # Actual seed: with ``policy: "random"`` it is randomized at submit time, and the value in the request can't tell it. Report it
                # rather than leave it empty: the image call entry doesn't merge it into version metadata (``_merge_result_provenance`` only
                # hangs on the video path), and this is the only place at this layer that can tell which seed this version used.
                # 实发种子：``policy: "random"`` 是提交那一刻现随机的，请求里那个值说不出它。报出来
                # 而不是留空——图像调用入口不把它并进版本元数据（``_merge_result_provenance`` 只挂在
                # 视频那一条路上），而这一位是本层唯一说得清这一版用了哪个种子的地方。
                seed=built.seed,
            )

    async def _upload_media(
        self, http: httpx.AsyncClient, request: ImageGenerationRequest, *, job_label: str
    ) -> MediaInputs:
        """Upload the reference images needed this time and get back the reference names the image-reading nodes know.

        Only those that fit into slots are uploaded: the slot count is how many the workflow can take; extra images would never be
        read by any node and waste the user's bandwidth and disk. Image endpoints don't have the first/end frame semantic keys (see
        ``comfyui.bindings``), so there is only the reference image path here.

        把这次要用到的参考图传上去，换回读图节点认的引用名。

        只传落得进格子的那几张：格子数就是这份 workflow 能收几张，多出来的图不会被任何节点读到，
        白占用户的带宽与磁盘。图像端点没有首尾帧这两个语义键（见 ``comfyui.bindings``），故这里
        只有参考图一路。
        """
        slots = self._reference_slots
        references: list[str] = []
        for index, reference in enumerate(request.reference_images[:slots]):
            path = Path(reference.path)
            references.append(
                await self._client.upload_image(
                    http, path, filename=upload_filename(job_label, "reference_images", path, index)
                )
            )
        return MediaInputs(reference_images=tuple(references))


def binding_image_capabilities(definition: Mapping[str, Any]) -> frozenset[ImageCapability]:
    """Pack an image endpoint definition's binding table into the backend layer's capability type.

    Reference image slots mean image-to-image only, none means text-to-image only; the two are mutually exclusive
    (``docs/adr/0082``). The criterion itself is in the ``comfyui`` subpackage (it only knows the binding table and workflow);
    packing lives in this module because the subpackage is bound by the forbidden contract of "not depending on the declarative
    runtime" and can't reach ``ImageCapability``. Same shape as ``binding_video_capabilities`` on the video side.

    把一份图像端点定义的绑定表装进 backend 层的能力类型。

    有参考图格子即仅图生图，没有即仅文生图，两者互斥（``docs/adr/0082``）。判据本身在 ``comfyui``
    子包里（它只认绑定表与 workflow）；装箱落在本模块，因为子包受「不依赖声明式运行时」的 forbidden
    契约约束，够不到 ``ImageCapability``。落点与视频那一侧的 ``binding_video_capabilities`` 同形。
    """
    if takes_reference_images(definition):
        return frozenset({ImageCapability.IMAGE_TO_IMAGE})
    return frozenset({ImageCapability.TEXT_TO_IMAGE})
