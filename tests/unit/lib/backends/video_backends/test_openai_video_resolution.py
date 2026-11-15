"""Tests OpenAIVideoBackend's size resolution: snaps to the valid sora enum by model + resolution tier, ratio first.

sora-2 (base) has only 720p; sora-2-pro with 1080p uses the exact HD tiers 1080x1920/1920x1080.

测试 OpenAIVideoBackend 的 size 解析：按 model+分辨率档吸附 sora 合法枚举，比例优先。

sora-2（base）仅 720p；sora-2-pro 选 1080p 时用 1080x1920/1920x1080 精确高清档。
"""

from unittest.mock import MagicMock

import pytest

from arcreel_market_core.video_backend_contract import VideoGenerationRequest
from lib.backends.video_backends.openai import _SORA_LEGAL_SIZES, OpenAIVideoBackend


def _make_backend(model: str = "sora-2"):
    backend = OpenAIVideoBackend.__new__(OpenAIVideoBackend)
    backend._client = MagicMock()
    backend._model = model
    backend._capabilities = set()
    return backend


async def _capture_size(backend, **req_kwargs) -> str:
    captured: dict[str, object] = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop")

    backend._client.videos.create = fake_create
    req = VideoGenerationRequest(prompt="x", duration_seconds=4, **req_kwargs)
    with pytest.raises(RuntimeError):
        await backend.generate(req)
    size = captured.get("size")
    assert isinstance(size, str)  # size is required. / size 必传
    return size


@pytest.mark.asyncio
@pytest.mark.parametrize(("aspect", "expected"), [("9:16", "720x1280"), ("16:9", "1280x720")])
async def test_standard_aspect_maps_to_exact_legal_size(tmp_path, aspect, expected):
    backend = _make_backend()
    size = await _capture_size(backend, output_path=tmp_path / "o.mp4", aspect_ratio=aspect, resolution=None)
    assert size == expected
    assert size in _SORA_LEGAL_SIZES


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", [None, "720p", "1080p", "4K"])
async def test_resolution_does_not_break_ratio(tmp_path, resolution):
    """sora-2 (base) has only the 720p tier: no resolution breaks the ratio, always 720x1280 (clarity yields to ratio/model capability).

    sora-2（base）只有 720p 档：任何 resolution 都不破坏比例，始终 720x1280（清晰度让位比例/模型能力）。"""
    backend = _make_backend()  # sora-2 base
    size = await _capture_size(backend, output_path=tmp_path / "o.mp4", aspect_ratio="9:16", resolution=resolution)
    assert size == "720x1280"


@pytest.mark.asyncio
async def test_custom_resolution_value_ignored_uses_legal_size(tmp_path):
    """A custom resolution value (e.g. 1080x1920, not a valid sora tier) is no longer passed through as an invalid size; it snaps to a valid tier by ratio.

    自定义 resolution 值（如 1080x1920，非 sora 合法档）不再被透传成非法 size，按比例吸附合法档。"""
    backend = _make_backend()
    size = await _capture_size(backend, output_path=tmp_path / "o.mp4", aspect_ratio="9:16", resolution="1080x1920")
    assert size == "720x1280"
    assert size in _SORA_LEGAL_SIZES


@pytest.mark.asyncio
async def test_size_always_set_and_legal(tmp_path):
    """The size field is required and must be a valid enum value -- no more "omit size and let upstream decide the ratio".

    size 字段必传且必为合法枚举——杜绝「不传 size 让上游决定比例」。"""
    backend = _make_backend()
    for aspect in ("9:16", "16:9", "1:1", "4:3", "21:9"):
        size = await _capture_size(backend, output_path=tmp_path / "o.mp4", aspect_ratio=aspect, resolution=None)
        assert size in _SORA_LEGAL_SIZES, aspect


@pytest.mark.asyncio
@pytest.mark.parametrize(("aspect", "expected"), [("9:16", "1080x1920"), ("16:9", "1920x1080")])
async def test_sora2pro_1080p_uses_exact_high_res(tmp_path, aspect, expected):
    """sora-2-pro + 1080p: returns the exact-ratio 1080p tier (fixes the regression in this PR that dropped the 1080 tier to 720p).

    sora-2-pro + 1080p：返回精确比例的 1080p 档（修复本 PR 把 1080 档丢成 720p 的回归）。"""
    backend = _make_backend(model="sora-2-pro")
    size = await _capture_size(backend, output_path=tmp_path / "o.mp4", aspect_ratio=aspect, resolution="1080p")
    assert size == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution", [None, "720p"])
async def test_sora2pro_default_and_720p_stay_720(tmp_path, resolution):
    """sora-2-pro with no resolution or explicit 720p lands on 720p (no unilateral upgrade to 1080p, avoiding overbilling).

    sora-2-pro 缺分辨率或显式 720p 时落 720p（不擅自升 1080p，避免超额计费）。"""
    backend = _make_backend(model="sora-2-pro")
    size = await _capture_size(backend, output_path=tmp_path / "o.mp4", aspect_ratio="9:16", resolution=resolution)
    assert size == "720x1280"


@pytest.mark.asyncio
async def test_sora2pro_4k_capped_to_1080p(tmp_path):
    """sora tops out at 1080p: sora-2-pro requesting 4K is capped to the exact 1080p tier (still exact ratio).

    sora 最高 1080p：sora-2-pro 请求 4K 封顶到 1080p 精确档（仍精确比例）。"""
    backend = _make_backend(model="sora-2-pro")
    size = await _capture_size(backend, output_path=tmp_path / "o.mp4", aspect_ratio="9:16", resolution="4K")
    assert size == "1080x1920"


@pytest.mark.asyncio
async def test_sora2pro_custom_short_picks_nearest_tier(tmp_path):
    """sora-2-pro with a custom resolution (short edge 1000, closer to 1080) picks the nearest tier 1080p, not wrongly lowered to 720p by "rounding down".

    sora-2-pro 自定义分辨率（短边 1000，更近 1080）选最近档 1080p，不被「向下取整」误降到 720p。"""
    backend = _make_backend(model="sora-2-pro")
    size = await _capture_size(backend, output_path=tmp_path / "o.mp4", aspect_ratio="9:16", resolution="1000x1778")
    assert size == "1080x1920"


@pytest.mark.asyncio
async def test_sora2_base_ignores_1080p_request(tmp_path):
    """sora-2 (base) does not support 1080p: a 1080p request still degrades to 720x1280 (clarity yields to model capability).

    sora-2（base）不支持 1080p：请求 1080p 仍降级为 720x1280（清晰度让位模型能力）。"""
    backend = _make_backend(model="sora-2")
    size = await _capture_size(backend, output_path=tmp_path / "o.mp4", aspect_ratio="9:16", resolution="1080p")
    assert size == "720x1280"


def test_offratio_1024_sizes_dropped():
    """1024x1792 / 1792x1024 (4:7) have been removed from the valid tiers -- ratio-first no longer produces 4:7 videos.

    1024x1792 / 1792x1024（4:7）已从合法档移除——比例优先不再产出 4:7 视频。"""
    assert "1024x1792" not in _SORA_LEGAL_SIZES
    assert "1792x1024" not in _SORA_LEGAL_SIZES
    assert set(_SORA_LEGAL_SIZES) == {"720x1280", "1280x720", "1080x1920", "1920x1080"}
