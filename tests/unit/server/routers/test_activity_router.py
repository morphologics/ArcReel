"""The activity signal: POST /api/v1/activity answers 204 and needs a signed-in user."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import CurrentUserInfo, get_current_user
from server.error_handlers import register_error_handlers
from server.routers import activity
from tests.auth_deps import AUTH_DEPENDENCIES


def _client(signed_in: bool = True) -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    if signed_in:
        app.dependency_overrides[get_current_user] = lambda: CurrentUserInfo(id="default", sub="u", role="admin")
    app.include_router(activity.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    return TestClient(app)


def test_activity_answers_no_content() -> None:
    response = _client().post("/api/v1/activity")
    assert response.status_code == 204
    assert response.content == b""


def test_activity_is_post_only() -> None:
    assert _client().get("/api/v1/activity").status_code == 405
