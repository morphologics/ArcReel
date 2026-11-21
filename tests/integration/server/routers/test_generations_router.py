"""Generation records over REST (API G16) and incremental sync (API G24): snapshot at enqueue, records for attempts
that never reached a provider, the exact result link, filters, cursor paging, the 404 code, and ``updated_since`` with
``server_time`` on /generations, /tasks, /projects/{p}/tasks and /usage/records.

生成记录的 REST 接口（API G16）与增量同步（API G24）：入队快照、从未到达供应商的尝试也有记录、结果的精确关联、筛选、游标
分页、404 错误码，以及 /generations、/tasks、/projects/{p}/tasks、/usage/records 上的 ``updated_since`` 与 ``server_time``。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import update

from lib.backends.providers import CallStatus
from lib.db.models.api_call import ApiCall
from lib.db.models.task import Task
from lib.generation.generation_queue import GenerationQueue
from lib.generation.task_failure import encode_failure
from lib.project.project_manager import ProjectManager
from server.client_identity import request_client
from server.error_handlers import register_error_handlers
from server.routers import generations, tasks, usage
from tests.auth_deps import AUTH_DEPENDENCIES, override_auth

PROJECT = "demo"
OTHER = "other"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
SCRIPT = {
    "episode": 1,
    "content_mode": "drama",
    "scenes": [
        {
            "scene_id": "E1S01",
            "image_prompt": {"scene": "@[Ann] at the door"},
            "video_prompt": {"action": "She opens the door"},
            "characters_in_scene": ["Ann"],
            "scenes": [],
            "props": [],
            "generated_assets": {},
        },
        {
            "scene_id": "E1S02",
            "image_prompt": {"scene": "Close on Ann"},
            "characters_in_scene": ["Ann"],
            "scenes": [],
            "props": [],
            "generated_assets": {},
        },
    ],
}


@pytest.fixture
def projects(tmp_path: Path) -> ProjectManager:
    manager = ProjectManager(tmp_path / "projects")
    for name in (PROJECT, OTHER):
        manager.create_project(name)
        manager.create_project_metadata(name, name.title(), content_mode="drama")
    project_path = manager.get_project_path(PROJECT)
    project = json.loads((project_path / "project.json").read_text(encoding="utf-8"))
    project["characters"] = {"Ann": {"description": "A tall woman", "character_sheet": "characters/Ann.png"}}
    (project_path / "project.json").write_text(json.dumps(project), encoding="utf-8")
    (project_path / "scripts").mkdir(exist_ok=True)
    (project_path / "scripts" / "episode_1.json").write_text(json.dumps(SCRIPT), encoding="utf-8")
    return manager


@pytest.fixture
async def queue(session_factory, projects: ProjectManager) -> GenerationQueue:
    generation_queue = GenerationQueue(session_factory=session_factory, project_manager=projects)
    await generation_queue.acquire_or_renew_worker_lease(name="default", owner_id="worker-a", ttl_seconds=60)
    return generation_queue


@pytest.fixture
async def http(
    session_factory, projects: ProjectManager, queue: GenerationQueue, monkeypatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(generations, "get_project_manager", lambda: projects)
    monkeypatch.setattr(generations, "async_session_factory", session_factory)
    monkeypatch.setattr(tasks, "get_task_queue", lambda: queue)
    monkeypatch.setattr(tasks, "get_project_manager", lambda: projects)
    monkeypatch.setattr(usage, "async_session_factory", session_factory)
    monkeypatch.setattr(usage, "get_project_manager", lambda: projects)
    app = FastAPI(dependencies=[Depends(request_client)])
    register_error_handlers(app)
    override_auth(app)
    for module in (generations, tasks, usage):
        app.include_router(module.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _enqueue(queue: GenerationQueue, resource_id: str, **kwargs: Any) -> str:
    params: dict[str, Any] = {
        "project_name": PROJECT,
        "task_type": "storyboard",
        "media_type": "image",
        "resource_id": resource_id,
        "script_file": "episode_1.json",
        "payload": {"prompt": SCRIPT["scenes"][0]["image_prompt"], "script_file": "episode_1.json"},
        "provider_id": "gemini",
        **kwargs,
    }
    return (await queue.enqueue_task(**params))["task_id"]


async def _set_times(session_factory, task_id: str, *, queued: datetime, updated: datetime | None = None) -> None:
    async with session_factory() as session:
        await session.execute(
            update(Task).where(Task.task_id == task_id).values(queued_at=queued, updated_at=updated or queued)
        )
        await session.commit()


def _write_versions(projects: ProjectManager, records: dict[str, Any]) -> None:
    versions = projects.get_project_path(PROJECT) / "versions"
    versions.mkdir(exist_ok=True)
    (versions / "versions.json").write_text(json.dumps(records), encoding="utf-8")


async def test_snapshot_is_taken_at_enqueue_and_a_cancelled_attempt_keeps_its_inputs(
    http: httpx.AsyncClient, queue: GenerationQueue
) -> None:
    task_id = await _enqueue(queue, "E1S01", source="director")
    await queue.cancel_task(task_id)

    response = await http.get(f"/api/v1/generations/{task_id}")

    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "cancelled"
    assert record["client"] == "director"
    assert record["calls"] == []
    assert record["result"] is None
    assert record["resource"] == {"type": "storyboards", "id": "E1S01", "episode": 1, "script_file": "episode_1.json"}
    request = record["request"]
    assert request["schema"] == 1
    assert request["prompt"]["editable"] == {"scene": "@[Ann] at the door"}
    assert request["prompt"]["source"] == "script.image_prompt"
    assert request["model"]["provider"] == "gemini"
    [ann] = request["references"]
    assert ann["role"] == "asset_sheet"
    assert (ann["resource_type"], ann["resource_id"], ann["file"]) == ("characters", "Ann", "characters/Ann.png")


async def test_a_refused_attempt_is_listed_with_its_inputs_and_error_code(
    http: httpx.AsyncClient, queue: GenerationQueue
) -> None:
    task_id = await _enqueue(queue, "E1S02")
    claimed = await queue.claim_next_task("image")
    assert claimed is not None
    await queue.mark_task_failed(task_id, encode_failure("image_output_missing"))

    response = await http.get("/api/v1/generations", params={"project": PROJECT, "status": "failed"})

    assert response.status_code == 200, response.text
    [record] = response.json()["items"]
    assert record["task_id"] == task_id
    assert record["error_code"] == "image_output_missing"
    assert record["error_message"]
    assert record["calls"] == []
    # Prompts are left out of list items unless asked for. / 列表项默认不带提示词。
    assert record["request"]["prompt"]["editable"] is None
    assert record["request"]["references"][0]["resource_id"] == "Ann"

    with_prompts = await http.get(
        "/api/v1/generations", params={"project": PROJECT, "status": "failed", "include": "prompts"}
    )
    assert with_prompts.json()["items"][0]["request"]["prompt"]["editable"] == {"scene": "Close on Ann"}


async def test_a_succeeded_attempt_joins_its_calls_cost_and_exactly_linked_version(
    http: httpx.AsyncClient, queue: GenerationQueue, projects: ProjectManager, session_factory
) -> None:
    task_id = await _enqueue(queue, "E1S01")
    claimed = await queue.claim_next_task("image")
    assert claimed is not None
    async with session_factory() as session:
        session.add_all(
            [
                ApiCall(
                    project_name=PROJECT,
                    call_type="image",
                    model="img-1",
                    provider="gemini",
                    prompt="RENDERED PROMPT",
                    status=CallStatus.FAILED,
                    started_at=NOW,
                    finished_at=NOW,
                    task_id=task_id,
                    cost_amount=0.0,
                    user_id="default",
                ),
                ApiCall(
                    project_name=PROJECT,
                    call_type="image",
                    model="img-1",
                    provider="gemini",
                    prompt="RENDERED PROMPT",
                    status=CallStatus.SUCCESS,
                    started_at=NOW + timedelta(seconds=5),
                    finished_at=NOW + timedelta(seconds=20),
                    duration_ms=15_000,
                    task_id=task_id,
                    cost_amount=0.04,
                    currency="USD",
                    inputs={"reference_images": [{"path": "characters/Ann.png", "label": None, "role": "array"}]},
                    user_id="default",
                ),
            ]
        )
        await session.commit()
    await queue.mark_task_succeeded(
        task_id, {"version": 2, "file_path": "storyboards/scene_E1S01.png", "resource_type": "storyboards"}
    )
    _write_versions(
        projects,
        {
            "storyboards": {
                "E1S01": {
                    "current_version": 2,
                    "versions": [
                        {
                            "version": 1,
                            "file": "versions/storyboards/E1S01_v1.png",
                            "created_at": "2026-11-20T10:00:00Z",
                        },
                        {
                            "version": 2,
                            "file": "versions/storyboards/E1S01_v2.png",
                            "created_at": "2026-11-21T12:00:20Z",
                            "generation_task_id": task_id,
                            "generation_call_id": 2,
                        },
                    ],
                }
            }
        },
    )

    record = (await http.get(f"/api/v1/generations/{task_id}")).json()

    assert [call["status"] for call in record["calls"]] == ["failed", "success"]
    assert record["calls"][1]["prompt"] == "RENDERED PROMPT"
    assert record["calls"][1]["inputs"]["reference_images"][0]["path"] == "characters/Ann.png"
    assert (record["provider"], record["model"]) == ("gemini", "img-1")
    assert record["cost"] == {"by_currency": {"USD": 0.04}, "unpriced_calls": 0, "pending_calls": 0}
    assert record["result"] == {
        "version": 2,
        "file": "versions/storyboards/E1S01_v2.png",
        "current_file": "storyboards/scene_E1S01.png",
        "thumbnail_url": "/api/v1/files/demo/versions/storyboards/E1S01_v2.png?w=320",
        "created_at": "2026-11-21T12:00:20Z",
        "linked": True,
    }
    listed = (await http.get("/api/v1/generations", params={"project": PROJECT})).json()["items"]
    # The sent prompt stays out of list items unless asked for. / 发出的提示词默认不进列表项。
    assert listed[0]["calls"][1]["prompt"] is None


async def test_filters_and_cursor_paging_newest_first(
    http: httpx.AsyncClient, queue: GenerationQueue, session_factory
) -> None:
    first = await _enqueue(queue, "E1S01")
    second = await _enqueue(queue, "E1S02", source="director")
    asset = await _enqueue(
        queue, "Ann", task_type="character", script_file=None, payload={}, provider_id=None, source="director"
    )
    other = await _enqueue(queue, "E1S01", project_name=OTHER, script_file=None, payload={})
    for offset, task_id in enumerate((first, second, asset, other)):
        await _set_times(session_factory, task_id, queued=NOW + timedelta(minutes=offset))

    page1 = (await http.get("/api/v1/generations", params={"limit": 2})).json()
    assert [item["task_id"] for item in page1["items"]] == [other, asset]
    assert page1["next_cursor"]
    page2 = (await http.get("/api/v1/generations", params={"limit": 2, "cursor": page1["next_cursor"]})).json()
    assert [item["task_id"] for item in page2["items"]] == [second, first]
    assert page2["next_cursor"] is None

    async def ids(**params: Any) -> list[str]:
        response = await http.get("/api/v1/generations", params=params)
        assert response.status_code == 200, response.text
        return [item["task_id"] for item in response.json()["items"]]

    assert await ids(project=PROJECT) == [asset, second, first]
    assert await ids(resource_type="characters") == [asset]
    assert await ids(project=PROJECT, resource_type="storyboards", resource_id="E1S02") == [second]
    assert await ids(episode=1) == [second, first]
    assert await ids(client="director") == [asset, second]
    assert await ids(task_type="character") == [asset]
    assert await ids(since=(NOW + timedelta(minutes=2)).isoformat()) == [other, asset]

    asset_record = (await http.get(f"/api/v1/generations/{asset}")).json()
    assert asset_record["resource"] == {"type": "characters", "id": "Ann", "episode": None, "script_file": None}
    assert asset_record["request"]["prompt"] == {
        "editable": "A tall woman",
        "source": "asset.description",
        "request": None,
    }


async def test_not_found_and_invalid_parameters_use_the_error_codes(http: httpx.AsyncClient) -> None:
    missing = await http.get("/api/v1/generations/nope")
    assert missing.status_code == 404
    assert missing.json()["code"] == "generation_record_not_found"
    assert missing.json()["params"] == {"id": "nope"}

    bad_cursor = await http.get("/api/v1/generations", params={"cursor": "%%%"})
    assert bad_cursor.status_code == 422
    assert bad_cursor.json()["code"] == "generation_cursor_invalid"

    bad_include = await http.get("/api/v1/generations", params={"include": "everything"})
    assert bad_include.status_code == 422
    assert bad_include.json()["code"] == "generation_include_invalid"

    bad_type = await http.get("/api/v1/generations", params={"resource_type": "nonsense"})
    assert bad_type.status_code == 422


async def test_a_task_queued_before_snapshots_gets_a_reduced_request(
    http: httpx.AsyncClient, queue: GenerationQueue, session_factory
) -> None:
    task_id = await _enqueue(queue, "E1S01", payload={"prompt": "old prompt", "seed": 3})
    async with session_factory() as session:
        await session.execute(update(Task).where(Task.task_id == task_id).values(request_snapshot_json=None))
        await session.commit()

    request = (await http.get(f"/api/v1/generations/{task_id}")).json()["request"]

    assert request["schema"] == 0
    assert request["prompt"] == {"editable": None, "source": None, "request": "old prompt"}
    assert request["options"] == {"seed": 3}
    assert request["references"] == []


async def test_text_tasks_are_not_generation_records(http: httpx.AsyncClient, queue: GenerationQueue) -> None:
    text_task = await _enqueue(
        queue, "episode-1", task_type="text_episode_script", media_type="text", script_file=None, payload={}
    )
    assert (await http.get(f"/api/v1/generations/{text_task}")).status_code == 404
    assert (await http.get("/api/v1/generations")).json()["items"] == []


# ---------------------------------------------------------------------------------------------------------------
# G24: updated_since + server_time
# ---------------------------------------------------------------------------------------------------------------


async def test_updated_since_on_generations_and_tasks(
    http: httpx.AsyncClient, queue: GenerationQueue, session_factory
) -> None:
    old = await _enqueue(queue, "E1S01")
    new = await _enqueue(queue, "E1S02")
    await _set_times(session_factory, old, queued=NOW, updated=NOW)
    await _set_times(session_factory, new, queued=NOW, updated=NOW + timedelta(minutes=5))
    since = (NOW + timedelta(minutes=1)).isoformat()

    generations_page = (await http.get("/api/v1/generations", params={"updated_since": since})).json()
    assert [item["task_id"] for item in generations_page["items"]] == [new]
    assert datetime.fromisoformat(generations_page["server_time"]) > NOW

    for path in ("/api/v1/tasks", f"/api/v1/projects/{PROJECT}/tasks"):
        response = await http.get(path, params={"updated_since": since})
        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["task_id"] for item in body["items"]] == [new]
        assert body["total"] == 1
        assert datetime.fromisoformat(body["server_time"]).tzinfo is not None

    # Inclusive: a row changed exactly at the cursor point is returned again (clients dedupe by id).
    # 含端点：恰好在游标时刻变化的行会再次返回（客户端按 id 去重）。
    exact = (NOW + timedelta(minutes=5)).isoformat()
    at_cursor = (await http.get("/api/v1/tasks", params={"updated_since": exact})).json()["items"]
    assert [item["task_id"] for item in at_cursor] == [new]


def _usage_call(*, started: datetime, finished: datetime | None) -> ApiCall:
    return ApiCall(
        project_name=PROJECT,
        call_type="image",
        model="m",
        provider="gemini",
        status=CallStatus.SUCCESS if finished is not None else CallStatus.PENDING,
        started_at=started,
        finished_at=finished,
        user_id="default",
    )


async def test_updated_since_on_usage_records_takes_started_or_finished(
    http: httpx.AsyncClient, session_factory
) -> None:
    async with session_factory() as session:
        calls = [
            # finished long ago: not changed since / 早已结束：之后没变化
            _usage_call(started=NOW - timedelta(hours=2), finished=NOW - timedelta(hours=2)),
            # started before, finished after: changed / 之前开始、之后结束：有变化
            _usage_call(started=NOW - timedelta(hours=1), finished=NOW + timedelta(minutes=10)),
            # started after, still pending without task: changed / 之后开始、无任务仍 pending：有变化
            _usage_call(started=NOW + timedelta(minutes=1), finished=None),
        ]
        session.add_all(calls)
        await session.commit()
        ids = [call.id for call in calls]

    response = await http.get("/api/v1/usage/records", params={"updated_since": NOW.isoformat()})

    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(item["id"] for item in body["items"]) == sorted(ids[1:])
    assert body["server_time"]
    without = (await http.get("/api/v1/usage/records")).json()
    assert {item["id"] for item in without["items"]} == set(ids)
