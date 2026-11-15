"""
Authentication dependency injection integration tests

Tests that authentication behaves correctly on each path after replacing the middleware.

认证依赖注入集成测试

测试替换中间件后，各路径的认证行为是否正确。
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server.auth as auth_module


@pytest.fixture(autouse=True)
def auth_env():
    """Set fixed authentication env vars for all tests and clear the cache afterwards.

    为所有测试设置固定的认证环境变量，测试结束后清理缓存。"""
    auth_module._cached_token_secret = None
    auth_module._cached_password_hash = None
    with patch.dict(
        os.environ,
        {
            "AUTH_USERNAME": "testuser",
            "AUTH_PASSWORD": "testpass",
            "AUTH_TOKEN_SECRET": "test-middleware-secret-key-at-least-32-bytes",
        },
    ):
        yield
    auth_module._cached_token_secret = None
    auth_module._cached_password_hash = None


@pytest.fixture
def middleware_client():
    """Create a test client using the real app.

    创建使用真实 app 的测试客户端。"""
    from server.app import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _login(middleware_client: TestClient) -> str:
    """Helper: log in and return the access_token.

    辅助函数：登录并返回 access_token。"""
    resp = middleware_client.post(
        "/api/v1/auth/token",
        data={"username": "testuser", "password": "testpass"},
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


class TestAuthIntegration:
    def test_health_no_auth(self, middleware_client):
        """GET /health needs no authentication and returns 200

        GET /health 不需要认证，返回 200"""
        resp = middleware_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_login_no_auth(self, middleware_client):
        """POST /api/v1/auth/token needs no authentication

        POST /api/v1/auth/token 不需要认证"""
        resp = middleware_client.post(
            "/api/v1/auth/token",
            data={"username": "testuser", "password": "testpass"},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    def test_api_without_token(self, middleware_client):
        """GET /api/v1/projects without a token returns 401

        GET /api/v1/projects 缺少 token 返回 401"""
        resp = middleware_client.get("/api/v1/projects")
        assert resp.status_code == 401

    def test_api_with_valid_token(self, middleware_client):
        """Log in first to get a token, then access the API with it; should not return 401

        先登录获取 token，再带 token 访问 API，不应返回 401"""
        token = _login(middleware_client)
        resp = middleware_client.get(
            "/api/v1/projects",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code != 401

    def test_api_with_invalid_token(self, middleware_client):
        """Access with an invalid token returns 401

        带无效 token 访问返回 401"""
        resp = middleware_client.get(
            "/api/v1/projects",
            headers={"Authorization": "Bearer invalid-token-value"},
        )
        assert resp.status_code == 401

    def test_docs_page_accessible(self, middleware_client):
        """The /docs Swagger UI should be accessible

        /docs Swagger UI 应可访问"""
        resp = middleware_client.get("/docs")
        assert resp.status_code == 200

    def test_frontend_path_no_auth(self, middleware_client):
        """Frontend paths (not starting with /api/) need no authentication

        前端路径（非 /api/ 开头）不需要认证"""
        resp = middleware_client.get("/app/projects")
        assert resp.status_code != 401
