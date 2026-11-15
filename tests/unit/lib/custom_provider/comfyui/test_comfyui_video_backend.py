"""ComfyUI video channel: upload, submit, poll, artifact storage, resume, stopping the remote, and the eight
failure codes.

ComfyUI 视频通道：上传、提交、轮询、产物入库、续跑、叫停远端与八个失败码。"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from arcreel_market_core.endpoint_definition import validate_definition
from arcreel_market_core.video_backend_contract import (
    ProviderResponseStage,
    ResumeExpiredError,
    VideoAudioMode,
    VideoCapabilities,
    VideoGenerationRequest,
)
from lib.backends.artifact_download_guard import (
    VIDEO_ARTIFACT_MAX_BYTES,
    ArtifactDestinationRejectedError,
    ArtifactTooLargeError,
)
from lib.backends.backend_runtime import VIDEO_POLL_MAX_CONSECUTIVE_FAILURES
from lib.backends.video_frame_slots import gate_video_request, resolve_video_capabilities
from lib.custom_provider.comfyui.comfyui_backend import ComfyuiVideoBackend
from lib.custom_provider.comfyui.failures import ComfyuiError
from lib.custom_provider.comfyui.request_builder import workflow_sha256
from lib.custom_provider.endpoint_resolution import endpoint_spec_from_row
from lib.custom_provider.factory import create_custom_backend
from lib.generation.task_failure import render_failure
from lib.generation.task_failure_encoding import encode_task_failure_message
from tests.factories import comfyui_endpoint_definition, make_display_names, make_translator
from tests.fakes import MP4_BYTES, bounded_poll_clock, captured_provider_job_ids
from tests.http_capture import capture_http, only_request, request_json

BASE_URL = "https://comfy.test"


def _definition(**overrides: Any) -> dict[str, Any]:
    definition = comfyui_endpoint_definition(**overrides)
    assert validate_definition(definition).valid
    return definition


def _backend(definition: dict[str, Any] | None = None, *, api_key: str = "") -> ComfyuiVideoBackend:
    return ComfyuiVideoBackend(
        provider_id="custom-1",
        model="wan-t2v",
        base_url=BASE_URL,
        api_key=api_key,
        definition=definition if definition is not None else _definition(),
    )


def _request(tmp_path: Path, **overrides: Any) -> VideoGenerationRequest:
    values: dict[str, Any] = {
        "prompt": "一只猫走过屋顶",
        "output_path": tmp_path / "out.mp4",
        "aspect_ratio": "9:16",
        "duration_seconds": 5,
        "task_id": "task-7",
    }
    values.update(overrides)
    return VideoGenerationRequest(**values)


def _history(outputs: dict[str, Any], *, completed: bool = True) -> dict[str, Any]:
    return {"status": {"completed": completed, "status_str": "success"}, "outputs": outputs}


def _video_output(filename: str = "final_00001.mp4") -> dict[str, Any]:
    return {"images": [], "gifs": [{"filename": filename, "subfolder": "video", "type": "output"}]}


def _entry(status: Any, outputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """One terminal-state record, with ``status`` put in as-is (``None`` is also a real-world shape).

    一条终态记录，``status`` 原样放进去（``None`` 也是一种现实形状）。"""
    return {"status": status, "outputs": outputs if outputs is not None else {}}


def _messages(*events: Any) -> dict[str, Any]:
    return {"completed": True, "status_str": "error", "messages": list(events)}


def _cancel_here(_request: httpx.Request) -> httpx.Response:
    """Make requests on this route hit a task cancellation.

    ``CancelledError`` inherits from ``BaseException`` and respx's exception-type ``side_effect`` only
    accepts ``Exception``, so it is raised through the callable path.

    让这一条路由的请求撞上一次任务取消。

    ``CancelledError`` 继承 ``BaseException``，respx 的异常型 ``side_effect`` 只收 ``Exception``，
    故经可调用的那一路抛。
    """
    raise asyncio.CancelledError


def _queue(*, running: Sequence[str] = (), pending: Sequence[str] = ()) -> dict[str, Any]:
    """Shape of ``/queue``: entries are ``[index, prompt_id, prompt, nodes to execute, extra data]``.

    ``/queue`` 的形状：条目是 ``[序号, prompt_id, prompt, 待执行节点, 额外数据]``。"""
    return {
        "queue_running": [[index, job, {}, [], {}] for index, job in enumerate(running)],
        "queue_pending": [[index, job, {}, [], {}] for index, job in enumerate(pending)],
    }


def _with_image_bindings() -> dict[str, Any]:
    """Add a first frame and two reference image slots to the minimal definition, bringing the upload step
    in.

    在最小定义上补首帧与两个参考图格子，把上传那一段带进来。"""
    definition = comfyui_endpoint_definition()
    definition["workflow"]["10"] = {"class_type": "LoadImage", "inputs": {"image": "draft.png"}}
    definition["workflow"]["11"] = {"class_type": "LoadImage", "inputs": {"image": "ref-a.png"}}
    definition["workflow"]["12"] = {"class_type": "LoadImage", "inputs": {"image": "ref-b.png"}}
    definition["bindings"]["start_image"] = [{"node": "10", "input": "image", "class_type": "LoadImage"}]
    definition["bindings"]["reference_images"] = [
        {"node": "11", "input": "image", "class_type": "LoadImage"},
        {"node": "12", "input": "image", "class_type": "LoadImage"},
    ]
    assert validate_definition(definition).valid
    return definition


class TestDeclaredCapabilities:
    """The backend's own capability declaration, and which one the pre-generation capability gate reads.

    backend 自己那份能力声明，以及生成前的能力闸门读到的是哪一份。"""

    @staticmethod
    def _gate(definition: dict[str, Any], *, has_image: bool) -> VideoCapabilities:
        """Get capabilities the way production does: factory builds the wrapper -> tier lookup -> gate.

        The wrapper's tier lookup deliberately does not short-circuit to the synthesized result the
        factory injected; it uses the wrapped backend's declaration as the base
        (``CustomVideoBackend.video_capabilities_for_tier``). If the backend declares one bit less, the
        gate blocks the request before it reaches ``generate``.

        照生产那条路取能力：工厂建包装层 → 档位查询 → 闸门。

        包装层的档位查询刻意不短路回工厂注入的合成结果，而是以被包装 backend 的声明为基底
        （``CustomVideoBackend.video_capabilities_for_tier``）。backend 少宣称一位，闸门就会在请求
        到达 ``generate`` 之前把它挡掉。
        """
        spec = endpoint_spec_from_row(cast("Any", SimpleNamespace(id=7, definition=definition)))
        provider = SimpleNamespace(provider_id="custom-1", base_url=BASE_URL, api_key="")
        backend = create_custom_backend(
            provider=cast("Any", provider), model_id="wan-t2v", endpoint="ce-7", endpoint_spec=spec
        )
        caps = resolve_video_capabilities(backend, service_tier="default", resolution=None)
        gate_video_request(
            caps=caps,
            provider="custom-1",
            model="wan-t2v",
            prompt="一只猫走过屋顶",
            has_image=has_image,
            end_image=None,
            reference_images=None,
            reference_audio_files=None,
            reference_audio_total_seconds=None,
        )
        return caps

    def test_a_text_only_workflow_gets_past_the_capability_gate(self):
        caps = self._gate(_definition(), has_image=False)

        assert (caps.text_to_video, caps.first_frame, caps.audio_track) == (True, False, VideoAudioMode.ALWAYS_OFF)

    def test_a_workflow_with_a_start_image_gets_past_it_as_image_to_video(self):
        definition = _definition()
        definition["bindings"]["start_image"] = [{"node": "11", "input": "image", "class_type": "LoadImage"}]

        caps = self._gate(definition, has_image=True)

        assert (caps.text_to_video, caps.first_frame) == (False, True)


class TestGenerate:
    async def test_a_generation_uploads_submits_polls_and_stores_the_artifact(self, tmp_path: Path):
        """One end-to-end run: media backfill -> submit -> poll -> artifact lands at output_path, and
        provenance comes back with the result.

        端到端一条：素材回填 → 提交 → 轮询 → 产物落到 output_path，溯源随结果回来。"""
        definition = _with_image_bindings()
        start = tmp_path / "first.png"
        start.write_bytes(b"png-bytes")
        reference = tmp_path / "ref.jpg"
        reference.write_bytes(b"jpg-bytes")

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids() as persisted:
            upload = router.post(f"{BASE_URL}/upload/image").mock(
                side_effect=[
                    httpx.Response(200, json={"name": "task-7-start_image.png", "subfolder": "arcreel"}),
                    httpx.Response(200, json={"name": "task-7-reference_images-1.jpg", "subfolder": "arcreel"}),
                ]
            )
            submit = router.post(f"{BASE_URL}/prompt").mock(
                return_value=httpx.Response(200, json={"prompt_id": "p-1", "node_errors": {}})
            )
            history = router.get(f"{BASE_URL}/history/p-1").mock(
                side_effect=[
                    httpx.Response(200, json={}),
                    httpx.Response(200, json={"p-1": _history({"9": _video_output()})}),
                ]
            )
            # The empty round also checks that this execution is still in the queue; otherwise a restarted
            # ComfyUI would just make polling spin until timeout.
            # 空态那一轮顺手确认这次执行还在队列上，否则一台重启过的 ComfyUI 只会让轮询空转到超时。
            queue = router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue(pending=["p-1"])))
            view = router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend(definition).generate(
                _request(tmp_path, start_image=start, reference_images=[reference])
            )

        assert result.video_path.read_bytes() == MP4_BYTES
        assert result.task_id == "p-1"
        assert upload.call_count == 2
        assert history.call_count == 2
        assert queue.call_count == 1
        # The reference value takes subfolder / name from the response, not the request; the server renames on
        # name clashes.
        # 引用值取响应里的 subfolder / name，不是请求里的——服务端会为重名改名。
        submitted = request_json(submit.calls.last.request)["prompt"]
        assert submitted["10"]["inputs"]["image"] == "arcreel/task-7-start_image.png"
        assert submitted["11"]["inputs"]["image"] == "arcreel/task-7-reference_images-1.jpg"
        assert request_json(submit.calls.last.request)["client_id"] == "arcreel-task-7"
        assert view.calls.last.request.url.params["filename"] == "final_00001.mp4"
        assert persisted == [
            {
                "task_id": "task-7",
                "job_id": "p-1",
                "provider": "custom-1",
                "endpoint": None,
                "base_url": BASE_URL,
            }
        ]

    async def test_the_version_metadata_gets_the_actual_seed_and_the_workflow_fingerprint(self, tmp_path: Path):
        """Both are only known after one generation: the seed is randomized at submit time, and the
        fingerprint is that of the workflow actually sent.

        两者都只有生成过一次才知道：种子是提交那一刻现随机的，指纹是那份实发 workflow 的。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            submit = router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        sent = request_json(submit.calls.last.request)["prompt"]
        assert result.seed == sent["3"]["inputs"]["seed"]
        assert result.provenance == {"workflow_sha256": workflow_sha256(sent)}

    async def test_an_unbound_duration_is_forwarded_untouched(self, tmp_path: Path):
        """``frames`` unbound means duration is not driven by ArcReel: submit as usual and keep the
        workflow's literal frame count.

        ``frames`` 未绑定即时长不由 ArcReel 驱动：照常提交，帧数保持 workflow 字面值。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            submit = router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path, duration_seconds=5))

        assert "frames" not in _definition()["bindings"]
        assert result.video_path.exists()
        assert request_json(submit.calls.last.request)["prompt"]["9"]["inputs"]["fps"] == 16

    async def test_only_bound_and_supplied_media_is_uploaded(self, tmp_path: Path):
        """Not a single extra reference image is uploaded: the slot count is how many images this workflow
        can take, and uploading more only wastes bandwidth.

        多出来的参考图一张都不传：格子数就是这份 workflow 能收几张，多传只是白占带宽。"""
        definition = _with_image_bindings()
        references = []
        for index in range(4):
            path = tmp_path / f"ref{index}.png"
            path.write_bytes(b"png")
            references.append(path)

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            upload = router.post(f"{BASE_URL}/upload/image").mock(
                return_value=httpx.Response(200, json={"name": "stored.png", "subfolder": "arcreel"})
            )
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            await _backend(definition).generate(_request(tmp_path, reference_images=references))

        # Two slots get two uploads; the first frame is bound but not given this time, so it is not uploaded. / 两个格子传两张；首帧绑定了但这次没给，不传。
        assert upload.call_count == 2

    async def test_a_root_level_history_entry_is_accepted(self, tmp_path: Path):
        """Both shapes must be accepted: different versions and proxies return one each, and accepting only
        the wrapped one would leave the other half of deployments waiting forever for a terminal state.

        两种形状都得认：不同版本与代理各回一种，只认包裹那种会让另一半部署永远等不到终态。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert result.video_path.read_bytes() == MP4_BYTES

    async def test_credentials_render_once_and_ride_every_route(self, tmp_path: Path):
        definition = _with_image_bindings()
        definition["auth"] = {"headers": {"Authorization": "Bearer {{api_key}}"}}
        assert validate_definition(definition).valid
        start = tmp_path / "first.png"
        start.write_bytes(b"png")

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            upload = router.post(f"{BASE_URL}/upload/image").mock(
                return_value=httpx.Response(200, json={"name": "stored.png", "subfolder": "arcreel"})
            )
            submit = router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            history = router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            view = router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            await _backend(definition, api_key="k-1").generate(_request(tmp_path, start_image=start))

        for route in (upload, submit, history, view):
            assert route.calls.last.request.headers["authorization"] == "Bearer k-1"

    async def test_query_credentials_ride_every_route_too(self, tmp_path: Path):
        """Reverse proxies that take credentials in the query must work too: append them to the URL of every
        route, not just the header table.

        按 query 传凭证的反代同样要认：拼进每一条路由的 URL，不只是头那一张表。"""
        definition = _definition(auth={"query": {"token": "{{api_key}}"}})

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            submit = router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            history = router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            view = router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            await _backend(definition, api_key="k-1").generate(_request(tmp_path))

        for route in (submit, history, view):
            assert route.calls.last.request.url.params["token"] == "k-1"
        # The artifact URL's own three parameters are not pushed out by the credentials. / 产物地址自己的三个参数不被凭证挤掉。
        assert view.calls.last.request.url.params["filename"] == "final_00001.mp4"

    @pytest.mark.parametrize("route", ["upload", "submit", "history", "view"])
    async def test_a_cross_origin_redirect_never_takes_the_credential_along(self, tmp_path: Path, route: str):
        """When the reverse proxy 302-redirects a route elsewhere, the credentials must not follow.

        httpx's follow_redirects only strips ``Authorization`` across origins, while the auth section
        allows any header name; letting it follow automatically, one ``/view`` redirect to object storage
        would send ``X-API-Key`` into a third party's access log.

        反代把某条路由 302 到别处时凭证不许跟过去。

        httpx 的 follow_redirects 跨源只摘 ``Authorization``，而 auth 节允许任意头名——交给它
        自动跟随，一次指向对象存储的 ``/view`` 跳转就会把 ``X-API-Key`` 送进第三方的访问日志。
        """
        definition = _with_image_bindings()
        definition["auth"] = {"headers": {"X-API-Key": "{{api_key}}"}}
        assert validate_definition(definition).valid
        start = tmp_path / "first.png"
        start.write_bytes(b"png")
        elsewhere = "https://elsewhere.test"
        redirect = httpx.Response(307, headers={"location": f"{elsewhere}/moved"})

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            moved_post = router.post(f"{elsewhere}/moved")
            moved_get = router.get(f"{elsewhere}/moved")
            upload = router.post(f"{BASE_URL}/upload/image").mock(
                return_value=redirect
                if route == "upload"
                else httpx.Response(200, json={"name": "stored.png", "subfolder": "arcreel"})
            )
            moved_post.mock(
                return_value=httpx.Response(200, json={"name": "stored.png", "subfolder": "arcreel"})
                if route == "upload"
                else httpx.Response(200, json={"prompt_id": "p-1"})
            )
            router.post(f"{BASE_URL}/prompt").mock(
                return_value=redirect if route == "submit" else httpx.Response(200, json={"prompt_id": "p-1"})
            )
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=redirect
                if route == "history"
                else httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(
                return_value=redirect if route == "view" else httpx.Response(200, content=MP4_BYTES)
            )
            moved_get.mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
                if route == "history"
                else httpx.Response(200, content=MP4_BYTES)
            )

            await _backend(definition, api_key="secret").generate(_request(tmp_path, start_image=start))

        followed = moved_post if route in {"upload", "submit"} else moved_get
        assert followed.call_count == 1
        assert "x-api-key" not in followed.calls.last.request.headers
        # The same-origin hop must still carry them, otherwise a deployment behind a reverse proxy could not
        # send anything.
        # 同源那一跳仍要带上，否则套了反代的部署一条都发不出去。
        assert upload.calls.last.request.headers["x-api-key"] == "secret"

    async def test_view_redirect_to_a_link_local_address_is_not_followed(self, tmp_path: Path):
        metadata = "http://169.254.169.254/latest/"
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            view = router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(302, headers={"location": metadata}))
            target = router.get(metadata).mock(return_value=httpx.Response(200, content=MP4_BYTES))

            with pytest.raises(ArtifactDestinationRejectedError):
                await _backend().generate(_request(tmp_path))

        assert view.call_count == 1
        assert target.call_count == 0

    async def test_view_body_over_the_video_limit_is_aborted(self, tmp_path: Path):
        oversized = {"Content-Length": str(VIDEO_ARTIFACT_MAX_BYTES + 1)}
        request = _request(tmp_path)
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            view = router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, headers=oversized, content=b""))

            with pytest.raises(ArtifactTooLargeError):
                await _backend().generate(request)

        assert view.call_count == 1
        assert not request.output_path.exists()

    async def test_a_same_origin_redirect_keeps_the_query_credential(self, tmp_path: Path):
        """``Location`` replaces the whole query string; if the credentials are not added back they are lost
        on a ``/view`` -> ``/view/`` normalization redirect.

        ``Location`` 整串替换查询串，凭证不补回就会在一次 ``/view`` → ``/view/`` 规范化跳转上丢掉。"""
        definition = _definition(auth={"query": {"token": "{{api_key}}"}})

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(
                return_value=httpx.Response(302, headers={"location": f"{BASE_URL}/files/final.mp4"})
            )
            moved = router.get(f"{BASE_URL}/files/final.mp4").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            await _backend(definition, api_key="k-1").generate(_request(tmp_path))

        assert moved.calls.last.request.url.params["token"] == "k-1"

    async def test_an_empty_api_key_leaves_the_auth_section_unrendered(self, tmp_path: Path):
        """ComfyUI has no auth natively: sending an empty ``Bearer `` only makes a reverse proxy reject with
        401 a request it should let through.

        ComfyUI 原生无鉴权：发一个空的 ``Bearer `` 只会让反代以 401 拒掉本该放行的请求。"""
        definition = _definition(auth={"headers": {"Authorization": "Bearer {{api_key}}"}})

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            submit = router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            await _backend(definition, api_key="").generate(_request(tmp_path))

        assert "authorization" not in submit.calls.last.request.headers


class TestFailures:
    async def test_an_upload_failure_stops_before_any_submit(self, tmp_path: Path):
        definition = _with_image_bindings()
        start = tmp_path / "first.png"
        start.write_bytes(b"png")

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/upload/image").mock(return_value=httpx.Response(507, text="disk full"))
            submit = router.post(f"{BASE_URL}/prompt")

            with pytest.raises(ComfyuiError) as caught:
                await _backend(definition).generate(_request(tmp_path, start_image=start))

        assert caught.value.code == "comfyui_upload_failed"
        assert submit.call_count == 0

    async def test_node_errors_on_a_200_fail_the_task_without_polling(self, tmp_path: Path):
        """The graph was submitted but node parameters fail validation: polling would only wait until
        timeout.

        图提交上去了、节点参数却过不了校验：进轮询只会等到超时。"""
        node_errors = {
            "3": {"class_type": "KSampler", "errors": [{"message": "value 4096 out of range"}]},
            "4": {"class_type": "CheckpointLoaderSimple", "errors": [{"message": "model not found"}]},
        }

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids() as persisted:
            router.post(f"{BASE_URL}/prompt").mock(
                return_value=httpx.Response(200, json={"prompt_id": "p-1", "node_errors": node_errors})
            )
            history = router.get(f"{BASE_URL}/history/p-1")

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_node_errors"
        assert caught.value.params == {"nodes": 2, "summary": "KSampler: value 4096 out of range"}
        assert history.call_count == 0
        assert persisted == []

    async def test_a_400_carries_the_same_failure_code(self, tmp_path: Path):
        """400 and 200-with-node_errors share the cause; the summary uses the title the user sees on the
        canvas.

        400 与 200 带 node_errors 同因，摘要取用户在画布上看到的那个标题。"""
        body = {"error": {"message": "Prompt has no outputs"}, "node_errors": {"6": {"class_type": "CLIPTextEncode"}}}

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(400, json=body))

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_node_errors"
        assert caught.value.params == {"nodes": 1, "summary": "正向"}

    async def test_a_400_without_node_errors_falls_back_to_the_error_message(self, tmp_path: Path):
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(
                return_value=httpx.Response(400, json={"error": {"message": "prompt outputs failed validation"}})
            )

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.params == {"nodes": 0, "summary": "prompt outputs failed validation"}

    async def test_a_failed_job_id_persistence_stops_before_polling(self, tmp_path: Path):
        """If job_id is not stored before polling, a process restart loses the already-running task for
        good.

        job_id 没落库就进轮询，进程一重启这笔已在跑的任务就再也找不回来。"""

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids(fail=RuntimeError("db is down")):
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            history = router.get(f"{BASE_URL}/history/p-1")

            with pytest.raises(RuntimeError, match="db is down"):
                await _backend().generate(_request(tmp_path))

        assert history.call_count == 0

    async def test_an_output_node_without_artifacts_is_refused(self, tmp_path: Path):
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json=_history({"9": {}})))

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_output_missing"
        assert caught.value.params == {"nodes": "9"}

    async def test_temp_artifacts_do_not_count_as_output(self, tmp_path: Path):
        """``temp`` is an intermediate preview that the server may clear at any time; only ``type ==
        "output"`` counts.

        ``temp`` 是中间预览、服务端随时会清掉它；只认 ``type == "output"``。"""
        outputs = {"9": {"gifs": [{"filename": "preview.mp4", "subfolder": "", "type": "temp"}]}}

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json=_history(outputs)))

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_output_missing"

    async def test_artifacts_of_an_unbound_node_are_not_taken(self, tmp_path: Path):
        """A side-branch ``PreviewImage`` also writes artifacts into history; scanning the whole graph would
        take the preview as the final cut.

        旁支的 ``PreviewImage`` 同样会往 history 写产物，扫全图会把预览图当成成片取走。"""
        outputs = {"99": _video_output("preview.mp4")}

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json=_history(outputs)))

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_output_missing"

    async def test_a_still_image_from_a_video_endpoint_is_refused(self, tmp_path: Path):
        outputs = {"9": {"images": [{"filename": "final_00001.png", "subfolder": "", "type": "output"}]}}

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json=_history(outputs)))

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_output_type_mismatch"
        assert caught.value.params == {"filename": "final_00001.png", "media_type": "video"}

    @pytest.mark.parametrize("filename", ["final_00001.mov", "final_00001.m4v", "FINAL.MP4"])
    async def test_the_iso_bmff_family_is_accepted(self, tmp_path: Path, filename: str):
        """The three allowed extensions all belong to ISO BMFF, the same rule as the three accepted for
        reference video upload.

        三个允许的扩展名同属 ISO BMFF，与参考视频上传收的那三个同一份口径。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output(filename)}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert result.video_path.read_bytes() == MP4_BYTES

    async def test_a_webm_artifact_is_refused_with_the_allowed_suffixes_in_the_copy(self, tmp_path: Path):
        """With ``.webm`` bytes put under the canonical ``.mp4`` path, both the extension and the served
        MIME would lie, so it is refused as a type mismatch.

        ``.webm`` 的字节装进规范 ``.mp4`` 路径后扩展名与下发 MIME 都在说谎，故按类型不符拒收。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output("final_00001.webm")}))
            )
            view = router.get(f"{BASE_URL}/view")

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_output_type_mismatch"
        assert view.call_count == 0
        rendered = render_failure(
            encode_task_failure_message(caught.value), make_translator("zh"), make_display_names("zh")
        )
        assert ".m4v / .mov / .mp4" in rendered
        assert "h264-mp4" in rendered

    async def test_webm_bytes_under_an_mp4_name_are_refused_and_nothing_is_left_on_disk(self, tmp_path: Path):
        """The extension is just a string in history: a save node can write another container under an
        ``.mp4`` name.

        扩展名只是 history 里的一个字符串：保存节点可以把别的容器写进 ``.mp4`` 的名字。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=b"\x1a\x45\xdf\xa3webm"))

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_output_container_mismatch"
        assert caught.value.params == {"filename": "final_00001.mp4", "media_type": "video"}
        assert not (tmp_path / "out.mp4").exists()
        rendered = render_failure(
            encode_task_failure_message(caught.value), make_translator("zh"), make_display_names("zh")
        )
        assert ".m4v / .mov / .mp4" in rendered

    async def test_still_image_iso_bmff_bytes_under_an_mp4_name_are_refused(self, tmp_path: Path):
        """``ftyp`` only says this is the ISO BMFF family: HEIC / AVIF use the same layer, and letting them
        through would leave a final cut that cannot play.

        ``ftyp`` 只说这是 ISO BMFF 家族：HEIC / AVIF 同用这一层，放行它们会留下一份放不出的成片。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(
                return_value=httpx.Response(200, content=b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heic-bytes")
            )

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_output_container_mismatch"
        assert not (tmp_path / "out.mp4").exists()

    @pytest.mark.parametrize(
        ("code", "params"),
        [
            ("comfyui_upload_failed", {"detail": "disk full"}),
            ("comfyui_node_errors", {"nodes": 2, "summary": "KSampler: out of range"}),
            ("comfyui_job_lost", {"prompt_id": "p-1"}),
            ("comfyui_execution_error", {"node": "KSampler", "detail": "OutOfMemoryError"}),
            ("comfyui_interrupted", {}),
            ("comfyui_output_missing", {"nodes": "9"}),
            ("comfyui_output_type_mismatch", {"filename": "a.png", "media_type": "video"}),
            ("comfyui_output_container_mismatch", {"filename": "a.mp4", "media_type": "video"}),
            ("comfyui_image_drop_unsupported", {"node": "10"}),
        ],
    )
    @pytest.mark.parametrize("locale", ["zh", "en", "vi"])
    def test_every_failure_code_renders_in_every_locale(self, code: str, params: dict[str, Any], locale: str):
        """Only the machine code is stored; the read side renders it by Accept-Language, and if one of the
        three languages is missing some users see the bare code.

        The encode step also pins that the worker recognizes this exception: exceptions that
        ``encode_task_failure_message`` does not recognize degrade to bare text, which the read side can
        never translate.

        No ``{`` remains in the rendered result: ``lib.i18n`` falls back to the unfilled template when
        formatting raises, so a template with mismatched placeholders would show a string of ``{name}``
        to the user.

        落库只存机器码，读侧按 Accept-Language 渲染；三语缺一就有用户看到裸码。

        编码这一步同时钉住 worker 认得这个异常：``encode_task_failure_message`` 认不出的异常
        会降级成一段裸文本，读侧就再也翻译不了。

        渲染结果里不留 ``{``：``lib.i18n`` 在格式化抛错时退回未填值的模板，占位符对不上的模板
        会带着一串 ``{name}`` 直接显示给用户。
        """
        message = encode_task_failure_message(ComfyuiError(code, **params))

        rendered = render_failure(message, make_translator(locale), make_display_names(locale))

        assert rendered
        assert code not in rendered
        assert "{" not in rendered

    @pytest.mark.parametrize("locale", ["zh", "en", "vi"])
    def test_the_mismatch_text_lists_the_allowed_extensions(self, locale: str):
        """The extension list is computed on the read side from the stored ``media_type``; the stored
        parameters are only the file name and media type.

        扩展名清单在读侧按落库的 ``media_type`` 现算，落库参数只有文件名与媒体类型。"""
        message = encode_task_failure_message(
            ComfyuiError("comfyui_output_type_mismatch", filename="a.png", media_type="video")
        )

        rendered = render_failure(message, make_translator(locale), make_display_names(locale))

        assert rendered
        assert ".m4v / .mov / .mp4" in rendered


class TestMultipleArtifacts:
    async def test_the_first_artifact_is_taken_and_the_rest_are_reported(self, tmp_path: Path, caplog):
        outputs = {
            "9": {
                "gifs": [
                    {"filename": "final_00001.mp4", "subfolder": "video", "type": "output"},
                    {"filename": "final_00002.mp4", "subfolder": "video", "type": "output"},
                ]
            }
        }

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json=_history(outputs)))
            view = router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            with caplog.at_level("WARNING"):
                result = await _backend().generate(_request(tmp_path))

        assert only_request(view).url.params["filename"] == "final_00001.mp4"
        assert "共 2 个" in caplog.text
        # The log is only seen by ops; this one has to reach the task result so the user knows they got one of
        # several.
        # 日志只有运维看得到；这一条要一路走到任务结果上，用户才知道自己拿到的是其中一个。
        assert result.warnings == (
            {"key": "comfyui_multiple_outputs", "params": {"count": 2, "filename": "final_00001.mp4"}},
        )

    async def test_a_thumbnail_beside_the_video_does_not_pass_for_the_result(self, tmp_path: Path):
        """When the same bound node writes both a thumbnail and the final cut, the final cut is taken.

        The order of ``_output_artifacts`` is the order of the three keys ``images`` / ``gifs`` /
        ``audio`` themselves and has nothing to do with "which one is the final cut"; taking the first by
        that order would report a successful render as an artifact type mismatch.

        同一个绑定节点既写缩略图又写成片时，取的是成片。

        ``_output_artifacts`` 的次序是 ``images`` / ``gifs`` / ``audio`` 这三个键自己的次序，与
        「哪个是成片」无关；照次序取第一个会把一次成功的出片报成产物类型不符。
        """
        outputs = {
            "9": {
                "images": [{"filename": "preview_00001.png", "subfolder": "", "type": "output"}],
                "gifs": [{"filename": "final_00001.mp4", "subfolder": "video", "type": "output"}],
            }
        }

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json=_history(outputs)))
            view = router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert only_request(view).url.params["filename"] == "final_00001.mp4"
        assert result.warnings == (
            {"key": "comfyui_multiple_outputs", "params": {"count": 2, "filename": "final_00001.mp4"}},
        )

    async def test_a_single_artifact_reports_nothing(self, tmp_path: Path):
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert result.warnings == ()

    @pytest.mark.parametrize("locale", ["zh", "en", "vi"])
    def test_the_warning_renders_in_every_locale(self, locale: str):
        """Warnings in the task result are rendered in the current language like failure reasons; if one of
        the three languages is missing some users see the bare key.

        任务结果里的 warning 与失败原因同样按当前语言渲染，三语缺一就有用户看到裸 key。"""
        rendered = make_translator(locale)("comfyui_multiple_outputs", count=2, filename="final_00001.mp4")

        assert rendered
        assert "comfyui_multiple_outputs" not in rendered


class TestDiagnostics:
    async def test_the_recorded_history_keeps_only_the_status_and_the_output_node(self, tmp_path: Path):
        """The full history carries every node's output, and one trace entry could blow the diagnostics
        column up to hundreds of KB.

        整份 history 带着每个节点的全部产出，一条留痕就能把诊断列撑到几百 KB。"""
        recorded: list[tuple[ProviderResponseStage, object]] = []

        async def _record(stage: ProviderResponseStage, body: object) -> None:
            recorded.append((stage, body))

        outputs = {"9": _video_output(), "99": {"images": [{"filename": "noise.png", "type": "temp"}]}}

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json=_history(outputs)))
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            await _backend().generate(_request(tmp_path, on_provider_response=_record))

        polled = [body for stage, body in recorded if stage == "poll"]
        assert polled == [{"status": {"completed": True, "status_str": "success"}, "outputs": {"9": _video_output()}}]
        assert [stage for stage, _ in recorded] == ["submit", "poll", "result"]


class TestTerminalStates:
    """Whether a terminal record means success or failure is decided by the last event in
    ``status.messages``.

    一条终态记录说的是成功还是失败，判据是 ``status.messages`` 的末尾事件。"""

    async def test_a_node_exception_is_reported_with_its_node_and_summary(self, tmp_path: Path):
        event = [
            "execution_error",
            {
                "prompt_id": "p-1",
                "node_id": "3",
                "node_type": "KSampler",
                "exception_message": "CUDA out of memory",
                "exception_type": "torch.OutOfMemoryError",
                "traceback": ["line one", "line two"],
            },
        ]

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json=_entry(_messages(event))))

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_execution_error"
        # The traceback has hundreds of lines while the failure reason is stored whole, so the summary takes
        # only the type and the message.
        # traceback 有上百行而失败原因整条落库，摘要只取类型与消息两项。
        assert caught.value.params == {"node": "KSampler", "detail": "torch.OutOfMemoryError: CUDA out of memory"}

    async def test_an_interrupted_execution_has_its_own_code(self, tmp_path: Path):
        """Someone pressed cancel in ComfyUI: this run did not finish, which by itself says nothing wrong
        about the workflow.

        有人在 ComfyUI 上按了取消：这一次没跑完，本身不说明这份 workflow 有问题。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_entry(_messages(["execution_interrupted", {"node_id": "9"}])))
            )

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_interrupted"
        assert caught.value.params == {}

    async def test_only_the_last_event_decides(self, tmp_path: Path):
        """Nodes after the failing node running to completion is normal; judging by "an error occurred"
        would call a final cut a failure.

        报错的节点后面还有节点照跑完是常态；按「出现过 error」判会把成片说成失败。"""
        events = (
            ["execution_error", {"node_type": "UpscaleImage", "exception_message": "skipped"}],
            ["execution_success", {"prompt_id": "p-1"}],
        )

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_entry(_messages(*events), {"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert result.video_path.read_bytes() == MP4_BYTES

    async def test_a_null_status_without_outputs_falls_back_to_execution_error(self, tmp_path: Path):
        """The cell that cannot be judged falls back to execution failure: calling it "the artifact node
        produced nothing" would blame the binding for an environment problem.

        无从判起的那一格按执行失败兜底：说成「产物节点没出东西」会把环境问题栽给绑定。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json=_entry(None)))

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_execution_error"
        assert caught.value.params["node"] == "-"

    async def test_a_null_status_with_outputs_is_still_a_success(self, tmp_path: Path):
        """Some versions and proxies do not return status; if there is output, treat it as finished,
        otherwise those deployments could never produce anything.

        部分版本与代理不回 status；有产出就当它跑完了，否则这些部署一次片都出不了。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_entry(None, {"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert result.video_path.read_bytes() == MP4_BYTES

    async def test_a_finished_run_without_artifacts_stays_output_missing(self, tmp_path: Path):
        """The last event says it succeeded, but the bound node produced no file: only this cell is a
        binding problem.

        末尾事件说跑成功了、绑定的节点却没出文件——这一格才是绑定的问题。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_entry(_messages(["execution_success", {}]), {"9": {}}))
            )

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_output_missing"


class TestJobLost:
    """While history stays empty, is this execution still on this machine at all?

    history 一直空着的时候，这次执行到底还在不在这台机器上。"""

    async def test_an_id_in_neither_queue_is_reported_lost(self, tmp_path: Path):
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            history = router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json={}))
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue(running=["other"])))

            with pytest.raises(ComfyuiError) as caught:
                await _backend().generate(_request(tmp_path))

        assert caught.value.code == "comfyui_job_lost"
        assert caught.value.params == {"prompt_id": "p-1"}
        # Declared dead in the first round: after confirming the queue does not have it, check history once
        # more, and stop spinning after two checks.
        # 第一轮就判死：确认过队列没有它之后再查一次 history，两次之后不再空转。
        assert history.call_count == 2

    @pytest.mark.parametrize("lane", ["running", "pending"])
    async def test_an_id_still_on_the_queue_keeps_polling(self, tmp_path: Path, lane: str):
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                side_effect=[
                    httpx.Response(200, json={}),
                    httpx.Response(200, json=_history({"9": _video_output()})),
                ]
            )
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue(**{lane: ["p-1"]})))
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert result.video_path.read_bytes() == MP4_BYTES

    async def test_a_run_that_finished_between_the_two_requests_is_not_lost(self, tmp_path: Path):
        """Queue and history are two independent requests: an execution that finishes right between them is
        in neither.

        队列与 history 是两次独立请求：执行恰好在两次之间走完时它两边都不在。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                side_effect=[
                    httpx.Response(200, json={}),
                    httpx.Response(200, json=_history({"9": _video_output()})),
                ]
            )
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue()))
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert result.video_path.read_bytes() == MP4_BYTES

    async def test_a_broken_queue_route_never_fails_a_healthy_run(self, tmp_path: Path):
        """A reverse proxy that blocks only ``/queue``: the task's own URL is fine, so do not let a helper
        criterion declare a running render dead.

        只挡掉 ``/queue`` 的反向代理：任务本身的地址好着，别拿辅助判据把出片中的执行判死。"""
        rounds = itertools.count()

        def _history_route(_request: httpx.Request) -> httpx.Response:
            # A real long generation stays empty until the artifact is ready, so this helper criterion has to
            # run every round.
            # 一次真实的长生成在产物就绪之前会一直空态，故这条辅助判据每一轮都要走一遍。
            if next(rounds) < VIDEO_POLL_MAX_CONSECUTIVE_FAILURES:
                return httpx.Response(200, json={})
            return httpx.Response(200, json=_history({"9": _video_output()}))

        with capture_http() as router, bounded_poll_clock(step=1.0), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(side_effect=_history_route)
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(503, text="bad gateway"))
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert result.video_path.read_bytes() == MP4_BYTES

    async def test_an_unreadable_queue_never_declares_a_loss(self, tmp_path: Path):
        """An HTML page returned while the proxy restarts: being unable to read this table does not mean the
        queue is empty.

        代理重启期回一页 HTML：读不出这张表不等于队列是空的。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                side_effect=[
                    httpx.Response(200, json={}),
                    httpx.Response(200, json=_history({"9": _video_output()})),
                ]
            )
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, html="<html>502</html>"))
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert result.video_path.read_bytes() == MP4_BYTES

    async def test_a_json_body_without_the_two_lists_is_unreadable_too(self, tmp_path: Path):
        """``{"error": "restarting"}`` returned while the proxy restarts: parsing as JSON does not mean the
        queue was read.

        代理重启期回 ``{"error": "restarting"}``：解得出 JSON 不代表读得到队列。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(
                side_effect=[
                    httpx.Response(200, json={}),
                    httpx.Response(200, json={}),
                    httpx.Response(200, json=_history({"9": _video_output()})),
                ]
            )
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json={"error": "restarting"}))
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().generate(_request(tmp_path))

        assert result.video_path.read_bytes() == MP4_BYTES


class TestResume:
    """``provider_job_id`` is the ``prompt_id``: resuming continues the same execution, with no re-upload
    and no re-submit.

    ``provider_job_id`` 就是 ``prompt_id``：接续的是同一次执行，不重传也不重提交。"""

    async def test_a_resume_goes_straight_to_polling(self, tmp_path: Path):
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids() as persisted:
            upload = router.post(f"{BASE_URL}/upload/image")
            submit = router.post(f"{BASE_URL}/prompt")
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend(_with_image_bindings()).resume_video("p-1", _request(tmp_path))

        assert result.video_path.read_bytes() == MP4_BYTES
        assert result.task_id == "p-1"
        assert upload.call_count == 0
        assert submit.call_count == 0
        # The submit happened in the previous process; there is no new job_id to store this time. / 提交发生在上一个进程里，这一次没有新的 job_id 要落库。
        assert persisted == []

    async def test_a_resume_polls_the_host_the_job_was_submitted_to(self, tmp_path: Path):
        """The provider's base_url can be changed after submit, while this job lives on the original
        ComfyUI.

        Asking the current domain would ask another machine, which answers "no such prompt_id"; an
        execution still rendering would be judged lost while the user's GPU is still spinning for it.

        供应商的 base_url 可以在提交之后被改，而这一笔活在原来那台 ComfyUI 上。

        照当前域名去问，问的是另一台机器，它答「没有这个 prompt_id」——一次仍在出片的执行会被
        判成丢失，用户的显卡还在为它转。
        """
        submitted = "https://old-comfy.test"
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            current = router.get(f"{BASE_URL}/history/p-1")
            router.get(f"{submitted}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            view = router.get(f"{submitted}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().resume_video("p-1", _request(tmp_path, submitted_base_url=submitted))

        assert result.video_path.read_bytes() == MP4_BYTES
        assert current.call_count == 0
        assert view.call_count == 1

    async def test_a_resume_carries_no_seed_or_fingerprint(self, tmp_path: Path):
        """Both only exist in the build at submit time; this path does not build, so both are absent rather
        than each getting a fake value.

        两者只在提交那一次的构造里存在；这条路不构造，故一起缺席而不是各给一个假值。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )
            router.get(f"{BASE_URL}/view").mock(return_value=httpx.Response(200, content=MP4_BYTES))

            result = await _backend().resume_video("p-1", _request(tmp_path))

        assert result.seed is None
        assert result.provenance is None

    async def test_a_resume_reads_the_output_binding_as_it_stands_now(self, tmp_path: Path):
        """The user changed the binding before resuming: take the artifact by the new binding, and if none
        is found it is output_missing.

        用户在续跑之前改过绑定：按新绑定取产物，取不到即 output_missing。"""
        definition = comfyui_endpoint_definition()
        definition["bindings"]["output"] = [{"node": "9", "class_type": "SaveVideo"}]
        assert validate_definition(definition).valid
        moved = comfyui_endpoint_definition()
        moved["workflow"]["77"] = {"class_type": "SaveVideo", "inputs": {"video": ["8", 0]}}
        moved["bindings"]["output"] = [{"node": "77", "class_type": "SaveVideo"}]
        assert validate_definition(moved).valid

        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.get(f"{BASE_URL}/history/p-1").mock(
                return_value=httpx.Response(200, json=_history({"9": _video_output()}))
            )

            with pytest.raises(ComfyuiError) as caught:
                await _backend(moved).resume_video("p-1", _request(tmp_path))

        assert caught.value.code == "comfyui_output_missing"
        assert caught.value.params == {"nodes": "77"}

    async def test_a_lost_job_on_resume_becomes_resume_expired(self, tmp_path: Path):
        """A loss during resume becomes resume_expired: the worker uses it to mark failure and settle the
        pending call row.

        续跑期的丢失归 resume_expired：worker 据此标失败并结算那条 pending 的调用行。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json={}))
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue()))

            with pytest.raises(ResumeExpiredError) as caught:
                await _backend().resume_video("p-1", _request(tmp_path))

        assert caught.value.job_id == "p-1"


class TestStoppingTheRemote:
    """When this local side is cancelled or times out, do not leave an unwanted execution on the user's GPU.

    本地这一侧被取消或等超时的时候，别把一个没人要的执行扔在用户的显卡上。"""

    @staticmethod
    def _polling_cancelled(router: Any) -> None:
        router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
        router.get(f"{BASE_URL}/history/p-1").mock(side_effect=_cancel_here)

    @staticmethod
    def _version(router: Any, version: str | None) -> None:
        body = {"system": {"comfyui_version": version}} if version is not None else {"system": {}}
        router.get(f"{BASE_URL}/system_stats").mock(return_value=httpx.Response(200, json=body))

    async def test_a_new_enough_server_gets_one_cancel_call(self, tmp_path: Path):
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            self._polling_cancelled(router)
            self._version(router, "0.26.0")
            cancel = router.post(f"{BASE_URL}/api/jobs/p-1/cancel").mock(return_value=httpx.Response(200, json={}))
            queue = router.get(f"{BASE_URL}/queue")

            with pytest.raises(asyncio.CancelledError):
                await _backend().generate(_request(tmp_path))

        assert cancel.call_count == 1
        # One action covers both queued and running, so there is no need to check the queue again. / 一个动作同时覆盖排队中与执行中，不必再查队列。
        assert queue.call_count == 0

    async def test_an_older_server_deletes_a_pending_entry(self, tmp_path: Path):
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            self._polling_cancelled(router)
            self._version(router, "0.25.14")
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue(pending=["p-1"])))
            drop = router.post(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json={}))
            interrupt = router.post(f"{BASE_URL}/interrupt")

            with pytest.raises(asyncio.CancelledError):
                await _backend().generate(_request(tmp_path))

        assert request_json(only_request(drop)) == {"delete": ["p-1"]}
        assert interrupt.call_count == 0

    async def test_an_older_server_interrupts_a_running_entry(self, tmp_path: Path):
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            self._polling_cancelled(router)
            self._version(router, "0.25.14")
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue(running=["p-1"])))
            drop = router.post(f"{BASE_URL}/queue")
            interrupt = router.post(f"{BASE_URL}/interrupt").mock(return_value=httpx.Response(200, json={}))

            with pytest.raises(asyncio.CancelledError):
                await _backend().generate(_request(tmp_path))

        assert interrupt.call_count == 1
        assert drop.call_count == 0

    async def test_someone_elses_run_is_never_interrupted(self, tmp_path: Path):
        """``/interrupt`` interrupts "whichever one is running now" and does not take an id.

        ``/interrupt`` 打断的是「当前正在执行的那一个」、不认 id。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            self._polling_cancelled(router)
            self._version(router, "0.25.14")
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue(running=["other"])))
            drop = router.post(f"{BASE_URL}/queue")
            interrupt = router.post(f"{BASE_URL}/interrupt")

            with pytest.raises(asyncio.CancelledError):
                await _backend().generate(_request(tmp_path))

        assert interrupt.call_count == 0
        assert drop.call_count == 0

    async def test_an_unreadable_version_takes_the_older_path(self, tmp_path: Path):
        """Old versions and some proxies do not return this field; taking the new path because of it would
        hit a 404 and stop nothing.

        老版本与部分代理不回这一字段；据此走新路会打在一个 404 上、什么都没停掉。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            self._polling_cancelled(router)
            router.get(f"{BASE_URL}/system_stats").mock(return_value=httpx.Response(500, text="boom"))
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue(pending=["p-1"])))
            drop = router.post(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json={}))

            with pytest.raises(asyncio.CancelledError):
                await _backend().generate(_request(tmp_path))

        assert drop.call_count == 1

    async def test_a_failed_stop_leaves_the_local_outcome_alone(self, tmp_path: Path):
        """Stopping is best-effort: if the remote refuses, only log it; what is raised is still the
        cancellation itself.

        叫停是 best-effort：远端拒了只记日志，抛出去的仍是取消本身。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            self._polling_cancelled(router)
            self._version(router, "0.26.1")
            cancel = router.post(f"{BASE_URL}/api/jobs/p-1/cancel").mock(return_value=httpx.Response(500, text="boom"))

            with pytest.raises(asyncio.CancelledError):
                await _backend().generate(_request(tmp_path))

        assert cancel.call_count == 1

    async def test_the_remote_is_stopped_before_a_timeout_surfaces(self, tmp_path: Path):
        """The global timeout stops it too: an unwanted half-finished execution still holds the GPU.

        全局超时同样叫停：跑到一半没人要的执行照样占着卡。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": "p-1"}))
            router.get(f"{BASE_URL}/history/p-1").mock(return_value=httpx.Response(200, json={}))
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue(running=["p-1"])))
            self._version(router, "0.26.0")
            cancel = router.post(f"{BASE_URL}/api/jobs/p-1/cancel").mock(return_value=httpx.Response(200, json={}))

            with pytest.raises(TimeoutError):
                await _backend().generate(_request(tmp_path, poll_timeout_seconds=60))

        assert cancel.call_count == 1

    async def test_nothing_is_stopped_when_the_job_was_never_submitted(self, tmp_path: Path):
        """Before submit there is no prompt_id to stop, and nothing is running.

        提交之前没有 prompt_id 可停，也没有任何执行在跑。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            router.post(f"{BASE_URL}/prompt").mock(side_effect=_cancel_here)
            stats = router.get(f"{BASE_URL}/system_stats")

            with pytest.raises(asyncio.CancelledError):
                await _backend().generate(_request(tmp_path))

        assert stats.call_count == 0

    async def test_a_missing_version_field_takes_the_older_path(self, tmp_path: Path):
        """``/system_stats`` is reachable but returns no version number: handled the same as unreadable.

        ``/system_stats`` 可达但不回版本号：与读不到同一处置。"""
        with capture_http() as router, bounded_poll_clock(), captured_provider_job_ids():
            self._polling_cancelled(router)
            self._version(router, None)
            router.get(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json=_queue(pending=["p-1"])))
            drop = router.post(f"{BASE_URL}/queue").mock(return_value=httpx.Response(200, json={}))

            with pytest.raises(asyncio.CancelledError):
                await _backend().generate(_request(tmp_path))

        assert drop.call_count == 1
