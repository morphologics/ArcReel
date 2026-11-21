"""Engine token middleware (server/engine_token.py): ENGINE_PROXY_TOKEN, header X-Engine-Token."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.engine_token import EngineTokenMiddleware, configured_engine_token, is_local_unforwarded

TOKEN = "0123456789abcdef" * 4
HOST_CLIENT = ("172.30.1.1", 40000)  # how a request from the host arrives in a container (bridge gateway)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(EngineTokenMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/echo")
    async def echo(request: Request):
        return {"engine_token_seen": request.headers.get("x-engine-token"), "client": request.client.host}

    @app.post("/api/v1/echo")
    async def echo_post(request: Request):
        return {"body": (await request.body()).decode()}

    @app.get("/api/v1/stream")
    async def stream():
        async def events():
            for i in range(3):
                yield f"data: {i}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    return app


@pytest.fixture
def token_on(monkeypatch):
    monkeypatch.setenv("ENGINE_PROXY_TOKEN", TOKEN)


@pytest.fixture
def client():
    return TestClient(_app(), client=HOST_CLIENT)


# ── off: unchanged behaviour ────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_or_empty_means_no_check(monkeypatch, client, value):
    if value is None:
        monkeypatch.delenv("ENGINE_PROXY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ENGINE_PROXY_TOKEN", value)
    r = client.get("/api/v1/echo")
    assert r.status_code == 200
    # a header sent anyway is not consumed when the check is off
    r = client.get("/api/v1/echo", headers={"X-Engine-Token": "whatever"})
    assert r.status_code == 200
    assert r.json()["engine_token_seen"] == "whatever"


def test_configured_engine_token_strips():
    assert configured_engine_token({"ENGINE_PROXY_TOKEN": f"  {TOKEN}\n"}) == TOKEN
    assert configured_engine_token({}) == ""


# ── on ───────────────────────────────────────────────────────────────────────


def test_missing_token_is_401_with_error_shape(token_on, client):
    r = client.get("/api/v1/echo", headers={"Accept-Language": "en"})
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == "engine_token_required"
    assert body["params"] == {}
    assert "engine token" in body["detail"]
    assert r.headers["cache-control"] == "no-store"


def test_detail_follows_accept_language(token_on, client):
    r = client.get("/api/v1/echo", headers={"Accept-Language": "zh-CN"})
    assert r.json()["detail"] == "本服务只接受经由反向代理的请求（缺少引擎令牌）"


@pytest.mark.parametrize("sent", ["wrong", TOKEN[:-1], TOKEN + "0", TOKEN.upper(), ""])
def test_wrong_token_is_401_invalid(token_on, client, sent):
    r = client.get("/api/v1/echo", headers={"X-Engine-Token": sent})
    assert r.status_code == 401
    assert r.json()["code"] == "engine_token_invalid"


def test_duplicate_header_is_refused(token_on, client):
    r = client.get("/api/v1/echo", headers=[("X-Engine-Token", TOKEN), ("X-Engine-Token", TOKEN)])
    assert r.status_code == 401
    assert r.json()["code"] == "engine_token_invalid"


def test_right_token_passes_and_is_removed(token_on, client):
    r = client.get("/api/v1/echo", headers={"X-Engine-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["engine_token_seen"] is None


def test_post_body_reaches_the_route(token_on, client):
    r = client.post("/api/v1/echo", content=b"payload", headers={"X-Engine-Token": TOKEN})
    assert r.status_code == 200
    assert r.json() == {"body": "payload"}
    assert client.post("/api/v1/echo", content=b"payload").status_code == 401


def test_sse_stream(token_on, client):
    assert client.get("/api/v1/stream").status_code == 401
    with client.stream("GET", "/api/v1/stream", headers={"X-Engine-Token": TOKEN}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "".join(r.iter_text()) == "data: 0\n\ndata: 1\n\ndata: 2\n\n"


def test_health_is_exempt(token_on, client):
    assert client.get("/health").status_code == 200
    # only the exact path
    assert client.get("/health/x").status_code == 401


def test_websocket(token_on, client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
    with client.websocket_connect("/ws", headers={"X-Engine-Token": TOKEN}) as ws:
        assert ws.receive_text() == "hi"


# ── loopback exemption ──────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"])
def test_own_loopback_is_exempt(token_on, host):
    c = TestClient(_app(), client=(host, 50000))
    assert c.get("/api/v1/echo").status_code == 200


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Real-IP", "Forwarded"])
def test_relayed_loopback_needs_the_token(token_on, header):
    """A proxy in the same network namespace (a dev server proxying /api) passes the forwarding headers."""
    c = TestClient(_app(), client=("127.0.0.1", 50000))
    r = c.get("/api/v1/echo", headers={header: "203.0.113.9"})
    assert r.status_code == 401
    assert r.json()["code"] == "engine_token_required"
    assert c.get("/api/v1/echo", headers={header: "203.0.113.9", "X-Engine-Token": TOKEN}).status_code == 200


@pytest.mark.parametrize("host", ["172.30.1.1", "10.0.0.1", "testclient", "", "fe80::1%eth0"])
def test_not_loopback(host):
    assert not is_local_unforwarded({"type": "http", "client": (host, 1), "headers": []})


def test_no_client_is_not_loopback():
    assert not is_local_unforwarded({"type": "http", "client": None, "headers": []})


# ── the real app ─────────────────────────────────────────────────────────────


@pytest.fixture
def real_client(token_on):
    from server.app import app

    # no `with`: lifespan (database, workers) is not needed for what is checked here
    return TestClient(app, client=HOST_CLIENT, raise_server_exceptions=False)


def test_real_app_health_exempt(real_client):
    assert real_client.get("/health").status_code == 200


@pytest.mark.parametrize("path", ["/api/v1/projects", "/api/v1/tasks/stats", "/api/v1/auth/token", "/"])
def test_real_app_routes_need_the_token(real_client, path):
    r = real_client.get(path)
    assert r.status_code == 401
    assert r.json()["code"] == "engine_token_required"


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_real_app_remote_mcp_mount_needs_the_token(real_client, path):
    r = real_client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401
    assert r.json()["code"] == "engine_token_required"
    r = real_client.post(path, json={}, headers={"X-Engine-Token": "nope"})
    assert r.json()["code"] == "engine_token_invalid"


def test_real_app_cors_preflight_is_answered_before_the_check(real_client):
    r = real_client.options(
        "/api/v1/projects",
        headers={"Origin": "https://example.test", "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 200


def test_real_app_401_has_cors_headers(real_client):
    r = real_client.get("/api/v1/projects", headers={"Origin": "https://example.test"})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == "*"
