"""lib.backends.vidu_shared unit tests -- focused on credential resolution and the env-var fallback semantics of the connection test.

lib.backends.vidu_shared 单元测试 — 重点校验凭证解析与连接测试的环境变量回退语义。"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import httpx
import pytest
import respx

from lib.backends import vidu_shared
from tests.http_capture import capture_http, only_request


class TestResolveViduApiKey:
    def test_explicit_key_wins(self, monkeypatch: pytest.MonkeyPatch):
        # spec section 5.4: even if VIDU_API_KEY is in the env, there is no fallback; the explicit argument always wins and is the only source.
        # spec §5.4：即使 env 里有 VIDU_API_KEY，也不会被 fallback；显式参数永远优先且唯一来源。
        monkeypatch.setenv("VIDU_API_KEY", "from-env")
        assert vidu_shared.resolve_vidu_api_key("explicit") == "explicit"

    def test_env_no_longer_falls_back(self, monkeypatch: pytest.MonkeyPatch):
        """spec section 5.4: env fallback removed -- even with VIDU_API_KEY in the environment, a missing argument still raises.

        spec §5.4：删除 env fallback——即使 VIDU_API_KEY 在环境中，缺失参数仍 raise。"""
        monkeypatch.setenv("VIDU_API_KEY", "from-env")
        with pytest.raises(ValueError, match="Vidu API Key"):
            vidu_shared.resolve_vidu_api_key(None)

    def test_missing_key_without_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("VIDU_API_KEY", raising=False)
        with pytest.raises(ValueError, match="Vidu API Key"):
            vidu_shared.resolve_vidu_api_key(None)


class TestViduConnectionTestKeyResolution:
    """When the connection-test config lacks api_key it must raise directly at the resolve stage (no HTTP request should be made).

    连接测试 config 缺失 api_key 时必须在 resolve 阶段直接 raise（不应发起 HTTP 请求）。"""

    def test_missing_config_key_raises_before_http(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("VIDU_API_KEY", "from-env")

        # No routes declared: actually sending a request would fail under respx; the credential resolution failure must happen before any outbound call.
        # 未声明任何路由：真发出请求会被 respx 判红，凭证解析失败必须先于出站发生
        with capture_http() as router:
            with pytest.raises(ValueError, match="Vidu API Key"):
                vidu_shared.test_vidu_connection({})

            assert router.calls.call_count == 0


@contextmanager
def _probe_route(*, status_code: int, body: str = "") -> Generator[respx.Route]:
    """Outbound flow of the connection-test probe: intercepted by respx at the transport layer.

    The allowlist decision takes the real response status code as input; URL joining and the Authorization header are within the assertions.

    连接测试探针的出站流：走 respx 在 transport 层拦截。

    白名单判定的输入是真实响应的状态码，URL 拼接与 Authorization 头都在断言范围内。
    """
    with capture_http() as router:
        yield router.get(url__regex=r".*/tasks/0/creations").mock(return_value=httpx.Response(status_code, text=body))


class TestViduConnectionTestUrl:
    """Verify the connection test uses a numeric task id (the Vidu server parses the id as int; a non-number gives 400 CODEC).

    验证连接测试用数字 task id（Vidu 服务端把 id 当 int 解析，非数字会 400 CODEC）。"""

    def test_url_uses_numeric_bogus_id(self):
        with _probe_route(status_code=404) as route:
            vidu_shared.test_vidu_connection({"api_key": "vda_test"})

        request = only_request(route)
        assert request.url.path.endswith("/tasks/0/creations")
        assert request.headers["authorization"] == "Token vda_test"

    def test_404_is_success(self):
        """404 = task does not exist but auth passed, allowlisted: no error raised, and the probe request was really sent.

        404 = task 不存在但认证通过，白名单放行：不抛错，且探针请求确实发出去了。"""
        with _probe_route(status_code=404) as route:
            assert vidu_shared.test_vidu_connection({"api_key": "vda_test"}) is None

        assert route.call_count == 1

    def test_401_is_invalid_credential(self):
        with _probe_route(status_code=401), pytest.raises(RuntimeError, match="凭证无效"):
            vidu_shared.test_vidu_connection({"api_key": "vda_test"})

    def test_400_is_undecidable(self):
        with _probe_route(status_code=400, body="CODEC parse error"), pytest.raises(RuntimeError, match="无法判定"):
            vidu_shared.test_vidu_connection({"api_key": "vda_test"})


class TestFetchViduTaskErrorRedaction:
    async def test_poll_4xx_message_drops_query_credentials(self):
        # The exception message of a polling 4xx lands in task.error_message and logs via str(exc); credentials passed in the query
        # are rendered in the URL that raise_for_status writes into the message, so they must be redacted before raising.
        # 轮询 4xx 的异常消息会经 str(exc) 落进 task.error_message 与日志；按 query 传的
        # 凭证渲染在 raise_for_status 写进消息的那条 URL 里，须先脱敏再抛。
        with capture_http() as router:
            router.get(url__regex=r"^https://api\.vidu\.cn/ent/v2/tasks/t-1/creations").mock(
                return_value=httpx.Response(401, json={"detail": "invalid token"})
            )
            async with httpx.AsyncClient(base_url=vidu_shared.VIDU_BASE_URL, params={"api_key": "SECRETKEY"}) as client:
                with pytest.raises(httpx.HTTPStatusError) as excinfo:
                    await vidu_shared.fetch_vidu_task(client, "t-1")

        assert "SECRETKEY" not in str(excinfo.value)
        assert excinfo.value.response.status_code == 401
