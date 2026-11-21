"""Generation batches over REST: read, cancel preview, cancel, the not-found code, and the caller's X-Client carried
from a batch submission to its tasks and the batch read.

生成批次的 REST 接口：读取、取消预览、取消、未找到的错误码，以及调用方的 X-Client 从批量提交带到任务与批次读取。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import null

from lib.backends.providers import CallStatus
from lib.custom_provider import make_provider_id
from lib.db.models.api_call import ApiCall
from lib.db.repositories.custom_provider_repo import CustomProviderRepository
from lib.generation.generation_batch import GenerationBatchRequestedItem, GenerationBatchRequestSnapshot
from lib.generation.generation_queue import GenerationQueue
from lib.generation.generation_result import GenerationSelectionMode
from lib.project.project_manager import ProjectManager
from server.client_identity import request_client
from server.error_handlers import register_error_handlers
from server.routers import batches, storyboard_batches
from tests.auth_deps import AUTH_DEPENDENCIES, override_auth

PROJECT = "demo"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def projects(tmp_path: Path) -> ProjectManager:
    manager = ProjectManager(tmp_path / "projects")
    manager.create_project(PROJECT)
    manager.create_project_metadata(PROJECT, "Demo", content_mode="narration")
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
    for module in (batches, storyboard_batches):
        monkeypatch.setattr(module, "get_project_manager", lambda: projects)
        monkeypatch.setattr(module, "get_generation_queue", lambda: queue)
        monkeypatch.setattr(module, "async_session_factory", session_factory)
    # Same app-level dependency as server/app.py. / 与 server/app.py 相同的 app 级依赖。
    app = FastAPI(dependencies=[Depends(request_client)])
    register_error_handlers(app)
    override_auth(app)
    app.include_router(batches.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    app.include_router(storyboard_batches.router, prefix="/api/v1", dependencies=AUTH_DEPENDENCIES)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _batch_in_three_states(queue: GenerationQueue) -> tuple[str, dict[str, str]]:
    """One batch whose members are succeeded (E1S01), running (E1S02) and queued (E1S03); returns unit -> task id.

    一个批次，成员分别为已成功（E1S01）、执行中（E1S02）、排队中（E1S03）；返回 unit → 任务 id。"""
    units = ("E1S01", "E1S02", "E1S03")
    batch_id = await queue.create_generation_batch(
        project_name=PROJECT,
        operation="generate_storyboards",
        requested=GenerationBatchRequestSnapshot(
            selection=GenerationSelectionMode.EXPLICIT,
            requested=[GenerationBatchRequestedItem(unit_id=unit) for unit in units],
        ),
        blocked=[],
        source="director",
    )
    task_ids: dict[str, str] = {}
    for unit in units:
        enqueued = await queue.enqueue_task(
            project_name=PROJECT,
            task_type="storyboard",
            media_type="image",
            resource_id=unit,
            script_file="episode_1.json",
            source="director",
            batch_id=batch_id,
            batch_unit_id=unit,
        )
        task_ids[unit] = enqueued["task_id"]
        if unit == "E1S01":
            claimed = await queue.claim_next_task("image")
            assert claimed is not None
            await queue.mark_task_succeeded(enqueued["task_id"], {"file_path": "storyboards/E1S01.png"})
        elif unit == "E1S02":
            claimed = await queue.claim_next_task("image")
            assert claimed is not None
    return batch_id, task_ids


async def test_read_reports_members_counts_source_and_spend(
    http: httpx.AsyncClient, queue: GenerationQueue, session_factory
) -> None:
    batch_id, task_ids = await _batch_in_three_states(queue)
    async with session_factory() as session:
        session.add_all(
            [
                ApiCall(
                    project_name=PROJECT,
                    call_type="image",
                    model="m",
                    provider="gemini",
                    status=CallStatus.SUCCESS,
                    started_at=NOW,
                    cost_amount=0.07,
                    currency="USD",
                    task_id=task_ids["E1S01"],
                ),
                ApiCall(
                    project_name=PROJECT,
                    call_type="image",
                    model="m",
                    provider="gemini",
                    status=CallStatus.PENDING,
                    started_at=NOW,
                    cost_amount=null(),
                    task_id=task_ids["E1S02"],
                ),
            ]
        )
        await session.commit()

    response = await http.get(f"/api/v1/batches/{batch_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["batch_id"] == batch_id
    assert body["project"] == PROJECT
    assert body["operation"] == "generate_storyboards"
    assert body["source"] == "director"
    assert {member["unit_id"]: (member["task_id"], member["status"]) for member in body["members"]} == {
        "E1S01": (task_ids["E1S01"], "succeeded"),
        "E1S02": (task_ids["E1S02"], "running"),
        "E1S03": (task_ids["E1S03"], "queued"),
    }
    assert body["counts"] == {
        "queued": 1,
        "running": 1,
        "succeeded": 1,
        "failed": 0,
        "cancelled": 0,
        "blocked": 0,
        "total": 3,
    }
    assert body["done"] is False
    assert body["poll_after_seconds"] is not None
    assert body["cost"] == {"calls": 1, "cost": {"USD": 0.07}, "unpriced_calls": 0, "pending_calls": 1}


async def test_cancel_preview_changes_nothing_and_cancel_cancels_only_queued_members(
    http: httpx.AsyncClient, queue: GenerationQueue
) -> None:
    batch_id, task_ids = await _batch_in_three_states(queue)
    expected = {
        "cancelled": [task_ids["E1S03"]],
        "skipped_running": [task_ids["E1S02"]],
        "skipped_terminal": [task_ids["E1S01"]],
    }

    preview = await http.get(f"/api/v1/batches/{batch_id}/cancel-preview")

    assert preview.status_code == 200
    assert preview.json() == expected
    queued = await queue.get_task(task_ids["E1S03"])
    assert queued is not None
    assert queued["status"] == "queued"

    cancelled = await http.post(f"/api/v1/batches/{batch_id}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json() == expected
    statuses = {unit: (await queue.get_task(task_id) or {}).get("status") for unit, task_id in task_ids.items()}
    assert statuses == {"E1S01": "succeeded", "E1S02": "running", "E1S03": "cancelled"}


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", ""), ("GET", "/cancel-preview"), ("POST", "/cancel")],
)
async def test_unknown_batch_is_404_with_its_code(http: httpx.AsyncClient, method: str, path: str) -> None:
    response = await http.request(method, f"/api/v1/batches/nope{path}")

    assert response.status_code == 404
    assert response.json()["code"] == "generation_batch_not_found"
    assert response.json()["params"] == {"id": "nope"}


async def test_list_tasks_filters_by_batch_membership(queue: GenerationQueue) -> None:
    batch_id, task_ids = await _batch_in_three_states(queue)
    await queue.enqueue_task(
        project_name=PROJECT, task_type="storyboard", media_type="image", resource_id="E9S01", source="webui"
    )

    listed = await queue.list_tasks(batch_id=batch_id)

    assert {item["task_id"] for item in listed["items"]} == set(task_ids.values())
    assert listed["total"] == 3


async def _write_episode_with_prompted_shot(session_factory, projects: ProjectManager) -> None:
    async with session_factory() as session:
        provider = await CustomProviderRepository(session).create_provider(
            display_name="Images",
            discovery_format="openai",
            base_url="https://api.example.com",
            api_key="test",
            models=[
                {
                    "model_id": "text-image",
                    "display_name": "Text image",
                    "endpoint": "openai-images-generations",
                    "entry_binding": "xai/grok-imagine-image-pro",
                },
            ],
        )
        provider_id = make_provider_id(provider.id)
        await session.commit()
    projects.update_project(
        PROJECT, lambda project: project.update({"image_provider_t2i": f"{provider_id}/text-image"})
    )
    scripts = projects.get_project_path(PROJECT) / "scripts"
    scripts.mkdir(exist_ok=True)
    segment = {
        "segment_id": "E1S01",
        "novel_text": "黄昏时分，风吹过村口。",
        "image_prompt": "村口黄昏",
        "video_prompt": {"action": "镜头平移", "camera_motion": "Pan", "ambiance_audio": "风声"},
        "duration_seconds": 4,
        "generated_assets": {},
    }
    script = {"episode": 1, "title": "第一集", "content_mode": "narration", "segments": [segment]}
    (scripts / "episode_1.json").write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    projects.add_episode(PROJECT, 1, "第一集", "scripts/episode_1.json")


async def test_x_client_of_a_batch_submission_labels_its_tasks_and_the_batch(
    http: httpx.AsyncClient, queue: GenerationQueue, session_factory, projects: ProjectManager
) -> None:
    await _write_episode_with_prompted_shot(session_factory, projects)

    submitted = await http.post(
        f"/api/v1/projects/{PROJECT}/episodes/1/storyboards/batch", headers={"X-Client": "flow"}
    )

    assert submitted.status_code == 200
    batch_id = submitted.json()["batch_id"]
    task = await queue.get_task(submitted.json()["task_ids_by_unit"]["E1S01"])
    assert task is not None
    assert task["source"] == "flow"
    read = await http.get(f"/api/v1/batches/{batch_id}")
    assert read.json()["source"] == "flow"


async def test_a_batch_submission_without_x_client_stays_webui(
    http: httpx.AsyncClient, queue: GenerationQueue, session_factory, projects: ProjectManager
) -> None:
    await _write_episode_with_prompted_shot(session_factory, projects)

    submitted = await http.post(f"/api/v1/projects/{PROJECT}/episodes/1/storyboards/batch")

    task = await queue.get_task(submitted.json()["task_ids_by_unit"]["E1S01"])
    assert task is not None
    assert task["source"] == "webui"
