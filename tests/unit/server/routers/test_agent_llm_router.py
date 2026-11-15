"""The studio agent's model for the editor's AI chat: requests go to the agent's provider with its key and model."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lib.db import get_async_session
from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import agent_llm
from tests.auth_deps import AUTH_DEPENDENCIES

AGENT_ENV = SimpleNamespace(
    env={"ANTHROPIC_BASE_URL": "https://llm.example", "ANTHROPIC_API_KEY": "sk-secret", "ANTHROPIC_MODEL": "agent-model"},
    pricing=SimpleNamespace(credential_id=7),
)
ANSWER = {
    "type": "message",
    "content": [{"type": "text", "text": "done"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50},
}


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="default", sub="u", role="admin")
    app.dependency_overrides[get_async_session] = lambda: MagicMock()
    app.include_router(agent_llm.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return app


def _patches(upstream: httpx.Response, env=AGENT_ENV, project_exists: bool = True):
    client = MagicMock()
    client.post = AsyncMock(return_value=upstream)
    ledger = MagicMock()
    ledger.record_agent_turn = AsyncMock()
    repo = MagicMock()
    repo.get_active = AsyncMock(return_value=SimpleNamespace(display_name="DeepSeek"))
    pm = MagicMock()
    pm.project_exists = MagicMock(return_value=project_exists)
    return client, ledger, (
        patch.object(agent_llm, "build_anthropic_env", AsyncMock(return_value=env)),
        patch.object(agent_llm, "get_http_client", lambda: client),
        patch.object(agent_llm, "Ledger", lambda: ledger),
        patch.object(agent_llm, "AgentCredentialRepository", lambda _session: repo),
        patch.object(agent_llm, "get_project_manager", lambda: pm),
    )


def _post(patches, body, headers=None):
    with patches[0], patches[1], patches[2], patches[3], patches[4], TestClient(_app()) as client:
        return client.post("/api/v1/agent-llm/messages", json=body, headers=headers or {})


def test_forwards_with_the_agents_key_and_model_and_drops_other_fields():
    upstream = httpx.Response(200, json=ANSWER)
    client, _, patches = _patches(upstream)
    body = {"model": "browser-pick", "stream": True, "max_tokens": 50000, "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "add_clip", "input_schema": {"type": "object"}}]}

    resp = _post(patches, body)

    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "done"
    url = client.post.call_args.args[0]
    sent = client.post.call_args.kwargs["json"]
    headers = client.post.call_args.kwargs["headers"]
    assert url == "https://llm.example/v1/messages"
    assert headers["x-api-key"] == "sk-secret"
    assert sent["model"] == "agent-model"
    assert sent["max_tokens"] == agent_llm.MAX_OUTPUT_TOKENS
    assert "stream" not in sent
    assert sent["tools"] == body["tools"]


def test_usage_is_recorded_under_a_real_project_only():
    _, ledger, patches = _patches(httpx.Response(200, json=ANSWER))
    _post(patches, {"messages": [{"role": "user", "content": "hi"}]}, {"x-youreel-project": "demo", "x-youreel-session": "s1"})
    call = ledger.record_agent_turn.call_args.kwargs
    assert call["project_name"] == "demo"
    assert call["session_id"] == "openreel-s1"
    assert call["calls"][0].model == "agent-model"
    assert call["calls"][0].input_tokens == 150
    assert call["calls"][0].output_tokens == 20

    _, ledger, patches = _patches(httpx.Response(200, json=ANSWER), project_exists=False)
    _post(patches, {"messages": [{"role": "user", "content": "hi"}]}, {"x-youreel-project": "nope"})
    ledger.record_agent_turn.assert_not_called()


def test_upstream_errors_do_not_leak_the_key():
    upstream = httpx.Response(401, json={"error": {"type": "authentication_error", "message": "bad key sk-secret"}})
    _, _, patches = _patches(upstream)
    resp = _post(patches, {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 401
    assert "sk-secret" not in resp.text


def test_no_agent_model_configured():
    env = SimpleNamespace(env={"ANTHROPIC_BASE_URL": "", "ANTHROPIC_API_KEY": ""}, pricing=SimpleNamespace(credential_id=None))
    _, _, patches = _patches(httpx.Response(200, json=ANSWER), env=env)
    assert _post(patches, {"messages": [{"role": "user", "content": "hi"}]}).status_code == 409
    with patches[0], patches[1], patches[2], patches[3], patches[4], TestClient(_app()) as client:
        assert client.get("/api/v1/agent-llm").json() == {"available": False, "model": None, "provider": None}


def test_rejects_a_body_without_messages():
    _, _, patches = _patches(httpx.Response(200, json=ANSWER))
    assert _post(patches, {"messages": []}).status_code == 400
