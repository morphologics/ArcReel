"""The X-Client request header: default, accepted labels, refusal, and the request's client scope.

X-Client 请求头：缺省值、可接受的标签、拒绝，以及请求的客户端作用域。"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from lib.billing.call_client import current_call_client
from server.client_identity import RequestClient, request_client
from server.error_handlers import register_error_handlers


@pytest.fixture
def client() -> TestClient:
    app = FastAPI(dependencies=[Depends(request_client)])
    register_error_handlers(app)

    @app.get("/probe")
    async def probe(client: RequestClient) -> dict[str, str | None]:
        return {"client": client, "scope": current_call_client()}

    return TestClient(app)


def test_without_the_header_the_request_is_the_web_ui(client: TestClient) -> None:
    assert client.get("/probe").json() == {"client": "webui", "scope": "webui"}


@pytest.mark.parametrize("label", ["director", "flow-2", "a.b_c", "x" * 32])
def test_a_valid_label_names_the_request_and_its_scope(client: TestClient, label: str) -> None:
    assert client.get("/probe", headers={"X-Client": label}).json() == {"client": label, "scope": label}


@pytest.mark.parametrize("label", ["", "Director", "has space", "x" * 33, "a/b", "a:b"])
def test_an_invalid_label_is_refused_with_a_code(client: TestClient, label: str) -> None:
    response = client.get("/probe", headers={"X-Client": label})

    assert response.status_code == 422
    assert response.json()["code"] == "client_header_invalid"


def test_the_header_sent_twice_is_refused(client: TestClient) -> None:
    response = client.get("/probe", headers=[("X-Client", "a"), ("X-Client", "b")])

    assert response.status_code == 422
    assert response.json()["code"] == "client_header_invalid"
