"""YouReel: HTTP contract of the client error reports (Settings → Options → Diagnostics)."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lib.db import get_async_session
from lib.db.models.client_error_report import ClientErrorReport
from lib.db.repositories.client_error_repo import ClientErrorReportRepository, NewClientErrorReport
from server.routers import diagnostics

URL = "/diagnostics/client-errors"


@pytest.fixture
async def client(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()

    async def session_override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = session_override
    app.include_router(diagnostics.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"User-Agent": "probe/1.0"}
    ) as c:
        yield c


async def test_reports_are_stored_without_query_strings_and_listed_newest_first(client: httpx.AsyncClient):
    first = {
        "kind": "toast",
        "message": "Could not open the editor",
        "detail": {"page": "/app/projects/demo/edit?token=secret", "source": "toast"},
    }
    second = {
        "kind": "request",
        "message": "HTTP 500",
        "detail": {
            "page": "/app/projects/demo#x",
            "method": "post",
            "api_path": "/api/v1/projects/demo/edit-timelines?access_token=secret",
            "status": 500,
            "unknown_field": "dropped",
        },
    }
    response = await client.post(URL, json={"entries": [first, second], "app_build": "index-abc123"})
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 2}

    listed = (await client.get(URL)).json()
    assert listed["kept"] == 1000
    entries = listed["entries"]
    assert [e["kind"] for e in entries] == ["request", "toast"]
    assert entries[0]["detail"] == {
        "page": "/app/projects/demo",
        "method": "POST",
        "api_path": "/api/v1/projects/demo/edit-timelines",
        "status": 500,
        "source": None,
        "stack": None,
    }
    assert entries[1]["detail"]["page"] == "/app/projects/demo/edit"
    assert entries[1]["message"] == "Could not open the editor"
    assert entries[1]["user_agent"] == "probe/1.0"
    assert entries[1]["app_build"] == "index-abc123"
    assert entries[1]["created_at"].endswith("Z")
    assert "secret" not in str(listed)


async def test_long_texts_are_trimmed(client: httpx.AsyncClient):
    entry = {"kind": "exception", "message": "m" * 5000, "detail": {"stack": "s" * 9000}}
    assert (await client.post(URL, json={"entries": [entry]})).status_code == 200
    stored = (await client.get(URL)).json()["entries"][0]
    assert len(stored["message"]) == diagnostics.MESSAGE_MAX
    assert len(stored["detail"]["stack"]) == diagnostics.STACK_MAX


@pytest.mark.parametrize(
    "body",
    [
        {"entries": []},
        {"entries": [{"kind": "toast", "message": "x"}] * 21},
        {"entries": [{"kind": "warning", "message": "x"}]},
        {"entries": [{"kind": "toast", "message": ""}]},
        {"entries": [{"kind": "toast", "message": "x" * 10_001}]},
    ],
    ids=["empty", "over-20", "unknown-kind", "empty-message", "absurd-message"],
)
async def test_invalid_batches_are_refused_and_nothing_is_stored(client: httpx.AsyncClient, body: dict):
    assert (await client.post(URL, json=body)).status_code == 422
    assert (await client.get(URL)).json()["entries"] == []


async def test_list_limit_and_clear(client: httpx.AsyncClient):
    entries = [{"kind": "toast", "message": f"e{i}"} for i in range(5)]
    assert (await client.post(URL, json={"entries": entries})).status_code == 200
    assert [e["message"] for e in (await client.get(URL, params={"limit": 2})).json()["entries"]] == ["e4", "e3"]

    cleared = await client.delete(URL)
    assert cleared.status_code == 200
    assert cleared.json() == {"deleted": 5}
    assert (await client.get(URL)).json()["entries"] == []


async def test_insert_prunes_to_the_newest_rows(session_factory: async_sessionmaker[AsyncSession]):
    def report(i: int) -> NewClientErrorReport:
        return NewClientErrorReport(kind="toast", message=f"e{i}", detail=None, user_agent=None, app_build=None)

    async with session_factory() as session:
        repo = ClientErrorReportRepository(session)
        await repo.add_many([report(i) for i in range(4)], keep=3)
        await repo.add_many([report(4)], keep=3)
        await session.commit()

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ClientErrorReport)) == 3
        kept = await ClientErrorReportRepository(session).list_recent(10)
        assert [r.message for r in kept] == ["e4", "e3", "e2"]
