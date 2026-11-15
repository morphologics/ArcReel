"""DashScopeImageBackend — Alibaba Bailian Qwen-Image / Wanxiang image generation backend (synchronous).

Uses the native multimodal-generation/generation synchronous endpoint; T2I and I2I share the same request body, differing
only in whether content includes an image element. Covers the qwen-image-2.0 fusion series, the qwen-image-edit editing
series and the wan2.7-image series. The schema follows the first-hand official docs listed in docs/api-docs/providers/dashscope.md.

DashScopeImageBackend — 阿里百炼 Qwen-Image / 万相图像生成后端（同步）。

走原生 multimodal-generation/generation 同步端点，T2I 与 I2I 共用同一请求体，
只差 content 是否含 image 元素。覆盖 qwen-image-2.0 融合系列、qwen-image-edit
编辑系列与 wan2.7-image 系列。schema 依据 docs/api-docs/providers/dashscope.md 所列一手官方文档。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import ClassVar

from arcreel_market_core.aspect_size import IMAGE_TIER_SHORT_EDGE, aspect_size, resolution_to_short_edge
from lib.backends.artifact_download_guard import artifact_http_client
from lib.backends.backend_runtime import should_retry_submit, submit_post
from lib.backends.dashscope_shared import (
    dashscope_headers,
    dashscope_native_base_url,
    extract_image_url,
    image_to_data_uri,
    resolve_dashscope_api_key,
    safe_body_for_log,
)
from lib.backends.image_backends.base import (
    BOTH_IMAGE_CAPABILITIES,
    ImageCapability,
    ImageCapabilityError,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageRequestFacts,
    download_image_to_path,
    per_image_usage,
)
from lib.backends.providers import PROVIDER_DASHSCOPE
from lib.backends.transport_layer import TransportLayer
from lib.billing.metering import Usage
from lib.infra.logging_utils import format_kwargs_for_log
from lib.infra.retry import with_retry_async

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen-image-2.0"

_IMAGE_ENDPOINT = "/services/aigc/multimodal-generation/generation"

# Reference image limits: qwen family 1~3, wan family 0~9 (confirmed by docs) / 参考图上限：qwen 系 1~3 张、wan 系 0~9 张（docs 确权）
_QWEN_REF_LIMIT = 3
_WAN_REF_LIMIT = 9

# Default short edge per family when resolution is missing. The ratio always comes from aspect_ratio; arcreel_market_core.aspect_size computes the exact ratio
# within each family's pixel constraints (see docs/adr/0011), rather than static pixel tables with ratio deviations:
#   - qwen fusion series native default 2048²; wan defaults to the 2K tier; the editing series is naturally capped by max_long_edge.
# 缺分辨率时各族默认短边。比例永远来自 aspect_ratio，由 arcreel_market_core.aspect_size 在各族像素约束内
# 算精确比例（见 docs/adr/0011），不用带比例偏差的静态像素表：
#   - qwen 融合系列 native 默认 2048²；wan 默认 2K 档；编辑系列由 max_long_edge 自然收口。
_DEFAULT_WAN_BUDGET = "2K"
_DEFAULT_SHORT_FUSION = 2048
_DEFAULT_SHORT_WAN = 1440
_DEFAULT_SHORT_EDIT = 2048

# Standard tier total pixel budget (non-pro / non-text-to-image limit) = 2048×2048; beyond that requires wan2.7-image-pro text-to-image (4K=4096×4096)
# 标准档总像素预算（非 pro / 非文生图上限）= 2048×2048；超出须 wan2.7-image-pro 文生图（4K=4096×4096）
_STANDARD_PIXEL_BUDGET = 2048 * 2048
_FOURK_PIXEL_BUDGET = 4096 * 4096
# Editing series (qwen-image-edit-plus / -max): width and height both ∈ [512, 2048] / 编辑系列（qwen-image-edit-plus / -max）宽高均 ∈ [512, 2048]
_EDIT_MAX_LONG_EDGE = 2048
# Ratio range supported by DashScope generation series: 1:8 ~ 8:1 / DashScope 生成系列比例支持区间 1:8 ~ 8:1
_DASHSCOPE_MAX_RATIO = 8.0
# Since the editing series has width and height both ∈ [512, 2048], the max expressible ratio is 2048/512 = 4:1 (narrower than the generation series)
# 编辑系列因宽高均 ∈ [512, 2048]，可表达的比例上限为 2048/512 = 4:1（比生成系列更窄）
_EDIT_MAX_RATIO = 4.0


class DashScopeImageBackend:
    """Alibaba Bailian image backend (synchronous multimodal endpoint). / 阿里百炼图像后端（同步 multimodal 端点）。"""

    CHANNEL_CAPABILITIES: ClassVar[frozenset[ImageCapability]] = BOTH_IMAGE_CAPABILITIES

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        http_timeout: float = 120.0,
        transport_layer: TransportLayer | None = None,
    ) -> None:
        self._api_key = resolve_dashscope_api_key(api_key)
        self._base_url = dashscope_native_base_url(base_url)
        self._model = model or DEFAULT_MODEL
        self._http_timeout = http_timeout
        self._transport_layer = transport_layer
        mid = self._model.lower()
        self._is_wan = mid.startswith("wan")
        self._is_edit = "qwen-image-edit" in mid

    @staticmethod
    def _exceeds_standard_budget(size: str) -> bool:
        """Whether size exceeds the standard tier total pixel budget (2048×2048).

        Per the docs: output beyond 2048×2048 is only supported by wan2.7-image-pro text-to-image (4K tier = 4096×4096). Tiers
        "1K"/"2K" are within budget, "4K" exceeds it; pixel values are judged by "total pixels > budget", so number forms like
        "4096*4096" / "3000*3000" can't bypass the gate by only recognizing the literal "4K" (this computes total pixels from
        the ratio, so legitimate narrow sizes like "4096*512" aren't wrongly rejected).

        size 是否超出标准档总像素预算（2048×2048）。

        docs 口径：超出 2048×2048 的输出仅 wan2.7-image-pro 文生图支持（4K 档=4096×4096）。
        档位 "1K"/"2K" 在预算内、"4K" 超预算；像素值按"总像素 > 预算"判定，避免只认 "4K"
        字面而让 "4096*4096" / "3000*3000" 等数字写法绕过门控（这是按比例算总像素，
        故 "4096*512" 这类窄幅合法尺寸不会被误拒）。
        """
        normalized = size.strip().upper()
        if normalized in ("1K", "2K"):
            return False
        if normalized == "4K":
            return True
        for sep in ("*", "X", "×"):
            if sep in normalized:
                parts = normalized.split(sep, 1)
                try:
                    return int(parts[0]) * int(parts[1]) > _STANDARD_PIXEL_BUDGET
                except ValueError:
                    return False
        return False

    @property
    def name(self) -> str:
        return PROVIDER_DASHSCOPE

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[ImageCapability]:
        return set(self.CHANNEL_CAPABILITIES)

    async def reference_image_limit(self) -> int:
        return self._reference_limit

    @property
    def _reference_limit(self) -> int:
        return _WAN_REF_LIMIT if self._is_wan else _QWEN_REF_LIMIT

    @staticmethod
    def estimate_usage(facts: ImageRequestFacts) -> Usage:
        """The estimate only reports the image count: Bailian images have a uniform per-image price, and size doesn't affect cost.

        预估只报张数：百炼图片按张统一价，尺寸不影响费用。"""
        refs = facts.reference_count
        limit = _WAN_REF_LIMIT if facts.model.lower().startswith("wan") else _QWEN_REF_LIMIT
        return per_image_usage(output_images=1, input_images=None if refs is None else min(refs, limit))

    @with_retry_async(retry_if=should_retry_submit)
    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        has_refs = bool(request.reference_images)
        size = self._resolve_size(request, has_refs)
        content = await asyncio.to_thread(self._build_content, request, has_refs)

        parameters: dict = {
            "n": 1,
            "watermark": False,
            # ArcReel script prompts are already LLM-refined descriptions; disable smart rewriting to keep the original intent
            # ArcReel 剧本 prompt 已是 LLM 精炼描述，关闭智能改写保留原意
            "prompt_extend": False,
            "size": size,
        }
        if request.seed is not None:
            parameters["seed"] = request.seed

        payload = {
            "model": self._model,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": parameters,
        }

        logger.info(
            "调用 %s 图片 API model=%s body=%s",
            self.name,
            self._model,
            format_kwargs_for_log(safe_body_for_log(payload)),
        )
        async with artifact_http_client(timeout=self._http_timeout, transport_layer=self._transport_layer) as client:
            # Synchronous image generation is a non-idempotent "create image + bill" POST: submit_post turns ambiguous transport errors (request may have arrived
            # but the response was lost in transit) into a terminal AmbiguousSubmitError, avoiding double billing from automatic retries; >=400 logs the body
            # + raises HTTPStatusError (keeping status_code so the choke point can recognize 413 for downgrade), for should_retry_submit
            # to route by status code -- 4xx fail-fast, 5xx/429 retried.
            # 同步图像生成是非幂等的「建图 + 计费」POST：submit_post 把歧义传输错误（请求可能已送达
            # 但响应在途丢失）转 AmbiguousSubmitError 终态失败，避免自动重试重复计费；>=400 落 body
            # 日志 + 抛 HTTPStatusError（保留 status_code 供咽喉层识别 413 降档），交 should_retry_submit
            # 按状态码分流——4xx fail-fast、5xx/429 重试。
            resp = await submit_post(
                lambda: client.post(
                    f"{self._base_url}{_IMAGE_ENDPOINT}",
                    json=payload,
                    headers=dashscope_headers(self._api_key),
                ),
                provider=PROVIDER_DASHSCOPE,
            )
            data = resp.json()

        url = extract_image_url(data)
        await download_image_to_path(url, request.output_path)
        logger.info("DashScope 图片生成完成: %s", request.output_path)

        return ImageGenerationResult(
            image_path=request.output_path,
            provider=PROVIDER_DASHSCOPE,
            model=self._model,
            image_uri=url,
            usage=per_image_usage(
                output_images=1,
                input_images=sum(1 for part in content if "image" in part),
                dimensions={"resolution": size},
            ),
        )

    def _resolve_size(self, request: ImageGenerationRequest, has_refs: bool) -> str:
        """Compute width*height with "ratio first, resolution second".

        The ratio always comes from aspect_ratio; image_size (tier word / custom width*height / None) only decides the
        resolution's short edge, and a custom value has its own ratio stripped (taking min). Each family is capped precisely by
        its own pixel constraints:
          - wan family: method-2 pixel values (any accepted, aligned to 16), total pixels ≤ standard 2048² / 4K 4096²;
            never pass a tier word down (wan text-to-image would be forced to output a square, losing the ratio).
          - qwen-image-2.0 fusion series: total pixels ∈ [512², 2048²], width and height freely set.
          - qwen-image-edit-plus / -max editing series: width and height both ∈ [512, 2048].
        The classic series (qwen-image / -plus / -max) only has 5 fixed tiers and doesn't accept arbitrary pixels; it has no
        registered preset, isn't recommended, and isn't covered by this mechanism (see docs/adr/0011).

        按「比例优先、清晰度其次」算出 宽*高。

        比例永远来自 aspect_ratio；image_size（档位词 / 自定义 宽*高 / None）只决定清晰度短边，
        自定义值剥离其自带比例（取 min）。各族按自身像素约束精确收口：
          - wan 系：方式二像素值（接受任意、对齐 16），总像素 ≤ 标准 2048² / 4K 4096²；
            绝不下传档位词（wan 文生图会被强制输出正方形、丢比例）。
          - qwen-image-2.0 融合系列：总像素 ∈ [512², 2048²]，自由设宽高。
          - qwen-image-edit-plus / -max 编辑系列：宽高均 ∈ [512, 2048]。
        经典系列（qwen-image / -plus / -max）仅 5 固定档、不接受任意像素，未注册预设、不推荐，
        不在本机制覆盖内（见 docs/adr/0011）。
        """
        explicit = (request.image_size or "").strip()
        aspect = request.aspect_ratio

        if self._is_wan:
            # Beyond the 2048×2048 budget (4K tier or large pixel values) only wan2.7-image-pro text-to-image is supported;
            # non-pro isn't, nor is pro's I2I -- gate first (tier words and pixel values judged uniformly)
            # 超 2048×2048 预算（4K 档或大像素值）仅 wan2.7-image-pro 文生图支持，
            # 非 pro 不支持、pro 的 I2I 不支持 —— 先门控（档位词与像素值统一判定）
            budget_word = explicit or _DEFAULT_WAN_BUDGET
            exceeds = self._exceeds_standard_budget(budget_word)
            if exceeds and ("pro" not in self._model.lower() or has_refs):
                raise ImageCapabilityError("image_dashscope_4k_t2i_only", model=self._model)
            max_total = _FOURK_PIXEL_BUDGET if exceeds else _STANDARD_PIXEL_BUDGET
            short = resolution_to_short_edge(
                explicit or None, tier_map=IMAGE_TIER_SHORT_EDGE, default_short=_DEFAULT_SHORT_WAN
            )
            w, h = aspect_size(aspect, short, round_to=16, max_total_pixels=max_total, max_ratio=_DASHSCOPE_MAX_RATIO)
            return f"{w}*{h}"

        if self._is_edit:
            short = resolution_to_short_edge(
                explicit or None, tier_map=IMAGE_TIER_SHORT_EDGE, default_short=_DEFAULT_SHORT_EDIT
            )
            w, h = aspect_size(aspect, short, round_to=16, max_long_edge=_EDIT_MAX_LONG_EDGE, max_ratio=_EDIT_MAX_RATIO)
            return f"{w}*{h}"

        # qwen-image-2.0 fusion series / qwen-image-2.0 融合系列
        short = resolution_to_short_edge(
            explicit or None, tier_map=IMAGE_TIER_SHORT_EDGE, default_short=_DEFAULT_SHORT_FUSION
        )
        w, h = aspect_size(
            aspect, short, round_to=16, max_total_pixels=_STANDARD_PIXEL_BUDGET, max_ratio=_DASHSCOPE_MAX_RATIO
        )
        return f"{w}*{h}"

    def _build_content(self, request: ImageGenerationRequest, has_refs: bool) -> list[dict]:
        content: list[dict] = []
        if has_refs:
            # fail-loud: if any declared reference image is missing (including "." resolved from a directory/empty string) or fails to read (permissions/concurrent delete
            # → OSError), abort generation and report the file names, so the user notices an image wasn't used, rather than silently dropping it, generating
            # a wrong result from a subset, and billing as usual.
            # fail-loud：任一声明的参考图缺失（含目录/空串解析出的 "."）或读取失败（权限/并发删除
            # → OSError）即中止生成并报错列出文件名，让用户感知到有图未被使用，而非静默丢弃、用子集
            # 生成出错误结果还照常计费。
            data_uris: list[str] = []
            unreadable: list[str] = []
            # names go into multilingual error templates (en/vi also render them), so separators and placeholders use locale-neutral forms:
            # an empty path has no file name to display, so #N identifies which reference image it is, avoiding a Chinese placeholder leaking into non-Chinese errors.
            # names 进多语言错误模板（en/vi 也渲染），分隔符与占位用 locale 中性形式：
            # 空路径无文件名可显示，用序号 #N 标识第几张参考图，避免中文占位漏进非中文报错。
            for idx, ref in enumerate(request.reference_images, start=1):
                path = Path(ref.path) if ref.path else None
                if path is None or not path.is_file():
                    unreadable.append(path.name if path else f"#{idx}")
                    continue
                try:
                    data_uris.append(image_to_data_uri(path))
                except OSError as exc:
                    logger.warning("DashScope 参考图读取失败: %s (%s)", path, exc)
                    unreadable.append(path.name)
            if unreadable:
                raise ImageCapabilityError(
                    "image_reference_images_unreadable", model=self._model, names=", ".join(unreadable)
                )
            if len(data_uris) > self._reference_limit:
                logger.warning(
                    "DashScope 参考图数量 %d 超过 model=%s 上限 %d，截断",
                    len(data_uris),
                    self._model,
                    self._reference_limit,
                )
                data_uris = data_uris[: self._reference_limit]
            content.extend({"image": uri} for uri in data_uris)
        content.append({"text": request.prompt})
        return content
