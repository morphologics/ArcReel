"""YouReel: HTTP contract of the client error reports (Settings → Options → Diagnostics)."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import lib.db
from lib.db import get_async_session
from lib.db.models.client_error_report import ClientErrorReport
from lib.db.repositories import client_error_repo
from lib.db.repositories.client_error_repo import ClientErrorReportRepository, NewClientErrorReport
from server.perf_timing import RequestTiming, write_timing
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
    assert listed["kept_perf"] == 500
    entries = listed["entries"]
    assert [e["kind"] for e in entries] == ["request", "toast"]
    assert entries[0]["detail"] == {
        "page": "/app/projects/demo",
        "method": "POST",
        "api_path": "/api/v1/projects/demo/edit-timelines",
        "status": 500,
        "source": None,
        "stack": None,
        "tag": None,
        "client": None,
        "metrics": None,
        "resource_groups": None,
        "top_resources": None,
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
        {"entries": [{"kind": "server_slow", "message": "x"}]},
        {"entries": [{"kind": "toast", "message": ""}]},
        {"entries": [{"kind": "toast", "message": "x" * 10_001}]},
    ],
    ids=["empty", "over-20", "unknown-kind", "server-kind-from-page", "empty-message", "absurd-message"],
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


async def test_perf_reports_keep_only_numbers_and_paths_without_queries(client: httpx.AsyncClient):
    perf = {
        "kind": "perf",
        "message": "Page load 2.3 s · 667 files",
        "detail": {
            "page": "/app/projects/demo?token=secret",
            "tag": "load",
            "metrics": {
                "load_ms": 2312.46,
                "res_count": 667,
                "note": "a text is dropped",
                "Bad-Key": 1,
                "flag": True,
            },
            "resource_groups": [{"type": "script", "count": 600, "transfer_bytes": 1e6, "decoded_bytes": 4e6}],
            "top_resources": [
                {"path": "/src/main.tsx?t=123&token=secret", "type": "script", "decoded_bytes": 5, "duration_ms": 9.04},
                {"path": "/api/v1/x#frag", "type": "bad type!", "transfer_bytes": 1},
            ],
        },
    }
    jank = {"kind": "jank", "message": "Main thread blocked 3.1 s in 10 s", "detail": {"metrics": {"blocked_ms": 3100}}}
    assert (await client.post(URL, json={"entries": [perf, jank]})).status_code == 200

    listed = (await client.get(URL)).json()
    stored = listed["entries"][1]
    assert stored["kind"] == "perf"
    detail = stored["detail"]
    assert detail["page"] == "/app/projects/demo"
    assert detail["tag"] == "load"
    assert detail["metrics"] == {"load_ms": 2312.5, "res_count": 667}
    assert detail["resource_groups"] == [
        {"type": "script", "count": 600, "transfer_bytes": 1000000, "decoded_bytes": 4000000}
    ]
    assert [r["path"] for r in detail["top_resources"]] == ["/src/main.tsx", "/api/v1/x"]
    assert detail["top_resources"][1]["type"] is None
    assert listed["entries"][0]["kind"] == "jank"
    assert "secret" not in str(listed)


async def test_list_and_clear_by_kind(client: httpx.AsyncClient):
    entries = [
        {"kind": "toast", "message": "t"},
        {"kind": "perf", "message": "p"},
        {"kind": "jank", "message": "j"},
    ]
    assert (await client.post(URL, json={"entries": entries})).status_code == 200
    perf_only = (await client.get(URL, params={"kind": "perf,jank"})).json()["entries"]
    assert [e["kind"] for e in perf_only] == ["jank", "perf"]
    assert (await client.get(URL, params=[("kind", "toast")])).json()["entries"][0]["message"] == "t"
    assert (await client.get(URL, params={"kind": "nonsense"})).json()["entries"] == []

    newest = perf_only[0]["id"]
    cleared = await client.delete(URL, params={"kind": "perf,jank", "min_id": newest})
    assert cleared.json() == {"deleted": 1}
    assert [e["kind"] for e in (await client.get(URL)).json()["entries"]] == ["perf", "toast"]


async def test_errors_and_perf_records_are_capped_apart(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(client_error_repo, "kept_for", lambda kind: 2 if kind in {"perf", "jank", "server_slow"} else 3)

    def report(kind: str, i: int) -> NewClientErrorReport:
        return NewClientErrorReport(kind=kind, message=f"{kind}{i}", detail=None, user_agent=None, app_build=None)

    async with session_factory() as session:
        repo = ClientErrorReportRepository(session)
        await repo.add_many([report("toast", i) for i in range(3)])
        # a run of performance records leaves the errors alone
        for i in range(6):
            await repo.add_many([report("perf" if i % 2 else "server_slow", i)])
        await repo.add_many([report("exception", 9)])
        await session.commit()

    async with session_factory() as session:
        kept = [r.message for r in await ClientErrorReportRepository(session).list_recent(20)]
    assert kept == ["exception9", "perf5", "server_slow4", "toast2", "toast1"]


def test_metrics_drop_non_finite_numbers():
    assert diagnostics._stored_metrics({"a_ms": float("inf"), "b_ms": float("nan"), "c": 2}) == {"c": 2}


async def test_server_slow_is_written_through_the_repository(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient
):
    monkeypatch.setattr(lib.db, "safe_session_factory", session_factory)
    await write_timing(RequestTiming("GET", "/api/v1/projects/x", 200, "api", "webui", 2500.0, 2400.0, 800.0, 1234))
    entries = (await client.get(URL, params={"kind": "server_slow"})).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["message"] == "GET /api/v1/projects/x · 2.5 s"
    assert entries[0]["detail"]["metrics"] == {"wall_ms": 2500.0, "cpu_ms": 800.0, "bytes": 1234, "ttfb_ms": 2400.0}
    assert entries[0]["detail"]["client"] == "webui"
