"""Tests for the KlingBackendBase shared scaffolding base class: asserts the two Kling media backends' auth headers, credential
assembly and submit/poll skeleton have a single origin (defined only in the base class, not copied verbatim by subclasses).

KlingBackendBase 共享脚手架基类测试：断言两个 Kling 媒体后端的鉴权头、凭证装配与
submit/poll 骨架出处单一（仅定义于基类，子类不各自逐字复制）。"""

from __future__ import annotations

import inspect

import httpx
import jwt
import pytest

from lib.backends.image_backends.kling import KlingImageBackend
from lib.backends.kling_backend_base import KlingBackendBase
from lib.backends.video_backends.kling import KlingVideoBackend
from tests.http_capture import capture_http

_SECRET = "s" * 40


class TestSharedBaseLineage:
    def test_both_backends_subclass_shared_base(self):
        assert issubclass(KlingImageBackend, KlingBackendBase)
        assert issubclass(KlingVideoBackend, KlingBackendBase)


class TestSingleSourceScaffolding:
    def test_headers_defined_only_on_base(self):
        # _headers has a single origin: defined only in the base class, not overridden by subclasses (no verbatim copies).
        # _headers 出处单一：仅定义于基类，子类不各自覆写（无逐字复制）。
        assert "_headers" in vars(KlingBackendBase)
        assert "_headers" not in vars(KlingImageBackend)
        assert "_headers" not in vars(KlingVideoBackend)

    def test_credential_dispatch_defined_only_on_base(self):
        # __init__'s credential dispatch + base_url normalization live in the base class; each subclass __init__ only does per-medium tail assembly.
        # _jwt / _static_api_key are instance attributes bound in __init__, not in the class __dict__ -- a vars() assertion would always pass and be useless,
        # so verify via source instead: subclass __init__ must delegate to super() and not re-copy the credential dispatch (any of these names means base logic was copied).
        # __init__ 的凭证分派 + base_url 归一化由基类收口；子类各自 __init__ 仅做 per-medium 尾部装配。
        # _jwt / _static_api_key 是 __init__ 绑定的实例属性，不进类 __dict__——vars() 断言会恒真而失效，
        # 改以源码核验：子类 __init__ 必须委托 super() 且不重抄凭证分派（出现这些名即视为复制了基类逻辑）。
        for subclass in (KlingImageBackend, KlingVideoBackend):
            init_src = inspect.getsource(subclass.__init__)
            assert "super().__init__(" in init_src
            assert "_jwt" not in init_src
            assert "_static_api_key" not in init_src
        # name / model are base-class properties (class-level descriptors, really in the class __dict__); subclasses do not override them.
        # name / model 是基类 property（类级描述符，确进类 __dict__）；子类不覆写。
        assert "name" in vars(KlingBackendBase)
        assert "model" in vars(KlingBackendBase)
        assert "name" not in vars(KlingImageBackend)
        assert "name" not in vars(KlingVideoBackend)
        assert "model" not in vars(KlingImageBackend)
        assert "model" not in vars(KlingVideoBackend)

    def test_submit_and_poll_skeleton_on_base(self):
        # The submit/poll retry skeleton is provided by the base class. / submit/poll 的 retry 骨架由基类提供。
        assert "_submit_task" in vars(KlingBackendBase)
        assert "_poll_query" in vars(KlingBackendBase)
        assert "_poll_until_terminal" in vars(KlingBackendBase)


class TestDualModeAuthViaBase:
    def test_jwt_mode_signs_bearer_token(self):
        backend = KlingImageBackend(auth_mode="jwt", access_key="ak-1", secret_key=_SECRET)
        headers = backend._headers()
        token = headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, _SECRET, algorithms=["HS256"], options={"verify_exp": False})
        assert claims["iss"] == "ak-1"

    def test_bearer_mode_uses_static_key(self):
        backend = KlingVideoBackend(auth_mode="bearer", api_key="static-key")
        assert backend._headers()["Authorization"] == "Bearer static-key"

    def test_unknown_auth_mode_raises_from_base(self):
        with pytest.raises(ValueError, match=r"未知 Kling auth_mode"):
            KlingImageBackend(auth_mode="oauth", api_key="k")


class TestPollErrorRedaction:
    async def test_poll_4xx_message_drops_query_credentials(self):
        # The exception message of a polling 4xx lands in task.error_message and logs via str(exc); credentials passed in the query
        # are rendered in the URL that raise_for_status writes into the message, so they must be redacted before raising.
        # 轮询 4xx 的异常消息会经 str(exc) 落进 task.error_message 与日志；按 query 传的
        # 凭证渲染在 raise_for_status 写进消息的那条 URL 里，须先脱敏再抛。
        backend = KlingVideoBackend(auth_mode="bearer", api_key="static-key", base_url="https://kling.example/v1")
        with capture_http() as router:
            router.get(url__regex=r"^https://kling\.example/v1/tasks/job-1").mock(
                return_value=httpx.Response(403, json={"message": "forbidden"})
            )
            async with httpx.AsyncClient(params={"api_key": "SECRETKEY"}) as client:
                with pytest.raises(httpx.HTTPStatusError) as excinfo:
                    await backend._poll_query(client, "tasks/job-1")

        assert "SECRETKEY" not in str(excinfo.value)
        assert excinfo.value.response.status_code == 403
