"""Unit tests for the pure functions of lib.backends.ark_shared (no real HTTP).

lib.backends.ark_shared 纯函数单元测试（不打真实 HTTP）。"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from lib.backends.ark_shared import ARK_BASE_URL, ark_base_url, create_ark_client, resolve_ark_api_key


class TestBaseUrlNormalization:
    def test_default_base(self):
        assert ark_base_url(None) == ARK_BASE_URL

    def test_trailing_slash_stripped(self):
        assert ark_base_url("https://ark.cn-beijing.volces.com/api/v3/") == ARK_BASE_URL

    def test_multiple_trailing_slashes_stripped(self):
        assert ark_base_url("https://ark.cn-beijing.volces.com/api/v3//") == ARK_BASE_URL

    def test_no_trailing_slash_is_idempotent(self):
        assert ark_base_url(ARK_BASE_URL) == ARK_BASE_URL

    def test_whitespace_falls_back_to_default(self):
        # A whitespace-only base_url is truthy and would bypass `or`; it must be stripped and fall back to the default.
        # 纯空白 base_url 是真值会绕过 or，须 strip 后回落默认值
        assert ark_base_url("   ") == ARK_BASE_URL

    def test_surrounding_whitespace_stripped(self):
        assert ark_base_url("  https://ark.cn-beijing.volces.com/api/v3/  ") == ARK_BASE_URL

    def test_non_standard_path_preserved(self):
        # ark-agent-plan uses non-standard paths like /api/plan/v3: only strip the trailing slash, do not rebuild by known suffixes,
        # or it would produce a wrong URL like .../api/plan/v3/api/v3.
        # ark-agent-plan 走 /api/plan/v3 这类非标准路径，只去尾斜杠，不按已知后缀重建，
        # 否则会拼出 .../api/plan/v3/api/v3 这种错误 URL。
        custom = "https://ark.cn-beijing.volces.com/api/plan/v3"
        assert ark_base_url(custom) == custom
        assert ark_base_url(custom + "/") == custom


class TestApiKeyResolution:
    def test_strips_and_returns(self):
        assert resolve_ark_api_key("  sk-abc  ") == "sk-abc"

    def test_missing_raises(self):
        with pytest.raises(ValueError, match=r"Enter the Ark API Key on the settings page"):
            resolve_ark_api_key(None)

    def test_blank_raises(self):
        with pytest.raises(ValueError, match=r"Enter the Ark API Key on the settings page"):
            resolve_ark_api_key("   ")


@contextmanager
def _recorded_ark_sdk() -> Generator[list[dict[str, Any]]]:
    """Recorder for Ark SDK class construction: takes the client-construction args and returns an empty stand-in.

    Ark SDK 类构造的记录器：收下建客户端的参数，回一个空替身。"""
    created: list[dict[str, Any]] = []

    def _create(**kwargs: Any) -> Any:
        created.append(kwargs)
        return MagicMock()

    with patch("volcenginesdkarkruntime.Ark", _create):
        yield created


class TestCreateArkClient:
    def test_trailing_slash_normalized_before_client_construction(self):
        with _recorded_ark_sdk() as created:
            create_ark_client(api_key="k", base_url="https://ark.cn-beijing.volces.com/api/v3/")
        assert created == [{"base_url": ARK_BASE_URL, "api_key": "k"}]

    def test_default_base_url_when_omitted(self):
        with _recorded_ark_sdk() as created:
            create_ark_client(api_key="k")
        assert created == [{"base_url": ARK_BASE_URL, "api_key": "k"}]
