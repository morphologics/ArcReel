"""DashScopeVideoBackend unit tests (mock httpx, async two-step).

DashScopeVideoBackend 单元测试（mock httpx，异步两步式）。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from arcreel_market_core.video_backend_contract import (
    ReferenceAudioMode,
    ResumeExpiredError,
    VideoCapabilities,
    VideoCapabilityError,
    VideoGenerationRequest,
)
from lib.backends.http_status_errors import ArtifactDownloadError
from lib.backends.providers import PROVIDER_DASHSCOPE
from tests.fakes import bounded_poll_clock, captured_provider_job_ids
from tests.http_capture import capture_http


def _resp(json_body: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    # Same as httpx: text is the raw response body. Both the >=400 log and the rejection-reason summary read it; leaving it a MagicMock
    # would make the stand-in diverge from a real response on the real code path.
    # 与 httpx 一致：text 是响应体原文。>=400 的日志与拒因摘要都读它，留成 MagicMock 会
    # 让替身在真实代码路径上的行为与真响应分叉。
    resp.text = json.dumps(json_body, ensure_ascii=False)
    resp.raise_for_status = MagicMock()
    return resp


def _http_error(status_code: int, message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://x/api/v1/tasks/t")
    response = httpx.Response(status_code, request=request, text=message)
    return httpx.HTTPStatusError(f"error {status_code}", request=request, response=response)


def _http_error_503_in_message(status_code: int) -> httpx.HTTPStatusError:
    """Produce a real HTTPStatusError whose str() contains "503" but whose status code is status_code.

    The raise_for_status message contains the request URL (here task_id carries "503"); the old string fallback would misjudge it as
    retryable; the status-code predicate reads only response.status_code and is unaffected by transient substrings in the URL/message.

    生成 str() 含 "503" 子串、但状态码为 status_code 的真实 HTTPStatusError。

    raise_for_status 的消息包含请求 URL（这里 task_id 带 "503"），旧字符串兜底会据此误判重试；
    状态码谓词只读 response.status_code，不受 URL/消息中瞬态子串影响。
    """
    request = httpx.Request("POST", "https://x/api/v1/tasks/job-503-xyz")
    response = httpx.Response(status_code, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("expected HTTPStatusError")  # pragma: no cover


def _submit(task_id: str = "t-1") -> dict:
    return {"output": {"task_id": task_id, "task_status": "PENDING"}}


def _succeeded(url: str = "https://x/o.mp4", duration: int = 5) -> dict:
    return {
        "output": {"task_status": "SUCCEEDED", "video_url": url},
        "usage": {"duration": duration, "input_video_duration": 0, "output_video_duration": duration},
    }


class _RecordingClient:
    """httpx.AsyncClient stand-in: records the url and params of each post/get; responses come from the stand-ins passed in.

    Contracts such as the submit body, endpoint and auth headers are about "what the outgoing request looks like", so assertions
    target the request contents captured in ``posts`` / ``gets``, not the stand-in's call objects.

    httpx.AsyncClient 替身：记录每次 post/get 的 url 与参数，响应由传入的替身产出。

    提交体、端点、鉴权头这些契约都是「发出去的请求长什么样」，断言落在 ``posts`` / ``gets``
    里的请求内容上，而不是替身的调用对象。
    """

    def __init__(self, *, post: AsyncMock | None = None, get: AsyncMock | None = None) -> None:
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self._post = post or AsyncMock()
        self._get = get or AsyncMock()

    async def post(self, url: str, **kwargs: Any):
        self.posts.append({"url": url, **kwargs})
        return await self._post(url, **kwargs)

    async def get(self, url: str, **kwargs: Any):
        self.gets.append({"url": url, **kwargs})
        return await self._get(url, **kwargs)

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _client(*, post=None, get=None) -> _RecordingClient:
    return _RecordingClient(post=post, get=get)


def _patches(client: _RecordingClient, download: AsyncMock):
    return (
        patch("httpx.AsyncClient", return_value=client),
        patch("lib.backends.video_backends.dashscope.download_resumable_video", download),
        bounded_poll_clock(),
    )


def _ref(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\nfake")
    return p


class TestCapabilities:
    def test_name_and_model(self):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-i2v")
        assert b.name == PROVIDER_DASHSCOPE
        assert b.model == "happyhorse-1.0-i2v"

    def test_happyhorse_r2v_caps(self):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-r2v")
        vc = b.video_capabilities
        assert vc.max_reference_images > 0
        assert vc.max_reference_images == 9
        assert vc.first_frame is False

    def test_happyhorse_11_caps(self):
        """Each of the three 1.1 modalities registers its own capability tier: t2v has no first frame (unregistered, it would fall back to the
        default tier's first_frame=True and be misfiled into the i2v bucket), i2v has a first frame and no reference images, r2v has 9
        reference images and no first frame.

        1.1 三模态各自登记能力档：t2v 无首帧（未登记会回落默认档的 first_frame=True，
        被误判进 i2v 桶），i2v 有首帧无参考图，r2v 参考图 9 张且无首帧。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        t2v = DashScopeVideoBackend.video_capabilities_for_model("happyhorse-1.1-t2v")
        assert t2v.first_frame is False
        assert t2v.max_reference_images == 0

        i2v = DashScopeVideoBackend.video_capabilities_for_model("happyhorse-1.1-i2v")
        assert i2v.first_frame is True
        assert i2v.max_reference_images == 0

        r2v = DashScopeVideoBackend.video_capabilities_for_model("happyhorse-1.1-r2v")
        assert r2v.first_frame is False
        assert r2v.max_reference_images == 9

    def test_default_model_is_happyhorse_11_i2v(self):
        from lib.backends.video_backends.dashscope import DEFAULT_MODEL, DashScopeVideoBackend

        assert DEFAULT_MODEL == "happyhorse-1.1-i2v"
        assert DashScopeVideoBackend(api_key="sk").model == "happyhorse-1.1-i2v"

    def test_wan_r2v_caps(self):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        b = DashScopeVideoBackend(api_key="sk", model="wan2.7-r2v")
        vc = b.video_capabilities
        assert vc.max_reference_images == 5
        assert vc.first_frame is True

    def test_t2v_caps(self):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-t2v")
        assert b.video_capabilities.first_frame is False
        assert b.video_capabilities.max_reference_images == 0

    def test_i2v_caps(self):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        b = DashScopeVideoBackend(api_key="sk", model="wan2.7-i2v")
        assert b.video_capabilities.first_frame is True

    def test_decorated_model_name_resolves_r2v_caps(self):
        """Prefix/suffix-decorated names from relay proxies (infer_endpoint routes by substring to dashscope-async-video) must resolve
        to the real r2v caps, not the unified fallback that drops reference_images.

        代理中转的前缀/后缀装饰名（infer_endpoint 会按子串路由到 dashscope-async-video）
        必须解析出真实 r2v caps，而非退回统一回落 丢掉 reference_images。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        for model, expected_max in (
            ("proxy/happyhorse-1.0-r2v", 9),
            ("provider:wan2.7-r2v", 5),
            ("wan2.7-r2v-0715", 5),  # Suffix version number. / 后缀版本号
            ("Pro/HappyHorse-1.0-R2V", 9),  # Case-insensitive. / 大小写不敏感
        ):
            # Instance side (_build_media builds media from this). / 实例侧（_build_media 据此构造 media）
            b = DashScopeVideoBackend(api_key="sk", model=model)
            assert b.video_capabilities.max_reference_images > 0
            assert b.video_capabilities.max_reference_images == expected_max
            # Resolver side (pure function, no backend constructed). / resolver 侧（纯函数，不构造 backend）
            assert DashScopeVideoBackend.video_capabilities_for_model(model).max_reference_images == expected_max

    def test_alnum_glued_prefix_does_not_resolve_r2v_caps(self):
        """A prefix glued directly to letters/digits (no separator) is not a decorated name and must fall back to the unified fallback.

        The opposite boundary of the previous case: "myhappyhorse-1.0-r2v" minus "my" is character-for-character
        "happyhorse-1.0-r2v", so substring matching without an identifier-boundary check would wrongly hit -- proxy-decorated names
        are told apart from real model names by non-alphanumeric separators like "/" ":", and a directly glued "my" does not qualify.

        字母数字直接粘连的前缀（无分隔符）不算装饰名，须落回统一回落。

        与上一条用例的边界相反："myhappyhorse-1.0-r2v" 去掉 "my" 后与
        "happyhorse-1.0-r2v" 逐字符相同，若子串匹配不做标识符边界校验就会误判命中——
        代理装饰名靠 "/" ":" 等非字母数字分隔符与真实型号名区分，"my" 直接粘连不满足。
        """
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        assert DashScopeVideoBackend.video_capabilities_for_model("myhappyhorse-1.0-r2v").max_reference_images == 0

    @pytest.mark.parametrize("model", ["wan2.7-i2vfoo", "happyhorse-1.0-r2vfoo"])
    def test_alnum_glued_suffix_does_not_resolve_known_profile(self, model):
        """A suffix glued directly to letters/digits is likewise not a decorated name and must fall back to the unified fallback.

        The third boundary direction complementing the previous two cases: "wan2.7-i2vfoo" with "foo" cut off is
        character-for-character the key "wan2.7-i2v"; if substring matching checks only the left boundary and not the right, an unknown
        variant suffix would be truncated and misjudged as a known modality; a numeric suffix ("-0715") satisfies the right boundary
        via "-" and is not constrained by this case.

        字母数字直接粘连的后缀同样不算装饰名，须落回统一回落。

        与前两条用例互补的第三个边界方向："wan2.7-i2vfoo" 截断掉 "foo" 后与 key "wan2.7-i2v"
        逐字符相同，若子串匹配只做左侧边界校验、不做右侧校验，未知变体后缀会被截断误判成已知
        modality；数字后缀（"-0715"）靠 "-" 分隔满足右侧边界，不受本用例约束。
        """
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model(model)
        assert caps.max_reference_images == 0
        assert caps.max_prompt_chars is None

    def test_unknown_bare_series_falls_back_to_default(self):
        """A bare series name without a variant suffix ("happyhorse") cannot tell t2v/i2v/r2v apart -> generic default (no r2v).

        仅系列名无变体后缀（裸 "happyhorse"）无法判别 t2v/i2v/r2v → 通用默认（无 r2v）。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        b = DashScopeVideoBackend(api_key="sk", model="happyhorse")
        assert b.video_capabilities.max_reference_images == 0

    def test_wan27_declares_prompt_char_limit(self):
        """The whole wan2.7 family has prompt <= 5000 characters; over the limit the official API silently truncates and still bills, so it must be blocked before paying.

        wan2.7 全家族 prompt ≤ 5000 字符；超限官方静默截断并照常计费，故须付费前拦。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        for model in ("wan2.7-t2v", "wan2.7-i2v", "wan2.7-r2v"):
            assert DashScopeVideoBackend.video_capabilities_for_model(model).max_prompt_chars == 5000

    def test_models_without_verified_limit_declare_none(self):
        """Models whose limit has not been verified declare none -- undeclared != a limit of 0; declaring one out of thin air would wrongly reject valid requests.

        未取证到上限的 model 不声明——未声明 ≠ 上限 0，凭空声明会误拒合法请求。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        for model in ("happyhorse-1.0-t2v", "happyhorse-1.0-i2v", "happyhorse-1.0-r2v", "happyhorse"):
            assert DashScopeVideoBackend.video_capabilities_for_model(model).max_prompt_chars is None


class TestReferenceToVideo:
    async def test_r2v_happy_path(self, tmp_path: Path):
        post = AsyncMock(return_value=_resp(_submit("t-r2v")))
        get = AsyncMock(return_value=_resp(_succeeded(duration=8)))
        client = _client(post=post, get=get)
        download = AsyncMock()
        ref1, ref2 = _ref(tmp_path, "a.png"), _ref(tmp_path, "b.png")
        p1, p2, p3 = _patches(client, download)
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-r2v")
            result = await b.generate(
                VideoGenerationRequest(
                    prompt="[Image 1] dances",
                    output_path=tmp_path / "o.mp4",
                    reference_images=[ref1, ref2],
                    resolution="720p",
                    aspect_ratio="16:9",
                    duration_seconds=5,
                )
            )

        body = client.posts[-1]["json"]
        assert body["model"] == "happyhorse-1.0-r2v"
        media = body["input"]["media"]
        assert len(media) == 2
        assert all(m["type"] == "reference_image" for m in media)
        assert media[0]["url"].startswith("data:image/png;base64,")
        # resolution uppercase, watermark off, ratio passed through. / resolution 大写、watermark 关、ratio 透传
        assert body["parameters"]["resolution"] == "720P"
        assert body["parameters"]["watermark"] is False
        assert body["parameters"]["ratio"] == "16:9"
        # submit endpoint + async header. / submit 端点 + async 头
        assert client.posts[-1]["url"].endswith("/api/v1/services/aigc/video-generation/video-synthesis")
        assert client.posts[-1]["headers"]["X-DashScope-Async"] == "enable"
        # Billed duration taken from usage.duration (not the requested value 5). / 计费时长取 usage.duration（非请求值 5）
        assert result.duration_seconds == 8
        assert result.provider == PROVIDER_DASHSCOPE
        assert result.task_id == "t-r2v"
        download.assert_called_once()

    async def test_r2v_ref_limit_happyhorse_9(self, tmp_path: Path):
        post = AsyncMock(return_value=_resp(_submit()))
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        refs = [_ref(tmp_path, f"r{i}.png") for i in range(9)]
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-r2v")
            await b.generate(
                VideoGenerationRequest(
                    prompt="p", output_path=tmp_path / "o.mp4", reference_images=refs, resolution="720p"
                )
            )
        assert len(client.posts[-1]["json"]["input"]["media"]) == 9

    async def test_r2v_refs_over_limit_rejected_before_submit(self, tmp_path: Path):
        """Reference images beyond the model limit are not truncated; rejected before submit.

        超出型号上限的参考图不截断，提交前拒绝。"""
        post = AsyncMock(return_value=_resp(_submit()))
        client = _client(post=post, get=AsyncMock())
        refs = [_ref(tmp_path, f"r{i}.png") for i in range(6)]
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-r2v")
            with pytest.raises(VideoCapabilityError) as ei:
                await b.generate(
                    VideoGenerationRequest(
                        prompt="p", output_path=tmp_path / "o.mp4", reference_images=refs, resolution="1080p"
                    )
                )
        assert ei.value.code == "video_reference_images_exceeded"
        assert ei.value.params == {"model": "wan2.7-r2v", "limit": 5, "count": 6}
        post.assert_not_awaited()

    def test_refs_on_model_without_reference_capability_rejected(self, tmp_path: Path):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        with pytest.raises(VideoCapabilityError) as ei:
            DashScopeVideoBackend(api_key="sk", model="wan2.7-i2v")._build_payload(
                VideoGenerationRequest(
                    prompt="p", output_path=tmp_path / "o.mp4", reference_images=[_ref(tmp_path, "r.png")]
                )
            )
        assert ei.value.code == "video_reference_images_unsupported"

    async def test_start_image_rejected_when_model_has_no_first_frame(self, tmp_path: Path):
        # When the model profile declares first_frame=False, a given first frame is not silently dropped: otherwise it bills as usual and the final video does not use it.
        # 型号档案声明 first_frame=False 时带首帧不静默丢弃：否则照常计费，成片没用上首帧
        post = AsyncMock(return_value=_resp(_submit()))
        client = _client(post=post, get=AsyncMock())
        p1, p2, p3 = _patches(client, AsyncMock())
        start = tmp_path / "start.png"
        start.write_bytes(b"\x89PNG")
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-t2v")
            with pytest.raises(VideoCapabilityError) as ei:
                await b.generate(VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", start_image=start))
        assert ei.value.code == "video_first_frame_unsupported"
        post.assert_not_awaited()

    async def test_r2v_all_refs_missing_fail_loud(self, tmp_path: Path):
        # Missing/unreadable r2v reference images (including declared items still left after filtering empty strings) must fail loudly listing names, no silent degradation.
        # r2v 参考图缺失/不可读（含空串过滤后仍有声明项）须 fail-loud 报错列名，不静默退化
        post = AsyncMock(return_value=_resp(_submit()))
        client = _client(post=post, get=AsyncMock())
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-r2v")
            with pytest.raises(VideoCapabilityError) as ei:
                await b.generate(
                    VideoGenerationRequest(
                        prompt="p",
                        output_path=tmp_path / "o.mp4",
                        reference_images=[str(tmp_path / "nope.png"), ""],
                        resolution="720p",
                    )
                )
        assert ei.value.code == "video_reference_images_unreadable"
        assert "nope.png" in ei.value.params["names"]
        # The submit request should never be sent. / 提交请求根本不应发出
        post.assert_not_called()

    async def test_r2v_no_refs_provided_raises(self, tmp_path: Path):
        # r2v model but the caller gave no reference images at all (None/empty) -> required error; no r2v request without media is submitted.
        # r2v 模型但调用方完全未提供参考图（None/空）→ required 错误，不提交无 media 的 r2v 请求
        post = AsyncMock(return_value=_resp(_submit()))
        client = _client(post=post, get=AsyncMock())
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-r2v")
            with pytest.raises(VideoCapabilityError) as ei:
                await b.generate(VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p"))
        assert ei.value.code == "video_reference_images_required"
        post.assert_not_called()

    async def test_r2v_partial_unreadable_refs_fail_loud(self, tmp_path: Path):
        # Some reference images raise OSError on read (is_file passes but reading fails) -> fail loudly, abort and list unreadable file names,
        # rather than silently generating with the readable subset (wrong result, still billed).
        # 部分参考图 read 抛 OSError（is_file 通过但读失败）→ fail-loud 中止并列出不可读文件名，
        # 不静默用可读子集生成（会产出错误结果且照常计费）
        post = AsyncMock(return_value=_resp(_submit("t-r2v")))
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        ra, rb = _ref(tmp_path, "a.png"), _ref(tmp_path, "b.png")

        def fake_uri(p: Path) -> str:
            if p.name == "a.png":
                raise OSError("io error")
            return "data:image/png;base64,OK"

        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3, patch("lib.backends.video_backends.dashscope.image_to_data_uri", side_effect=fake_uri):
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-r2v")
            with pytest.raises(VideoCapabilityError) as ei:
                await b.generate(
                    VideoGenerationRequest(
                        prompt="p",
                        output_path=tmp_path / "o.mp4",
                        reference_images=[str(ra), str(rb)],
                        resolution="720p",
                    )
                )
        assert ei.value.code == "video_reference_images_unreadable"
        assert "a.png" in ei.value.params["names"]
        post.assert_not_called()

    async def test_r2v_all_refs_unreadable_oserror_fail_loud(self, tmp_path: Path):
        # All reference images raise OSError on read -> fail loudly; no r2v request without media is submitted.
        # 全部参考图 read 抛 OSError → fail-loud，不提交无 media 的 r2v 请求
        post = AsyncMock(return_value=_resp(_submit()))
        client = _client(post=post, get=AsyncMock())
        ra = _ref(tmp_path, "a.png")
        p1, p2, p3 = _patches(client, AsyncMock())
        with (
            p1,
            p2,
            p3,
            patch("lib.backends.video_backends.dashscope.image_to_data_uri", side_effect=OSError("denied")),
        ):
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-r2v")
            with pytest.raises(VideoCapabilityError) as ei:
                await b.generate(
                    VideoGenerationRequest(
                        prompt="p", output_path=tmp_path / "o.mp4", reference_images=[str(ra)], resolution="720p"
                    )
                )
        assert ei.value.code == "video_reference_images_unreadable"
        post.assert_not_called()


class TestFirstFrameAndTextOnly:
    async def test_i2v_start_image_oserror_fail_loud(self, tmp_path: Path):
        # A first frame is declared but read raises OSError (permission/IO) -> fail loudly and abort, rather than silently ignoring the first frame and producing a video.
        # 声明了首帧图却 read 抛 OSError（权限/IO）→ fail-loud 中止，不静默忽略首帧照常出片
        post = AsyncMock(return_value=_resp(_submit()))
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        start = _ref(tmp_path, "start.png")
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3, patch("lib.backends.video_backends.dashscope.image_to_data_uri", side_effect=OSError("io")):
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-i2v")
            with pytest.raises(VideoCapabilityError) as ei:
                await b.generate(
                    VideoGenerationRequest(
                        prompt="p", output_path=tmp_path / "o.mp4", start_image=start, resolution="720p"
                    )
                )
        assert ei.value.code == "video_start_image_unreadable"
        assert "start.png" in ei.value.params["name"]
        post.assert_not_called()

    async def test_i2v_first_frame(self, tmp_path: Path):
        post = AsyncMock(return_value=_resp(_submit()))
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        start = _ref(tmp_path, "start.png")
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-i2v")
            await b.generate(
                VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", start_image=start, resolution="720p")
            )
        media = client.posts[-1]["json"]["input"]["media"]
        assert media == [{"type": "first_frame", "url": media[0]["url"]}]
        assert media[0]["url"].startswith("data:image/png;base64,")
        # With a first frame (image-to-video) the aspect ratio follows the first frame: even with a non-empty default aspect_ratio, ratio must not be sent or upstream rejects.
        # 带首帧（图生视频）按首帧定宽高比：默认 aspect_ratio 非空也不得下传 ratio，否则上游拒绝
        assert "ratio" not in client.posts[-1]["json"]["parameters"]

    async def test_t2v_no_media(self, tmp_path: Path):
        post = AsyncMock(return_value=_resp(_submit()))
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-t2v")
            await b.generate(VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="1080p"))
        assert "media" not in client.posts[-1]["json"]["input"]
        assert client.posts[-1]["json"]["parameters"]["resolution"] == "1080P"

    async def test_happyhorse_11_i2v_payload(self, tmp_path: Path):
        """1.1 matches 1.0: watermark explicitly off (official default is on), the 480P tier passed through in uppercase.

        1.1 与 1.0 同口径：水印显式关（官方默认开），480P 档位透传为大写。"""
        post = AsyncMock(return_value=_resp(_submit()))
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        start = _ref(tmp_path, "start.png")
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.1-i2v")
            await b.generate(
                VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", start_image=start, resolution="480p")
            )
        params = client.posts[-1]["json"]["parameters"]
        assert client.posts[-1]["json"]["model"] == "happyhorse-1.1-i2v"
        assert params["watermark"] is False
        assert params["resolution"] == "480P"
        assert client.posts[-1]["json"]["input"]["media"][0]["type"] == "first_frame"


class TestUserRelaxedCapabilities:
    """When the request carries the capabilities from the execution gate with user overrides applied, relaxed dimensions are sent as usual, not rejected by the model profile.

    请求带上执行闸门那份已叠加用户覆盖的能力时，放宽的维度照常发出，不按型号档案拒绝。"""

    @staticmethod
    def _build(tmp_path: Path, model: str, **request_fields) -> dict:
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        return DashScopeVideoBackend(api_key="sk", model=model)._build_payload(
            VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", **request_fields)
        )

    @staticmethod
    def _caps(model: str, **changes) -> VideoCapabilities:
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        return replace(DashScopeVideoBackend.video_capabilities_for_model(model), **changes)

    def test_relaxed_last_frame_sent(self, tmp_path: Path):
        payload = self._build(
            tmp_path,
            "wan2.7-i2v",
            start_image=_ref(tmp_path, "s.png"),
            end_image=_ref(tmp_path, "e.png"),
            video_capabilities=self._caps("wan2.7-i2v", last_frame=True),
        )
        assert [m["type"] for m in payload["input"]["media"]] == ["first_frame", "last_frame"]

    def test_relaxed_first_frame_sent(self, tmp_path: Path):
        payload = self._build(
            tmp_path,
            "wan2.7-t2v",
            start_image=_ref(tmp_path, "s.png"),
            video_capabilities=self._caps("wan2.7-t2v", first_frame=True),
        )
        assert [m["type"] for m in payload["input"]["media"]] == ["first_frame"]

    def test_relaxed_reference_limit_sends_all_references(self, tmp_path: Path):
        refs = [_ref(tmp_path, f"r{i}.png") for i in range(6)]
        payload = self._build(
            tmp_path,
            "wan2.7-r2v",
            reference_images=refs,
            video_capabilities=self._caps("wan2.7-r2v", max_reference_images=6),
        )
        assert [m["type"] for m in payload["input"]["media"]] == ["reference_image"] * 6

    def test_relaxed_references_on_i2v_model_sent_and_not_required(self, tmp_path: Path):
        caps = self._caps("wan2.7-i2v", max_reference_images=2)
        with_refs = self._build(
            tmp_path, "wan2.7-i2v", reference_images=[_ref(tmp_path, "r.png")], video_capabilities=caps
        )
        assert [m["type"] for m in with_refs["input"]["media"]] == ["reference_image"]
        # Relaxing the reference-image limit does not turn an i2v model into "reference images required": a request with only a first frame is still sent.
        # 放宽参考图上限不把 i2v 型号变成「必须带参考图」：只带首帧的请求照旧发出
        frame_only = self._build(tmp_path, "wan2.7-i2v", start_image=_ref(tmp_path, "s.png"), video_capabilities=caps)
        assert [m["type"] for m in frame_only["input"]["media"]] == ["first_frame"]

    def test_narrowed_reference_limit_still_requires_reference_on_r2v_model(self, tmp_path: Path):
        with pytest.raises(VideoCapabilityError) as ei:
            self._build(
                tmp_path,
                "happyhorse-1.0-r2v",
                video_capabilities=self._caps("happyhorse-1.0-r2v", max_reference_images=0),
            )
        assert ei.value.code == "video_reference_images_required"

    def test_metered_reference_count_follows_relaxed_limit(self, tmp_path: Path):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        refs = [_ref(tmp_path, f"r{i}.png") for i in range(6)]
        request = VideoGenerationRequest(
            prompt="p",
            output_path=tmp_path / "o.mp4",
            reference_images=refs,
            video_capabilities=self._caps("wan2.7-r2v", max_reference_images=6),
        )
        assert DashScopeVideoBackend.metered_usage("wan2.7-r2v", request).quantities["input_images"] == 6


class TestPollingAndFailures:
    async def test_polls_through_running(self, tmp_path: Path):
        post = AsyncMock(return_value=_resp(_submit("t3")))
        get = AsyncMock(
            side_effect=[
                _resp({"output": {"task_status": "RUNNING"}}),
                _resp({"output": {"task_status": "RUNNING"}}),
                _resp(_succeeded()),
            ]
        )
        client = _client(post=post, get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-i2v")
            result = await b.generate(
                VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p")
            )
        assert len(client.gets) == 3
        assert result.task_id == "t3"

    async def test_failed_raises(self, tmp_path: Path):
        post = AsyncMock(return_value=_resp(_submit()))
        get = AsyncMock(return_value=_resp({"output": {"task_status": "FAILED", "code": "X", "message": "boom"}}))
        client = _client(post=post, get=get)
        download = AsyncMock()
        p1, p2, p3 = _patches(client, download)
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-t2v")
            with pytest.raises(RuntimeError, match="boom"):
                await b.generate(VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p"))
        download.assert_not_called()

    async def test_generate_unknown_raises_runtime(self, tmp_path: Path):
        post = AsyncMock(return_value=_resp(_submit("t-new")))
        get = AsyncMock(return_value=_resp({"output": {"task_status": "UNKNOWN"}}))
        client = _client(post=post, get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-t2v")
            with pytest.raises(RuntimeError) as ei:
                await b.generate(VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p"))
            assert "expired" in str(ei.value).lower()
            assert not isinstance(ei.value, ResumeExpiredError)


class TestResume:
    async def test_download_failure_can_resume_without_resubmit(self, tmp_path: Path):
        download_ready = False

        def download_response(_request: httpx.Request) -> httpx.Response:
            if download_ready:
                return httpx.Response(200, content=b"resumed")
            return httpx.Response(503, text="cdn unavailable")

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids() as persisted:
            submit = router.post(
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
            ).mock(return_value=httpx.Response(200, json=_submit("task-download")))
            router.get("https://dashscope.aliyuncs.com/api/v1/tasks/task-download").mock(
                return_value=httpx.Response(200, json=_succeeded(url="https://x/r.mp4"))
            )
            router.get("https://x/r.mp4").mock(side_effect=download_response)
            request = VideoGenerationRequest(
                prompt="p",
                output_path=tmp_path / "o.mp4",
                resolution="720p",
                task_id="worker-task",
                poll_timeout_seconds=1800,
            )
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            backend = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-t2v")
            with pytest.raises(ArtifactDownloadError) as caught:
                await backend.generate(request)

            assert caught.value.code == "artifact_download_failed"
            download_ready = True
            result = await backend.resume_video(persisted[0]["job_id"], request)

            assert submit.call_count == 1

        assert result.video_path.read_bytes() == b"resumed"

    async def test_resume_polls_without_post(self, tmp_path: Path):
        post = AsyncMock(side_effect=AssertionError("resume 不应 POST"))
        get = AsyncMock(return_value=_resp(_succeeded(url="https://x/r.mp4")))
        client = _client(post=post, get=get)
        download = AsyncMock()
        p1, p2, p3 = _patches(client, download)
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-i2v")
            result = await b.resume_video(
                "t-resume",
                VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p"),
            )
        post.assert_not_called()
        assert client.gets[-1]["url"].endswith("/tasks/t-resume")
        assert result.task_id == "t-resume"

    async def test_resume_unknown_raises_resume_expired(self, tmp_path: Path):
        get = AsyncMock(return_value=_resp({"output": {"task_status": "UNKNOWN"}}))
        client = _client(get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-r2v")
            with pytest.raises(ResumeExpiredError) as ei:
                await b.resume_video(
                    "t-exp",
                    VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p"),
                )
            assert ei.value.job_id == "t-exp"
            assert ei.value.provider == PROVIDER_DASHSCOPE

    async def test_resume_404_raises_without_retry(self, tmp_path: Path):
        not_found = _resp({"error": "nope"}, status_code=404)
        not_found.raise_for_status = MagicMock(side_effect=_http_error(404, "not found"))
        get = AsyncMock(return_value=not_found)
        client = _client(get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-r2v")
            with pytest.raises(ResumeExpiredError):
                await b.resume_video(
                    "t-404",
                    VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p"),
                )
            assert len(client.gets) == 1


class TestPersist:
    async def test_persist_called_with_task_id(self, tmp_path: Path):
        post = AsyncMock(return_value=_resp(_submit("job-9")))
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3, captured_provider_job_ids() as persisted:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-i2v")
            await b.generate(
                VideoGenerationRequest(
                    prompt="p", output_path=tmp_path / "o.mp4", resolution="720p", task_id="db-task-1"
                )
            )
        assert [(r["task_id"], r["job_id"], r["provider"]) for r in persisted] == [
            ("db-task-1", "job-9", PROVIDER_DASHSCOPE)
        ]


class TestSubmit413:
    async def test_submit_413_surfaces_httpstatuserror_no_retry(self, tmp_path: Path):
        err413 = _resp({"code": "PayloadTooLarge"}, status_code=413)
        err413.raise_for_status = MagicMock(side_effect=_http_error(413, "Request Entity Too Large"))
        post = AsyncMock(return_value=err413)
        client = _client(post=post)
        download = AsyncMock()
        ref1 = _ref(tmp_path, "a.png")
        p1, p2, p3 = _patches(client, download)
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-r2v")
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await b.generate(
                    VideoGenerationRequest(
                        prompt="[Image 1] x",
                        output_path=tmp_path / "o.mp4",
                        reference_images=[ref1],
                        resolution="720p",
                        aspect_ratio="16:9",
                        duration_seconds=5,
                    )
                )
        # Keep status_code so the choke-point layer recognizes 413; 413 is not retryable -> fail fast, single submit.
        # 保留 status_code 让咽喉层识别 413；413 非 retryable → fail-fast 单次提交
        assert ei.value.response.status_code == 413
        assert len(client.posts) == 1
        download.assert_not_called()


class TestRetryStatusGating:
    """Submit/poll decide retries by HTTP status_code, eliminating substring misjudgments.

    提交/轮询按 HTTP status_code 决定重试，消除字符串子串误判。"""

    async def test_submit_4xx_with_503_substring_no_retry(self, tmp_path: Path):
        # A 4xx error message containing "503" (URL/task_id): the old string fallback would retry by mistake; the new predicate fails fast on 400.
        # 4xx 错误消息里带 "503" 子串（URL/task_id）：旧字符串兜底会误判重试，新谓词按 400 fail-fast。
        err = _http_error_503_in_message(400)
        assert "503" in str(err)
        bad = _resp({"code": "InvalidParameter"}, status_code=400)
        bad.raise_for_status = MagicMock(side_effect=err)
        post = AsyncMock(return_value=bad)
        client = _client(post=post)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3, bounded_poll_clock():
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="wan2.7-t2v")
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await b.generate(VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p"))
        assert ei.value.response.status_code == 400
        assert len(client.posts) == 1

    async def test_submit_real_503_retries_then_succeeds(self, tmp_path: Path):
        # A real 5xx: retried by status_code, succeeds on the third attempt. / 真 5xx：按 status_code 重试，第三次成功。
        err503 = _resp({"code": "ServiceUnavailable"}, status_code=503)
        err503.raise_for_status = MagicMock(side_effect=_http_error_503_in_message(503))
        post = AsyncMock(side_effect=[err503, err503, _resp(_submit("t-ok"))])
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3, bounded_poll_clock():
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-t2v")
            result = await b.generate(
                VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p")
            )
        assert len(client.posts) == 3
        assert result.task_id == "t-ok"

    async def test_submit_connect_error_retries(self, tmp_path: Path):
        # Network-level errors (connection definitely not delivered) are still retried. / 网络层错误（连接确定未送达）维持重试。
        post = AsyncMock(side_effect=[httpx.ConnectError("refused"), _resp(_submit("t-ok"))])
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3, bounded_poll_clock():
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-t2v")
            result = await b.generate(
                VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p")
            )
        assert len(client.posts) == 2
        assert result.task_id == "t-ok"

    async def test_poll_timeout_retries(self, tmp_path: Path):
        # Network-level Timeout on polling (idempotent GET) is still retried. / 轮询（幂等 GET）网络层 Timeout 维持重试。
        post = AsyncMock(return_value=_resp(_submit("t-poll")))
        get = AsyncMock(side_effect=[httpx.TimeoutException("read timed out"), _resp(_succeeded())])
        client = _client(post=post, get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            from lib.backends.video_backends.dashscope import DashScopeVideoBackend

            b = DashScopeVideoBackend(api_key="sk", model="happyhorse-1.0-i2v")
            result = await b.generate(
                VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", resolution="720p")
            )
        assert len(client.gets) == 2
        assert result.task_id == "t-poll"


class TestWan27ReferenceVoice:
    """wan2.7-r2v voices are not separate media entries but a reference_voice field on the reference material items.

    wan2.7-r2v 的音色不是独立 media 条目，而是挂在参考素材项上的 reference_voice 字段。"""

    @staticmethod
    def _backend():
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        return DashScopeVideoBackend(api_key="sk", model="wan2.7-r2v")

    @staticmethod
    def _refs(tmp_path, count: int) -> list:
        out = []
        for i in range(count):
            p = tmp_path / f"r{i}.png"
            p.write_bytes(b"\x89PNG\r\n")
            out.append(p)
        return out

    @staticmethod
    def _audio(tmp_path, name: str) -> Path:
        p = tmp_path / name
        p.write_bytes(b"riff-audio")
        return p

    def test_r2v_declares_audio_capability(self):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model("wan2.7-r2v")
        assert caps.reference_audio_mode is ReferenceAudioMode.DIRECT
        # Audio is attached per reference material item, so the max number of audio segments equals the max number of reference materials.
        # 音频逐段挂参考素材项，段数上限即参考素材总数上限
        assert caps.max_reference_audio_count == 5

    def test_i2v_declares_no_audio_capability(self):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model("wan2.7-i2v")
        assert caps.reference_audio_mode is ReferenceAudioMode.NONE

    def test_audio_attached_to_reference_items_in_order(self, tmp_path):
        payload = self._backend()._build_payload(
            VideoGenerationRequest(
                prompt="两人对话",
                output_path=tmp_path / "o.mp4",
                reference_images=self._refs(tmp_path, 2),
                reference_audio_files=[self._audio(tmp_path, "a.mp3"), self._audio(tmp_path, "b.wav")],
            )
        )

        refs = [m for m in payload["input"]["media"] if m["type"] == "reference_image"]
        assert len(refs) == 2
        assert refs[0]["reference_voice"].startswith("data:audio/mpeg;base64,")
        assert refs[1]["reference_voice"].startswith("data:audio/wav;base64,")

    def test_reference_audio_targets_align_by_explicit_index_not_position(self, tmp_path):
        """Reference audio order (first appearance of the dialogue line speaker) and reference image order (first mention) are derived independently and not naturally aligned.

        Scenario: references = [scene, Zhang San] (image 0 = scene, image 1 = Zhang San), audio_speakers = [Zhang San] (the only
        character who speaks). Aligning by position would attach Zhang San's voice to the scene image; targets=[1] explicitly declares
        that Zhang San's voice goes with image 1 and must be aligned to the correct reference_items[1].

        参考音频顺序（台词 speaker 首现）与参考图顺序（mention 首现）独立派生，不天然同序。

        场景：references = [场景, 张三]（图0=场景，图1=张三），audio_speakers = [张三]（唯一
        开口的角色）。若按位置对齐会把张三的声音错挂到场景图上；targets=[1] 显式声明张三的
        声音配图 1，须对齐到正确的 reference_items[1]。
        """
        payload = self._backend()._build_payload(
            VideoGenerationRequest(
                prompt="场景先出现，张三说话",
                output_path=tmp_path / "o.mp4",
                reference_images=self._refs(tmp_path, 2),
                reference_audio_files=[self._audio(tmp_path, "zhangsan.mp3")],
                reference_audio_targets=[1],
            )
        )

        refs = [m for m in payload["input"]["media"] if m["type"] == "reference_image"]
        assert "reference_voice" not in refs[0]
        assert refs[1]["reference_voice"].startswith("data:audio/mpeg;base64,")

    def test_reference_audio_targets_out_of_range_raises_slots_insufficient(self, tmp_path):
        with pytest.raises(VideoCapabilityError) as exc:
            self._backend()._build_payload(
                VideoGenerationRequest(
                    prompt="x",
                    output_path=tmp_path / "o.mp4",
                    reference_images=self._refs(tmp_path, 1),
                    reference_audio_files=[self._audio(tmp_path, "a.mp3")],
                    reference_audio_targets=[5],
                )
            )

        assert exc.value.code == "video_reference_audio_slots_insufficient"

    def test_reference_audio_targets_duplicate_index_raises_instead_of_silently_overwriting(self, tmp_path):
        """When two audio segments point at the same reference material item, per-item assignment would silently overwrite the earlier binding -- must fail hard.

        两段音频指向同一个参考素材项时，逐条赋值会静默覆盖前一条绑定——必须硬失败。"""
        with pytest.raises(VideoCapabilityError) as exc:
            self._backend()._build_payload(
                VideoGenerationRequest(
                    prompt="两人对话",
                    output_path=tmp_path / "o.mp4",
                    reference_images=self._refs(tmp_path, 2),
                    reference_audio_files=[self._audio(tmp_path, "a.mp3"), self._audio(tmp_path, "b.wav")],
                    reference_audio_targets=[0, 0],
                )
            )

        assert exc.value.code == "video_reference_audio_slots_insufficient"

    def test_fewer_audios_than_references_leaves_rest_unvoiced(self, tmp_path):
        payload = self._backend()._build_payload(
            VideoGenerationRequest(
                prompt="一人说话",
                output_path=tmp_path / "o.mp4",
                reference_images=self._refs(tmp_path, 3),
                reference_audio_files=[self._audio(tmp_path, "a.mp3")],
            )
        )

        refs = [m for m in payload["input"]["media"] if m["type"] == "reference_image"]
        assert "reference_voice" in refs[0]
        assert all("reference_voice" not in r for r in refs[1:])

    def test_no_audio_leaves_reference_items_untouched(self, tmp_path):
        payload = self._backend()._build_payload(
            VideoGenerationRequest(
                prompt="无对白",
                output_path=tmp_path / "o.mp4",
                reference_images=self._refs(tmp_path, 2),
            )
        )

        refs = [m for m in payload["input"]["media"] if m["type"] == "reference_image"]
        assert all("reference_voice" not in r for r in refs)

    def test_more_audios_than_references_raises(self, tmp_path):
        """Audio that cannot be attached is not dropped: silently dropping it would make a character's voice declaration silently ineffective while still billing.

        The blocker is "not enough attachable reference materials", which differs from the gate's "exceeds the model's capability
        limit", so each has its own code: the suggested fixes are opposite (add reference images vs. remove characters), and sharing
        one would give a hint that does not match the real blocker.

        挂不上的音频不丢弃：静默丢弃会让某个角色的音色声明无声失效，且照常扣费。

        卡点是"可挂载的参考素材不够"，与 gate 的"超出模型能力上限"是两回事，故各用一个
        code：两者的处置建议相反（补参考图 vs 减角色），共用会给出与实际卡点不符的提示。
        """
        with pytest.raises(VideoCapabilityError) as exc:
            self._backend()._build_payload(
                VideoGenerationRequest(
                    prompt="三人对话",
                    output_path=tmp_path / "o.mp4",
                    reference_images=self._refs(tmp_path, 1),
                    reference_audio_files=[self._audio(tmp_path, "a.mp3"), self._audio(tmp_path, "b.mp3")],
                )
            )

        assert exc.value.code == "video_reference_audio_slots_insufficient"
        assert exc.value.params["slots"] == 1
        assert exc.value.params["count"] == 2

    def test_model_without_reference_images_rejects_audio_instead_of_dropping(self, tmp_path):
        """A model without reference image capability receiving audio must error, not silently drop it.

        Reachable path: a custom provider overrides the endpoint-level reference_audio_mode to direct (that endpoint's delegate does
        send audio), but the specific model uses a tier without reference materials like wan2.7-i2v -- the audio has nowhere to attach.
        Dropping it would generate a video with a random voice and still bill.

        无参考图能力的 model 收到音频要报错，不静默丢弃。

        可达路径：自定义供应商把 endpoint 级的 reference_audio_mode 覆盖成 direct（该 endpoint
        的 delegate 确实会下传音频），但具体 model 走 wan2.7-i2v 这类无参考素材的档位——
        音频无处挂载。丢弃会生成一段音色随机的视频并照常扣费。
        """
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        with pytest.raises(VideoCapabilityError) as exc:
            DashScopeVideoBackend(api_key="sk", model="wan2.7-i2v")._build_payload(
                VideoGenerationRequest(
                    prompt="独白",
                    output_path=tmp_path / "o.mp4",
                    start_image=self._refs(tmp_path, 1)[0],
                    reference_audio_files=[self._audio(tmp_path, "a.mp3")],
                )
            )

        assert exc.value.code == "video_reference_audio_unsupported"

    def test_missing_audio_file_raises(self, tmp_path):
        with pytest.raises(VideoCapabilityError) as exc:
            self._backend()._build_payload(
                VideoGenerationRequest(
                    prompt="x",
                    output_path=tmp_path / "o.mp4",
                    reference_images=self._refs(tmp_path, 1),
                    reference_audio_files=[tmp_path / "missing.mp3"],
                )
            )

        assert exc.value.code == "video_reference_audio_unreadable"

    def test_unsupported_audio_format_raises(self, tmp_path):
        with pytest.raises(VideoCapabilityError) as exc:
            self._backend()._build_payload(
                VideoGenerationRequest(
                    prompt="x",
                    output_path=tmp_path / "o.mp4",
                    reference_images=self._refs(tmp_path, 1),
                    reference_audio_files=[self._audio(tmp_path, "a.ogg")],
                )
            )

        assert exc.value.code == "video_reference_audio_format_unsupported"


class TestWan2Aliases:
    """Capability tier resolution for wan2.7 model_ids: hyphen/underscore aliases and the dotted form must map to the same tier;
    WAN2_PATTERN only recognizes 2.7, other 2.x minor versions (2.1/2.2 etc.) are outside this regex's authority (see the note at WAN2_PATTERN).

    wan2.7 的 model_id 能力档解析：连字符/下划线别名与点号形态须归同一档；WAN2_PATTERN 只认
    2.7，其余 2.x 小版本（2.1/2.2 等）不在本正则确权范围内（见 WAN2_PATTERN 处的说明）。
    """

    @pytest.mark.parametrize("model", ["wan-2.7-r2v", "wan_2.7-r2v", "wan_2.7_r2v"])
    def test_alias_forms_get_wan27_r2v_capabilities(self, model):
        """Hyphen/underscore wan2.7 aliases returned by discovery (already routed to this backend by endpoints.py) must be recognized as
        wan2.7-r2v, not fall back to the default profile.

        Shares WAN2_PATTERN with endpoints.infer_endpoint: if the two differ in breadth we get the contradiction "routed to this backend
        but treated as a generic model, losing reference image/first frame params". Aliases like "wan_2.7_r2v" that also use an
        underscore between the version and the modality suffix need that separator normalized to a hyphen to match the _MODEL_PROFILES keys.

        discovery 返回的连字符/下划线 wan2.7 别名（endpoints.py 已路由到本后端）须认作
        wan2.7-r2v，不落回默认档案。

        与 endpoints.infer_endpoint 共用 WAN2_PATTERN：两处不同宽即会出现"路由到本后端却被当
        通用型号丢失参考图/首帧参数"的矛盾。"wan_2.7_r2v" 这类版本号与模态后缀之间也用下划线
        分隔的别名，须额外把该分隔符归一化成连字符才能匹配 _MODEL_PROFILES 的 key。
        """
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model(model)
        assert caps.first_frame is True
        assert caps.max_reference_images == 5
        assert caps.max_reference_audio_count == 5

    @pytest.mark.parametrize("model", ["wan-2.7-image-to-video", "wan_2.7-image2video", "wan_2.7_image_to_video"])
    def test_image_to_video_alias_gets_wan27_i2v_capabilities(self, model):
        """Hyphen/underscore image-to-video continuation aliases still carry that suffix after normalization (e.g.
        "wan2.7-image-to-video"), which is not a substring match for "wan2.7-i2v" in _MODEL_PROFILES; the suffix must first be folded
        to "i2v" before lookup, otherwise it silently falls to the default tier, loses first_frame, and _build_media then does not send
        start_image.

        连字符/下划线形态的 image-to-video 续接别名归一化后仍带该后缀（如
        "wan2.7-image-to-video"），不与 _MODEL_PROFILES 的 "wan2.7-i2v" 构成子串关系；须先把
        该后缀折成 "i2v" 再查表，否则静默落默认档、丢失 first_frame，_build_media 据此不下发
        start_image。
        """
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model(model)
        assert caps.first_frame is True
        # r2v also has first_frame=True; max_reference_images tells the two apart: i2v has no reference image capability,
        # and without this assertion a normalization that wrongly lands on the r2v profile would still pass.
        # r2v 同样 first_frame=True，靠 max_reference_images 区分二者：i2v 无参考图能力，
        # 缺这条断言时归一化误落 r2v 档案也会通过。
        assert caps.max_reference_images == 0
        assert caps.max_prompt_chars == 5000

    def test_fully_underscored_t2v_alias_does_not_get_default_first_frame(self):
        """"wan_2.7_t2v" separates both the version and the modality suffix with underscores; t2v has no first-frame capability, exactly
        the opposite of the unified fallback's default first_frame=True, which verifies it really resolved to wan2.7-t2v rather than silently falling back.

         "wan_2.7_t2v" 的版本号与模态后缀均用下划线分隔；t2v 无首帧能力，与统一回落
        的默认 first_frame=True 恰好相反，能验证确实解析到了 wan2.7-t2v 而非静默落回默认档。
        """
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model("wan_2.7_t2v")
        assert caps.first_frame is False
        assert caps.max_prompt_chars == 5000

    @pytest.mark.parametrize("model", ["swan2", "vendorwan2", "wan20", "swan2.7-r2v", "vendorwan2.7-t2v"])
    def test_substring_without_boundary_does_not_get_wan2_capabilities(self, model):
        """Model names containing "wan2" but without non-alphanumeric boundaries on both sides must not be misjudged as the Wan 2.x family.

        Full aliases like "swan2.7-r2v" / "vendorwan2.7-t2v" (letters glued on the left, a valid modality suffix on the right) cannot be
        stopped by WAN2_PATTERN's boundary anchors alone -- what hits is the fallback substring match at the end of _profile_for_model:
        "swan2.7-r2v" minus its first character is character-for-character the _MODEL_PROFILES key "wan2.7-r2v", so that loop must also
        check the left boundary for each key to avoid a false hit.

        含 "wan2" 子串但两侧非字母数字边界不成立的型号名，不得被误判为万相 2.x 家族。

        "swan2.7-r2v" / "vendorwan2.7-t2v" 这类完整别名（左侧紧贴字母、右侧带合法模态后缀）单靠
        WAN2_PATTERN 的边界锚点无法拦下——命中的是 _profile_for_model 末尾的兜底子串匹配：
        "swan2.7-r2v" 去掉首字符后与 _MODEL_PROFILES 的 key "wan2.7-r2v" 逐字符相同，该循环须
        对每个 key 同样做左侧边界校验才不会误判命中。
        """
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model(model)
        assert caps.max_reference_images == 0
        assert caps.max_prompt_chars is None

    @pytest.mark.parametrize("model", ["wan-2.1-r2v", "wan_2.2-i2v"])
    def test_non_27_alias_forms_fall_back_to_default_profile(self, model):
        """WAN2_PATTERN only recognizes 2.7: hyphen/underscore forms of other 2.x minor versions do not land on the wan2.7 capability tier --
        the endpoint this backend always requests does not match those versions' actual protocol (see the note at WAN2_PATTERN), and
        endpoints.py does not route them to this backend either; this confirms capability tier resolution does not overreach either.

        WAN2_PATTERN 只认 2.7：连字符/下划线形态的其余 2.x 小版本不落 wan2.7 能力档——
        本后端固定请求的端点与这些小版本的实际协议不符（见 WAN2_PATTERN 处的说明），
        endpoints.py 也不会把它们路由到本后端；此处确认能力档解析同样不越界。
        """
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model(model)
        assert caps.max_reference_images == 0
        assert caps.max_prompt_chars is None


class TestWan3:
    """wan3.0-video: one model covers all three paths, first/last frames + separate reference audio entries + a controllable audio track.

    wan3.0-video：单模型通吃三条路径，首尾帧 + 独立参考音频条目 + 可控音轨。"""

    @staticmethod
    def _backend(**kwargs):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        return DashScopeVideoBackend(api_key="sk", model="wan3.0-video", **kwargs)

    @staticmethod
    def _file(tmp_path, name: str, data: bytes = b"\x89PNG\r\n") -> Path:
        p = tmp_path / name
        p.write_bytes(data)
        return p

    def test_declares_capabilities(self):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model("wan3.0-video")
        assert caps.first_frame is True
        assert caps.last_frame is True
        assert caps.max_reference_images == 10
        assert caps.reference_audio_mode is ReferenceAudioMode.DIRECT
        assert caps.max_reference_audio_count == 5
        assert caps.max_reference_audio_total_seconds == 15.0
        assert caps.max_prompt_chars == 20000
        # Audio is a separate media entry, not attached to reference material items (the opposite of wan2.7-r2v).
        # 音频是独立 media 条目，不挂在参考素材项上（与 wan2.7-r2v 相反）
        assert caps.reference_audio_per_image is False

    @pytest.mark.parametrize("model", ["wan-3-turbo", "wan3-turbo", "wan_3-turbo", "wan_3_turbo"])
    def test_alias_forms_get_wan3_capabilities(self, model):
        """Hyphen/underscore aliases returned by discovery (already routed to this backend by endpoints.py) must be recognized as wan3.0, not fall back to the default profile.

        Shares WAN3_PATTERN with endpoints.infer_endpoint / duration_presets: if the three differ in breadth we get the contradiction
        "routed to this backend but treated as a generic model, losing reference image/end frame/audio track params".

        discovery 返回的连字符/下划线别名（endpoints.py 已路由到本后端）须认作 wan3.0，不落回默认档案。

        与 endpoints.infer_endpoint / duration_presets 共用 WAN3_PATTERN：三处不同宽即会出现
        "路由到本后端却被当通用型号丢失参考图/尾帧/音轨参数" 的矛盾。
        """
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model(model)
        assert caps.last_frame is True
        assert caps.max_reference_images == 10
        assert caps.reference_audio_mode is ReferenceAudioMode.DIRECT
        assert caps.max_prompt_chars == 20000

    @pytest.mark.parametrize("model", ["swan3", "vendorwan3", "wan30"])
    def test_wan3_substring_without_boundary_does_not_get_wan3_capabilities(self, model):
        """Model names containing "wan3" but without non-alphanumeric boundaries on both sides must not be misjudged as the Wan 3.0 family.

        含 "wan3" 子串但两侧非字母数字边界不成立的型号名，不得被误判为万相 3.0 家族。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        caps = DashScopeVideoBackend.video_capabilities_for_model(model)
        assert caps.max_reference_images == 0
        assert caps.max_prompt_chars is None

    def test_first_and_last_frame_in_media(self, tmp_path):
        payload = self._backend()._build_payload(
            VideoGenerationRequest(
                prompt="p",
                output_path=tmp_path / "o.mp4",
                start_image=self._file(tmp_path, "s.png"),
                end_image=self._file(tmp_path, "e.png"),
            )
        )
        types = [m["type"] for m in payload["input"]["media"]]
        assert types == ["first_frame", "last_frame"]
        # With a first frame present, ratio is not sent (upstream takes the ratio from the first frame). / 首帧在场即不下发 ratio（上游按首帧定比例）
        assert "ratio" not in payload["parameters"]

    def test_reference_audio_is_standalone_media_item(self, tmp_path):
        payload = self._backend()._build_payload(
            VideoGenerationRequest(
                prompt="p",
                output_path=tmp_path / "o.mp4",
                reference_images=[self._file(tmp_path, "r0.png")],
                reference_audio_files=[
                    self._file(tmp_path, "a.mp3", b"riff"),
                    self._file(tmp_path, "b.wav", b"riff"),
                ],
            )
        )
        media = payload["input"]["media"]
        assert [m["type"] for m in media] == ["reference_image", "reference_audio", "reference_audio"]
        # The order is the contract for "audio N" references in the prompt; must not be reordered. / 顺序即 prompt 中「音频N」的指认契约，不得重排
        assert media[1]["url"].startswith("data:audio/mpeg;base64,")
        assert media[2]["url"].startswith("data:audio/wav;base64,")
        # Audio is not attached to reference image items. / 音频不挂在参考图项上
        assert "reference_voice" not in media[0]

    def test_reference_images_optional(self, tmp_path):
        """Image/text-to-video requests to the all-in-one model have no reference images and must not be judged required like the r2v-only models.

        通吃型号的图生/文生请求没有参考图，不能按 r2v 专用型号那样判 required。"""
        payload = self._backend()._build_payload(
            VideoGenerationRequest(
                prompt="p", output_path=tmp_path / "o.mp4", start_image=self._file(tmp_path, "s.png")
            )
        )
        assert [m["type"] for m in payload["input"]["media"]] == ["first_frame"]

    @pytest.mark.parametrize("generate_audio", [True, False])
    def test_audio_switch_is_sent(self, tmp_path, generate_audio):
        payload = self._backend()._build_payload(
            VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", generate_audio=generate_audio)
        )
        assert payload["parameters"]["audio"] is generate_audio

    @pytest.mark.parametrize("model", ["wan2.7-i2v", "happyhorse-1.1-i2v"])
    def test_audio_switch_not_sent_for_always_on_models(self, tmp_path, model):
        """Always-audio models reject this param upstream as invalid.

        恒有声型号收到该参数会被上游当非法参数拒。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        payload = DashScopeVideoBackend(api_key="sk", model=model)._build_payload(
            VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", generate_audio=True)
        )
        assert "audio" not in payload["parameters"]

    @pytest.mark.parametrize("model", ["wan2.7-i2v", "happyhorse-1.1-i2v"])
    def test_silent_request_on_always_on_model_rejected(self, tmp_path, model):
        """Always-audio models cannot send a silent request; reject rather than produce a final video with audio as usual.

        恒有声型号发不出静音请求，拒绝而不是照常出有声成片。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        with pytest.raises(VideoCapabilityError) as ei:
            DashScopeVideoBackend(api_key="sk", model=model)._build_payload(
                VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", generate_audio=False)
            )
        assert ei.value.code == "video_audio_switch_not_supported"

    def test_silent_request_on_unrecognized_model_not_rejected(self, tmp_path):
        """Unknown models declare it controllable; a silent request is assembled as usual without sending the switch.

        认不得的型号声明可控，静音请求照常组装、不下发开关。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        payload = DashScopeVideoBackend(api_key="sk", model="proxy-custom-video")._build_payload(
            VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", generate_audio=False)
        )
        assert "audio" not in payload["parameters"]

    def test_end_frame_on_model_without_last_frame_rejected(self, tmp_path):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        with pytest.raises(VideoCapabilityError) as ei:
            DashScopeVideoBackend(api_key="sk", model="wan2.7-i2v")._build_payload(
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    start_image=self._file(tmp_path, "s.png"),
                    end_image=self._file(tmp_path, "e.png"),
                )
            )
        assert ei.value.code == "video_last_frame_unsupported"

    @pytest.mark.parametrize("model", ["wan-3-turbo", "wan3-turbo", "wan_3-turbo", "wan_3_turbo"])
    def test_audio_switch_is_sent_for_alias_forms(self, tmp_path, model):
        """Aliases are likewise dispatched as controllable-audio models, not falling back to the always-audio default profile.

        别名同样按可控音轨型号分派，不落回恒有声默认档案。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        payload = DashScopeVideoBackend(api_key="sk", model=model)._build_payload(
            VideoGenerationRequest(prompt="p", output_path=tmp_path / "o.mp4", generate_audio=False)
        )
        assert payload["parameters"]["audio"] is False

    def test_prompt_over_limit_rejected_before_submit(self, tmp_path):
        """Over the limit the remote end silently truncates and still bills, so the gate rejects before paying.

        超限对端静默截断且照常计费，故由 gate 在付费前拒。"""
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend
        from lib.backends.video_frame_slots import gate_video_request

        caps = DashScopeVideoBackend.video_capabilities_for_model("wan3.0-video")
        with pytest.raises(VideoCapabilityError) as exc:
            gate_video_request(
                caps=caps,
                provider=PROVIDER_DASHSCOPE,
                model="wan3.0-video",
                prompt="超" * 20001,
            )
        assert exc.value.code == "video_prompt_too_long"

    async def test_dedicated_base_url_used_for_submit_and_poll(self):
        b = self._backend(wan3_base_url="https://maas-cn-hangzhou.example.com/ws-123/api/v1/")
        # Trailing slash normalized; submit and poll use the same domain (a task id can only be queried on the endpoint that created it).
        # 尾部斜杠归一，提交与轮询同域名（任务 id 只在创建它的 endpoint 上可查）
        assert b._request_base_url == "https://maas-cn-hangzhou.example.com/ws-123/api/v1"
        # Assert the POST/GET URLs actually sent, not just read-only properties -- if both places reverted to a fixed base_url, property assertions would stay green.
        # 断言实际发出的 POST/GET URL，而非只读属性——两处若改回固定 base_url，属性断言仍会绿
        post = AsyncMock(return_value=_resp(_submit("t-wan3")))
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        assert await b._create_task(client, {}) == "t-wan3"
        await b._poll_once(client, "t-wan3", b._request_base_url)
        from lib.backends.video_backends.dashscope import _VIDEO_ENDPOINT

        assert client.posts[-1]["url"] == f"https://maas-cn-hangzhou.example.com/ws-123/api/v1{_VIDEO_ENDPOINT}"
        assert client.gets[-1]["url"] == "https://maas-cn-hangzhou.example.com/ws-123/api/v1/tasks/t-wan3"

    def test_falls_back_to_shared_base_url(self):
        b = self._backend()
        assert b._request_base_url == b._base_url
        assert b._request_base_url.endswith("/api/v1")

    def test_dedicated_base_url_not_applied_to_other_models(self):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        b = DashScopeVideoBackend(api_key="sk", model="wan2.7-i2v", wan3_base_url="https://maas.example.com/api/v1")
        assert b._request_base_url == b._base_url

    async def test_submit_persists_actual_base_url(self, tmp_path: Path):
        """On submit, the domain actually used is stored with job_id -- resume relies on it for replay.

        提交时把实际使用的域名与 job_id 一并落库——续跑要靠它回放。"""
        post = AsyncMock(return_value=_resp(_submit("t-wan3")))
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3, captured_provider_job_ids() as persisted:
            b = self._backend(wan3_base_url="https://maas-a.example.com/ws-1/api/v1")
            await b.generate(
                VideoGenerationRequest(
                    prompt="p", output_path=tmp_path / "o.mp4", resolution="720p", task_id="db-task-1"
                )
            )

        assert [(r["base_url"], r["endpoint"]) for r in persisted] == [("https://maas-a.example.com/ws-1/api/v1", None)]

    async def test_resume_polls_submitted_base_url_after_config_change(self, tmp_path: Path):
        """Resume after changing wan3_base_url mid-flight: polling still hits the domain used at submit, not the one resolved from the current config.

        在途改 wan3_base_url 后续跑：轮询仍打提交时的域名，而非当下配置解析出的域名。"""
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=AsyncMock(), get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            # The config has been changed to B; submit used A. / 配置已被改成 B，提交时用的是 A
            b = self._backend(wan3_base_url="https://maas-b.example.com/ws-2/api/v1")
            await b.resume_video(
                "t-wan3",
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    resolution="720p",
                    submitted_base_url="https://maas-a.example.com/ws-1/api/v1",
                ),
            )

        assert client.gets[-1]["url"] == "https://maas-a.example.com/ws-1/api/v1/tasks/t-wan3"


class TestCustomProviderBaseUrlReplay:
    """A custom provider delegating to the dashscope protocol: the protocol flag and the submit domain are stored separately; resume replays with the submit domain.

    自定义供应商委托 dashscope 协议：协议标识与提交域名分列落地，续跑按提交域名回放。"""

    @staticmethod
    def _wrapped(base_url: str):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend
        from lib.custom_provider.backends import CustomVideoBackend

        delegate = DashScopeVideoBackend(api_key="sk", base_url=base_url, model="wan3.0-video")
        return CustomVideoBackend(
            provider_id="custom-7",
            delegate=delegate,
            model="wan3.0-video",
            endpoint="dashscope-async-video",
        )

    async def test_submit_persists_protocol_id_and_domain(self, tmp_path: Path):
        """A submit through the wrapper layer: the endpoint slot stores the protocol flag for comparison, the base_url slot stores the domain for replay.

        穿过包装层的提交：endpoint 位落协议标识供比对，域名落 base_url 位供回放。"""
        post = AsyncMock(return_value=_resp(_submit("job-c1")))
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=post, get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3, captured_provider_job_ids() as persisted:
            backend = self._wrapped("https://custom-a.example.com")
            await backend.generate(
                VideoGenerationRequest(
                    prompt="p", output_path=tmp_path / "o.mp4", resolution="720p", task_id="db-task-c1"
                )
            )

        assert [(r["endpoint"], r["base_url"]) for r in persisted] == [
            ("dashscope-async-video", "https://custom-a.example.com/api/v1")
        ]

    async def test_resume_polls_submitted_domain_after_base_url_change(self, tmp_path: Path):
        """Resume after changing a custom provider's base_url mid-flight: polling hits the domain used at submit, where the job still lives.

        在途改自定义供应商的 base_url 后续跑：轮询打提交时的域名，job 仍在该域名上。"""
        get = AsyncMock(return_value=_resp(_succeeded()))
        client = _client(post=AsyncMock(), get=get)
        p1, p2, p3 = _patches(client, AsyncMock())
        with p1, p2, p3:
            # The config has been changed to B; submit used A. / 配置已被改成 B，提交时用的是 A
            backend = self._wrapped("https://custom-b.example.com")
            await backend.resume_video(
                "job-c1",
                VideoGenerationRequest(
                    prompt="p",
                    output_path=tmp_path / "o.mp4",
                    resolution="720p",
                    submitted_base_url="https://custom-a.example.com/api/v1",
                ),
            )

        assert client.gets[-1]["url"] == "https://custom-a.example.com/api/v1/tasks/job-c1"


class TestPollErrorRedaction:
    async def test_poll_4xx_message_drops_query_credentials(self):
        from lib.backends.video_backends.dashscope import DashScopeVideoBackend

        # The exception message of a polling 4xx lands in task.error_message and logs via str(exc); credentials passed in the query
        # are rendered in the URL that raise_for_status writes into the message, so they must be redacted before raising.
        # 轮询 4xx 的异常消息会经 str(exc) 落进 task.error_message 与日志；按 query 传的
        # 凭证渲染在 raise_for_status 写进消息的那条 URL 里，须先脱敏再抛。
        backend = DashScopeVideoBackend(api_key="k")
        base_url = "https://dashscope.example/api/v1"
        with capture_http() as router:
            router.get(url__regex=rf"^{base_url}/tasks/task-1").mock(
                return_value=httpx.Response(403, json={"message": "forbidden"})
            )
            async with httpx.AsyncClient(params={"api_key": "SECRETKEY"}) as client:
                with pytest.raises(httpx.HTTPStatusError) as excinfo:
                    await backend._poll_once(client, "task-1", base_url)

        assert "SECRETKEY" not in str(excinfo.value)
        assert excinfo.value.response.status_code == 403
