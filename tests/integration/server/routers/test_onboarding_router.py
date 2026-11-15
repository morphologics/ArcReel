"""Tests for the onboarding state routes.

Onboarding 引导状态路由测试。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lib.config.service import ConfigService
from lib.db import get_async_session
from server.auth import CurrentUserInfo, get_current_user
from server.routers import onboarding
from tests.auth_deps import AUTH_DEPENDENCIES


def _make_app(session_factory) -> FastAPI:
    app = FastAPI()

    async def override_session():
        async with session_factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_async_session] = override_session
    # The auth dependency is attached at registration; the mini app must attach it the same way as server/app.py,
    # otherwise the real auth behavior cannot be tested.
    # 认证依赖挂在注册处，mini app 须与 server/app.py 的挂法一致，否则测不到真实的鉴权行为。
    app.include_router(onboarding.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return app


@pytest_asyncio.fixture
async def onboarding_client(db_factory):
    app = _make_app(db_factory)
    app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="default", sub="testuser", role="admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def unauth_client(db_factory, monkeypatch):
    """No dependency override → real auth applies → expects 401/403."""
    # With AUTH_ENABLED=false get_current_user returns an anonymous admin directly, so this fixture cannot test
    # rejection.
    # AUTH_ENABLED=false 时 get_current_user 直接返回匿名 admin，本 fixture 就测不到拒绝。
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = _make_app(db_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_status_reports_not_seen_when_flag_missing(onboarding_client) -> None:
    resp = await onboarding_client.get("/api/v1/onboarding/status")
    assert resp.status_code == 200
    assert resp.json() == {"seen": False}


@pytest.mark.asyncio
async def test_mark_seen_then_status_reports_seen(onboarding_client) -> None:
    marked = await onboarding_client.post("/api/v1/onboarding/seen")
    assert marked.status_code == 200
    assert marked.json() == {"seen": True}

    resp = await onboarding_client.get("/api/v1/onboarding/status")
    assert resp.json() == {"seen": True}


@pytest.mark.asyncio
async def test_mark_seen_is_idempotent(onboarding_client) -> None:
    for _ in range(3):
        resp = await onboarding_client.post("/api/v1/onboarding/seen")
        assert resp.status_code == 200
        assert resp.json() == {"seen": True}

    assert (await onboarding_client.get("/api/v1/onboarding/status")).json() == {"seen": True}


@pytest.mark.asyncio
async def test_mark_seen_persists_flag_under_expected_key(onboarding_client, db_factory) -> None:
    """The flag is written to the instance-level SystemSetting `onboarding_seen`, not kept in memory.

    标记写在实例级 SystemSetting 的 `onboarding_seen` 上，不是内存态。"""
    await onboarding_client.post("/api/v1/onboarding/seen")

    async with db_factory() as session:
        stored = await ConfigService(session).get_setting(onboarding.ONBOARDING_SEEN_KEY)
    assert stored == "true"


@pytest.mark.asyncio
async def test_mark_seen_writes_only_the_flag(onboarding_client, db_factory) -> None:
    """Zero side effects: marking onboarding writes only one setting and touches no other config.

    零副作用：标记引导只落一个 setting，不碰其他配置。"""
    async with db_factory() as session:
        before = await ConfigService(session).get_all_settings()

    await onboarding_client.post("/api/v1/onboarding/seen")

    async with db_factory() as session:
        after = await ConfigService(session).get_all_settings()
    assert set(after) - set(before) == {onboarding.ONBOARDING_SEEN_KEY}
    assert {k: v for k, v in after.items() if k != onboarding.ONBOARDING_SEEN_KEY} == before


@pytest.mark.asyncio
async def test_status_rejects_unauthenticated(unauth_client) -> None:
    resp = await unauth_client.get("/api/v1/onboarding/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_mark_seen_rejects_unauthenticated(unauth_client) -> None:
    resp = await unauth_client.post("/api/v1/onboarding/seen")
    assert resp.status_code == 401
