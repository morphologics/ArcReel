"""Studio user options: one stored JSON object, validated against the known keys, merged on PUT."""

import json
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.db import get_async_session
from server.auth import CurrentUserInfo, get_current_user
from server.dependencies import get_config_service
from server.error_handlers import register_error_handlers
from server.routers import user_options
from tests.auth_deps import AUTH_DEPENDENCIES


class _InMemorySettings:
    """The two ConfigService calls the router makes, over a dict."""

    def __init__(self, stored: dict[str, str] | None = None) -> None:
        self.stored = dict(stored or {})

    async def get_setting(self, key: str, default: str = "") -> str:
        return self.stored.get(key, default)

    async def set_setting(self, key: str, value: str) -> None:
        self.stored[key] = value


def _client(settings: _InMemorySettings) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    session = MagicMock()
    session.commit = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="default", sub="u", role="admin")
    app.dependency_overrides[get_async_session] = lambda: session
    app.dependency_overrides[get_config_service] = lambda: settings
    app.include_router(user_options.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return TestClient(app)


def test_nothing_stored_returns_an_empty_object():
    with _client(_InMemorySettings()) as client:
        resp = client.get("/api/v1/user-options")

    assert resp.status_code == 200
    assert resp.json() == {}


def test_put_merges_into_the_stored_options_and_get_returns_them():
    settings = _InMemorySettings({user_options.USER_OPTIONS_KEY: json.dumps({"mode": "easy"})})
    with _client(settings) as client:
        put = client.put("/api/v1/user-options", json={"default_editor": "openreel"})
        get = client.get("/api/v1/user-options")

    assert put.status_code == 200
    assert put.json() == {"default_editor": "openreel", "mode": "easy"}
    assert get.json() == {"default_editor": "openreel", "mode": "easy"}


def test_null_removes_the_key_back_to_its_default():
    settings = _InMemorySettings({user_options.USER_OPTIONS_KEY: json.dumps({"mode": "easy", "default_editor": "openreel"})})
    with _client(settings) as client:
        resp = client.put("/api/v1/user-options", json={"mode": None})

    assert resp.json() == {"default_editor": "openreel"}
    assert json.loads(settings.stored[user_options.USER_OPTIONS_KEY]) == {"default_editor": "openreel"}


def test_unknown_key_is_rejected_and_nothing_is_written():
    settings = _InMemorySettings()
    with _client(settings) as client:
        resp = client.put("/api/v1/user-options", json={"mode": "easy", "font_size": "huge"})

    assert resp.status_code == 422
    assert settings.stored == {}


def test_value_outside_the_allowed_set_is_rejected():
    settings = _InMemorySettings()
    with _client(settings) as client:
        resp = client.put("/api/v1/user-options", json={"default_editor": "premiere"})

    assert resp.status_code == 422
    assert settings.stored == {}


def test_stored_keys_no_longer_known_are_left_out_of_the_answer():
    settings = _InMemorySettings({user_options.USER_OPTIONS_KEY: json.dumps({"mode": "advanced", "retired": "x"})})
    with _client(settings) as client:
        resp = client.get("/api/v1/user-options")

    assert resp.json() == {"mode": "advanced"}
