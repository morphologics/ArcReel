"""Unit tests for the pure functions of lib.backends.agnes_shared (no real HTTP).

lib.backends.agnes_shared 纯函数单元测试（不打真实 HTTP）。"""

from __future__ import annotations

import pytest

from lib.backends.agnes_shared import (
    AGNES_BASE_URL,
    agnes_base_url,
    agnes_headers,
    agnes_host,
    resolve_agnes_api_key,
)


class TestBaseUrlDerivation:
    def test_default_base(self):
        assert agnes_base_url(None) == AGNES_BASE_URL
        assert AGNES_BASE_URL == "https://apihub.agnes-ai.com/v1"

    def test_host_only_gets_v1_suffix(self):
        # The user enters only the host; /v1 is added on derivation. / 用户只填 host，派生时补 /v1
        assert agnes_base_url("https://apihub.agnes-ai.com") == "https://apihub.agnes-ai.com/v1"

    def test_full_v1_base_is_idempotent(self):
        assert agnes_base_url("https://apihub.agnes-ai.com/v1") == "https://apihub.agnes-ai.com/v1"

    def test_trailing_slash_stripped(self):
        assert agnes_base_url("https://apihub.agnes-ai.com/v1/") == "https://apihub.agnes-ai.com/v1"
        assert agnes_base_url("https://apihub.agnes-ai.com/") == "https://apihub.agnes-ai.com/v1"

    def test_custom_relay_host(self):
        assert agnes_base_url("https://relay.example.com") == "https://relay.example.com/v1"

    def test_whitespace_falls_back_to_default(self):
        # A whitespace-only base_url is truthy and would bypass `or`; it must be stripped and fall back to the default host,
        # not be stripped to an empty string that derives an invalid relative URL like "/v1".
        # 纯空白 base_url 是真值会绕过 or，须 strip 后回落默认 host，
        # 不能 strip 成空串派生出 "/v1" 这类非法相对 URL
        assert agnes_base_url("   ") == AGNES_BASE_URL


class TestHostDerivation:
    """agnes_host shares normalization with agnes_base_url; the only difference is no /v1 -- the gateway's final-video
    query endpoint /agnesapi sits under the host root, and joining needs that form.

    agnes_host 与 agnes_base_url 同源归一化，差别只在不带 /v1——网关的成片查询端点
    /agnesapi 挂在 host 根下，拼接需要这个形态。"""

    def test_default_host(self):
        assert agnes_host(None) == "https://apihub.agnes-ai.com"

    def test_strips_v1_suffix(self):
        assert agnes_host("https://apihub.agnes-ai.com/v1") == "https://apihub.agnes-ai.com"
        assert agnes_host("https://apihub.agnes-ai.com/v1/") == "https://apihub.agnes-ai.com"

    def test_host_only_unchanged(self):
        assert agnes_host("https://apihub.agnes-ai.com") == "https://apihub.agnes-ai.com"
        assert agnes_host("https://relay.example.com/") == "https://relay.example.com"

    def test_whitespace_falls_back_to_default(self):
        assert agnes_host("   ") == "https://apihub.agnes-ai.com"


class TestApiKeyResolution:
    def test_strips_and_returns(self):
        assert resolve_agnes_api_key("  sk-abc  ") == "sk-abc"

    def test_missing_raises(self):
        with pytest.raises(ValueError, match=r"Enter the Agnes API Key on the settings page"):
            resolve_agnes_api_key(None)

    def test_blank_raises(self):
        # No env fallback: a missing key is an explicit error. / 不走 env fallback：缺失即明确报错
        with pytest.raises(ValueError, match=r"Enter the Agnes API Key on the settings page"):
            resolve_agnes_api_key("   ")


class TestHeaders:
    def test_bearer_and_content_type(self):
        h = agnes_headers("sk-abc")
        assert h["Authorization"] == "Bearer sk-abc"
        assert h["Content-Type"] == "application/json"

    def test_strips_whitespace_key(self):
        # Reuses resolve_agnes_api_key: normalize before building the header so no leading/trailing whitespace goes into Bearer.
        # 复用 resolve_agnes_api_key：构造头前先归一化，不把首尾空白带进 Bearer
        assert agnes_headers("  sk-abc  ")["Authorization"] == "Bearer sk-abc"

    def test_blank_key_raises(self):
        # A blank key raises locally instead of building "Bearer " and getting a 401 only at request time.
        # 空白 key 本地即 raise，不拼出 "Bearer " 拖到请求期才 401
        with pytest.raises(ValueError, match=r"Enter the Agnes API Key on the settings page"):
            agnes_headers("   ")
