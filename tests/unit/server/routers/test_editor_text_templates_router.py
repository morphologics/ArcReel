"""The member's own Editor text templates: one stored JSON list, validated, replaced on PUT."""

import json
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.db import get_async_session
from server.auth import CurrentUserInfo, get_current_user
from server.dependencies import get_config_service
from server.error_handlers import register_error_handlers
from server.routers import editor_text_templates
from tests.auth_deps import AUTH_DEPENDENCIES

TEMPLATE = {
    "id": "t1",
    "name": "Lower third",
    "text": "Name Here",
    "style": {"fontFamily": "Inter", "fontWeight": 700, "color": "#ffffff"},
    "size": {"mode": "height", "fraction": 0.05},
    "position": {"x": 0.25, "y": 0.82},
}


class _InMemorySettings:
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
    app.include_router(editor_text_templates.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return TestClient(app)


def test_nothing_stored_returns_an_empty_list():
    with _client(_InMemorySettings()) as client:
        resp = client.get("/api/v1/editor/text-templates")
    assert resp.status_code == 200
    assert resp.json() == {"templates": []}


def test_put_replaces_the_list_and_get_returns_it():
    settings = _InMemorySettings()
    with _client(settings) as client:
        put = client.put("/api/v1/editor/text-templates", json={"templates": [TEMPLATE]})
        get = client.get("/api/v1/editor/text-templates")
    assert put.status_code == 200
    assert get.json() == {"templates": [TEMPLATE]}
    assert json.loads(settings.stored[editor_text_templates.TEXT_TEMPLATES_KEY]) == [TEMPLATE]


def test_invalid_templates_are_refused():
    bad_size = {**TEMPLATE, "size": {"mode": "width", "fraction": 1.5}}
    bad_position = {**TEMPLATE, "position": {"x": -0.1, "y": 0.5}}
    unknown_key = {**TEMPLATE, "script": "x"}
    huge_style = {**TEMPLATE, "style": {"a": "x" * 9000}}
    with _client(_InMemorySettings()) as client:
        for template in (bad_size, bad_position, unknown_key, huge_style):
            resp = client.put("/api/v1/editor/text-templates", json={"templates": [template]})
            assert resp.status_code == 422, template


def test_a_stored_template_that_no_longer_validates_is_dropped_not_fatal():
    settings = _InMemorySettings({editor_text_templates.TEXT_TEMPLATES_KEY: json.dumps([TEMPLATE, {"id": "broken"}])})
    with _client(settings) as client:
        resp = client.get("/api/v1/editor/text-templates")
    assert resp.json() == {"templates": [TEMPLATE]}


def test_subtitle_styles_are_remembered_per_episode_and_forgotten():
    settings = _InMemorySettings()
    with _client(settings) as client:
        assert client.get("/api/v1/editor/subtitle-styles").json() == {"styles": {}}
        client.put("/api/v1/editor/subtitle-styles", json={"key": "broken-mirror#7", "template": TEMPLATE})
        client.put("/api/v1/editor/subtitle-styles", json={"key": "broken-mirror#9", "template": {**TEMPLATE, "name": "Top"}})
        both = client.get("/api/v1/editor/subtitle-styles").json()["styles"]
        assert both["broken-mirror#7"]["name"] == "Lower third"
        assert both["broken-mirror#9"]["name"] == "Top"
        client.put("/api/v1/editor/subtitle-styles", json={"key": "broken-mirror#7"})
        assert list(client.get("/api/v1/editor/subtitle-styles").json()["styles"]) == ["broken-mirror#9"]


def test_a_bad_subtitle_style_is_refused():
    with _client(_InMemorySettings()) as client:
        resp = client.put("/api/v1/editor/subtitle-styles", json={"key": "", "template": TEMPLATE})
        assert resp.status_code == 422


TOOL_TEMPLATE = {
    "id": "mine:1",
    "name": "Shot 2 · Dining Room",
    "tool": "Color Grading",
    "settings": {"colorGrading": {"temperature": 12, "tint": -3}},
}


def test_tool_templates_start_empty_and_put_replaces_them():
    settings = _InMemorySettings()
    with _client(settings) as client:
        empty = client.get("/api/v1/editor/tool-templates")
        put = client.put("/api/v1/editor/tool-templates", json={"templates": [TOOL_TEMPLATE]})
        get = client.get("/api/v1/editor/tool-templates")
    assert empty.json() == {"templates": []}
    assert put.status_code == 200
    assert get.json() == {"templates": [TOOL_TEMPLATE]}
    assert json.loads(settings.stored["editor_tool_templates"]) == [TOOL_TEMPLATE]


def test_a_tool_template_without_a_tool_or_too_large_is_refused():
    with _client(_InMemorySettings()) as client:
        no_tool = client.put("/api/v1/editor/tool-templates", json={"templates": [{**TOOL_TEMPLATE, "tool": ""}]})
        huge = client.put(
            "/api/v1/editor/tool-templates",
            json={"templates": [{**TOOL_TEMPLATE, "settings": {"blob": "x" * (70 * 1024)}}]},
        )
    assert no_tool.status_code == 422
    assert huge.status_code == 422


def test_a_stored_tool_template_that_no_longer_validates_is_dropped():
    stored = json.dumps([TOOL_TEMPLATE, {"id": "bad", "name": "no tool", "settings": {}}])
    with _client(_InMemorySettings({"editor_tool_templates": stored})) as client:
        resp = client.get("/api/v1/editor/tool-templates")
    assert resp.json() == {"templates": [TOOL_TEMPLATE]}
