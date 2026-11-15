"""Auth coverage tests.

Enumerate all API operations from ``app.openapi()`` and send an unauthenticated request to each to prove 401 -
auth requirements are declared in the router registration block of ``server/app.py``; missing one line leaves
a whole router naked, and this is where that is found.

Endpoints in the exemption list skip that traversal, so each has a separate positive assertion proving it is
not unguarded: public endpoints assert anonymous reachability, self-authenticating endpoints assert that
anonymous requests are still refused.

认证覆盖测试。

从 ``app.openapi()`` 枚举全部 API 操作，逐个发未认证请求实证 401——认证要求声明在
``server/app.py`` 的 router 注册区块，漏挂一行整个 router 就会裸奔，这里是发现它的地方。

豁免清单里的端点会跳过上述遍历，所以每一条都另有正面断言证明它并非不设防：
公开端点断言匿名可达，自带认证端点断言匿名请求仍被拒。
"""

import os
import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server.auth as auth_module

# Goes through the production app's full routing and auth stack, not an isolated unit; by the pytest markers
# discipline it is integration.
# 穿过生产 app 的完整路由与认证栈，不是隔离单元，按 pytest markers 纪律属 integration。

# Anonymously reachable: the login entry is the prerequisite for getting a token; static media load via
# <img src> / <video src>.
# 匿名可达：登录入口是拿 token 的前提；静态媒体经 <img src> / <video src> 加载。
PUBLIC_OPERATIONS = frozenset(
    {
        "GET /health",
        "GET /api/v1/auth/status",
        "POST /api/v1/auth/token",
        "GET /api/v1/files/{project_name}/{path}",
        "GET /api/v1/global-assets/{asset_type}/{filename}",
    }
)

# Self-authenticating: native browser download navigation cannot carry an Authorization header, so the
# endpoint checks a short-lived download token.
# 自带认证：浏览器原生下载导航带不了 Authorization header，端点内校验短时效下载 token。
SELF_AUTH_OPERATIONS = frozenset(
    {
        "GET /api/v1/projects/{name}/export",
        "GET /api/v1/projects/{project_name}/edit-timelines/{timeline_id}/jianying-draft/download",
    }
)

EXEMPT_OPERATIONS = PUBLIC_OPERATIONS | SELF_AUTH_OPERATIONS

# SSE endpoints use an ordinary protected router, consumed by the frontend via fetch with an Authorization
# header; session credentials are not accepted as a query param, see docs/adr/0071.
# SSE 端点走普通受保护 router，由前端以 fetch 带 Authorization header 消费；
# 会话凭证不接受 query param 形态，见 docs/adr/0071。
SSE_PATHS = (
    "/api/v1/projects/demo/assistant/sessions/s1/entries/stream",
    "/api/v1/projects/demo/assistant/sessions/s1/stream",
    "/api/v1/projects/demo/events/stream",
)

# Probe endpoint verifying "let through": protected, and its handler only reads in-process constants. Keep this
# property when changing the endpoint - depending on state set up by the lifespan, such as the database, would
# make these two cases fail with 500 in a clean environment, masking the auth conclusion.
# 验证「放行」的探针端点：受保护，且处理函数只读进程内常量。换端点时要保持这个性质——
# 依赖数据库等 lifespan 建立的状态，会让这两个用例在干净环境里以 500 失败，掩盖认证结论。
_PROBE_ENDPOINT = "/api/v1/custom-providers/endpoints"

# All operation fields of an OpenAPI Path Item. Listing them all rather than just the common ones leaves no gap
# in the promise "new routes are included automatically" - missing one method would silently skip that
# method's endpoints instead of failing.
# OpenAPI Path Item 的全部操作字段。写全而非只列常用的几个，是为了让「新增路由自动纳入」
# 这条承诺不留缺口——漏一个方法，该方法的端点就会被静默跳过而不是报错。
_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_PARAM_RE = re.compile(r"\{([^}]+)\}")
# Path parameter placeholder values: auth runs before parameter validation, so the values only need to form a
# valid URL.
# 路径参数占位值：认证在参数校验之前执行，取值只需能拼出合法 URL。
_PARAM_PLACEHOLDERS = {"path": "x/y.png"}


# Module scope: the whole file shares one auth environment; private caches are cleared on entry and exit so
# other test modules are not polluted.
# module 级：整个文件共用一套认证环境；私有缓存在进出时清空，不污染其它测试模块。
@pytest.fixture(scope="module", autouse=True)
def auth_env():
    auth_module._cached_token_secret = None
    auth_module._cached_password_hash = None
    with patch.dict(
        os.environ,
        {
            "AUTH_ENABLED": "true",
            "AUTH_USERNAME": "testuser",
            "AUTH_PASSWORD": "testpass",
            "AUTH_TOKEN_SECRET": "test-auth-coverage-secret-key-at-least-32-bytes",
        },
    ):
        yield
    auth_module._cached_token_secret = None
    auth_module._cached_password_hash = None


@pytest.fixture(scope="module")
def auth_coverage_client():
    from server.app import app

    # No lifespan (no with): auth dependencies only read env vars and JWT, and routes and openapi are ready at
    # import time.
    # Running the lifespan would drag in DB migration, worker startup and sandbox probing, unrelated to what this
    # file verifies, and would make the results depend on whether the host can create unprivileged user namespaces.
    # 不进 lifespan（不用 with）：认证依赖只读环境变量与 JWT，路由和 openapi 在 import 时即就绪。
    # 跑 lifespan 会把 DB 迁移、worker 启动、沙箱探测一并拖进来，与本文件要验证的东西无关，
    # 还会让测试结果取决于 host 能否创建非特权 user namespace。
    return TestClient(app, raise_server_exceptions=False)


def _fill(path: str) -> str:
    return _PARAM_RE.sub(lambda m: _PARAM_PLACEHOLDERS.get(m.group(1), "1"), path)


def _all_operations() -> list[str]:
    """All API operations, in the form ``"GET /api/v1/providers"``.

    Uses the public ``app.openapi()`` rather than route objects - ``include_router`` mounts lazy wrappers, and
    expanding them would touch FastAPI internals. The cost is that ``include_in_schema=False`` endpoints are
    not covered.

    全部 API 操作，形如 ``"GET /api/v1/providers"``。

    走公开的 ``app.openapi()`` 而非路由对象——``include_router`` 挂的是惰性包装，
    展开它要碰 FastAPI 内部表示。代价是 ``include_in_schema=False`` 的端点不在覆盖内。
    """
    from server.app import app

    spec = app.openapi()
    return [f"{m.upper()} {path}" for path, ops in spec["paths"].items() for m in ops if m in _HTTP_METHODS]


def test_every_protected_endpoint_rejects_anonymous(auth_coverage_client):
    """Operations outside the exemption list: requests without a token always get 401.

    豁免清单之外的操作，无 token 请求一律 401。"""
    operations = _all_operations()
    assert len(operations) > 100, "枚举结果异常，覆盖断言会变成空跑"

    unprotected = []
    for op in operations:
        if op in EXEMPT_OPERATIONS:
            continue
        method, path = op.split(" ", 1)
        resp = auth_coverage_client.request(method, _fill(path), json={})
        if resp.status_code != 401:
            unprotected.append(f"{op} -> {resp.status_code}")

    assert not unprotected, "以下端点未被认证保护：\n" + "\n".join(sorted(unprotected))


def test_exempt_operations_are_registered():
    """The exemption list contains no renamed or deleted endpoints - stale entries would mislead people into
    thinking something is still intentionally open.

    豁免清单不含已改名或删除的端点——过期条目会让人误以为某处仍有意开放。"""
    stale = EXEMPT_OPERATIONS - set(_all_operations())
    assert not stale, f"豁免清单引用了不存在的操作：{sorted(stale)}"


def test_public_endpoints_stay_reachable(auth_coverage_client):
    """Public endpoints are not hit by router-level dependencies by mistake.

    公开端点不被 router 级依赖误伤。"""
    assert auth_coverage_client.get("/health").status_code == 200
    assert auth_coverage_client.get("/api/v1/auth/status").status_code == 200
    assert (
        auth_coverage_client.post(
            "/api/v1/auth/token",
            data={"username": "testuser", "password": "testpass"},
        ).status_code
        == 200
    )
    # Static media: 404 when the project does not exist, not 401.
    # 静态媒体：项目不存在时是 404，不该是 401。
    assert auth_coverage_client.get("/api/v1/files/demo/x.png").status_code != 401
    assert auth_coverage_client.get("/api/v1/global-assets/characters/x.png").status_code != 401


def _login_token(client: TestClient) -> str:
    return client.post(
        "/api/v1/auth/token",
        data={"username": "testuser", "password": "testpass"},
    ).json()["access_token"]


@pytest.mark.parametrize("path", SSE_PATHS)
def test_sse_endpoints_reject_query_token(auth_coverage_client, path):
    """A long-lived session JWT must not subscribe to the event stream via ?token=: 401 even if the JWT is valid.

    长效会话 JWT 不得经 ?token= 订阅事件流：即便 JWT 有效也须 401。"""
    token = _login_token(auth_coverage_client)
    response = auth_coverage_client.get(path, params={"token": token})
    assert response.status_code == 401


@pytest.mark.parametrize("path", SSE_PATHS)
def test_sse_endpoints_accept_bearer_token(auth_coverage_client, path):
    """With a Bearer JWT the router-level dependency lets it through.

    The status after that is decided by the endpoint itself (session missing, endpoint retired, or the project
    events service not set up without the lifespan); here we only assert the auth layer did not refuse.

    带 Bearer JWT 时 router 级依赖放行。

    放行后的状态由端点自身决定（会话不存在、接口已下线，或项目事件服务未随 lifespan
    建立），这里只断言认证层没有拒绝。
    """
    token = _login_token(auth_coverage_client)
    response = auth_coverage_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code != 401


@pytest.mark.parametrize("path", SSE_PATHS)
def test_sse_endpoints_accept_api_key(auth_coverage_client, path):
    """With an ``arc-`` API Key the router-level dependency lets it through.

    带 ``arc-`` API Key 时 router 级依赖放行。"""
    key = "arc-auth-coverage-key"
    key_hash = auth_module.hash_api_key(key)
    auth_module._set_api_key_cache(key_hash, {"sub": "apikey:coverage", "via": "apikey"})
    try:
        response = auth_coverage_client.get(path, headers={"Authorization": f"Bearer {key}"})
        assert response.status_code != 401
    finally:
        auth_module.invalidate_api_key_cache(key_hash)


@pytest.mark.parametrize("path", SSE_PATHS)
def test_sse_endpoints_allow_anonymous_when_auth_disabled(auth_coverage_client, path):
    """With AUTH_ENABLED=false, SSE behaves like other protected endpoints and anonymous requests are let through
    too.

    AUTH_ENABLED=false 时 SSE 与其它受保护端点一致，匿名请求也放行。"""
    with patch.dict(os.environ, {"AUTH_ENABLED": "false"}):
        assert auth_coverage_client.get(path).status_code != 401


def test_trial_artifact_rejects_query_token(auth_coverage_client):
    """Trial artifacts should not put reusable session credentials in the URL.

    试跑产物不应让可复用的会话凭据出现在 URL 里。"""
    token = auth_coverage_client.post(
        "/api/v1/auth/token",
        data={"username": "testuser", "password": "testpass"},
    ).json()["access_token"]
    response = auth_coverage_client.get(
        "/api/v1/custom-endpoints/trial-runs/missing/artifact",
        params={"token": token},
    )
    assert response.status_code == 401


def test_trial_artifact_accepts_bearer_token(auth_coverage_client):
    token = auth_coverage_client.post(
        "/api/v1/auth/token",
        data={"username": "testuser", "password": "testpass"},
    ).json()["access_token"]
    response = auth_coverage_client.get(
        "/api/v1/custom-endpoints/trial-runs/missing/artifact",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/projects/demo/export?download_token=not-a-valid-token",
        "/api/v1/projects/demo/edit-timelines/tl-00000000/jianying-draft/download"
        "?download_token=not-a-valid-token&draft_path=/tmp/d",
    ],
)
def test_export_endpoints_reject_forged_token(auth_coverage_client, path):
    """Export uses a short-lived download token; a forged one must be refused by verify_download_token.

    导出走短时效下载 token，伪造的必须被 verify_download_token 拒绝。"""
    assert auth_coverage_client.get(path).status_code in (401, 403)


def test_authenticated_request_passes(auth_coverage_client):
    """With a valid token the router-level dependency lets it through.

    带合法 token 时 router 级依赖放行。"""
    token = auth_coverage_client.post(
        "/api/v1/auth/token",
        data={"username": "testuser", "password": "testpass"},
    ).json()["access_token"]
    assert auth_coverage_client.get(_PROBE_ENDPOINT, headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_auth_disabled_bypasses_enforcement(auth_coverage_client):
    """With AUTH_ENABLED=false nothing is intercepted, keeping local no-auth deployments usable.

    AUTH_ENABLED=false 时不拦截，保持本地无认证部署可用。"""
    with patch.dict(os.environ, {"AUTH_ENABLED": "false"}):
        assert auth_coverage_client.get(_PROBE_ENDPOINT).status_code == 200


def test_download_token_rejected_as_bearer(auth_coverage_client):
    """A download token with a purpose claim is not a session credential: a protected endpoint gives 401 when it is
    carried as Bearer.

    带 purpose 声明的下载 token 不作为会话凭据：受保护端点以 Bearer 携带时 401。"""
    token = auth_module.create_download_token("testuser", "demo")
    response = auth_coverage_client.get(_PROBE_ENDPOINT, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_export_endpoint_accepts_download_token(auth_coverage_client):
    """The download endpoint's own check still accepts the same token: the auth layer lets it through, and the
    status after that is decided by the endpoint itself.

    下载端点自带校验仍接受同一 token：认证层放行，后续状态由端点自身决定。"""
    token = auth_module.create_download_token("testuser", "demo")
    response = auth_coverage_client.get("/api/v1/projects/demo/export", params={"download_token": token})
    assert response.status_code not in (401, 403)
