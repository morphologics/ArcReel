"""Usage records carry the client they were made for, and filter by task, batch, episode and client.

使用记录带上所属的客户端，并可按任务、批次、集与客户端筛选。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import null, select

from lib.backends.providers import CallStatus
from lib.billing.call_client import call_client_scope
from lib.db.models.api_call import ApiCall
from lib.db.models.task import BatchTask, GenerationBatch, Task
from lib.db.repositories.usage_repo import UsageFilters, UsageRepository

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _task(task_id: str, *, source: str = "webui", script_file: str | None = None) -> Task:
    return Task(
        task_id=task_id,
        project_name="demo",
        task_type="storyboard",
        media_type="image",
        resource_id=task_id,
        script_file=script_file,
        status="succeeded",
        source=source,
        queued_at=NOW,
        updated_at=NOW,
    )


def _call(**overrides: object) -> ApiCall:
    fields: dict = {
        "project_name": "demo",
        "call_type": "image",
        "model": "m",
        "provider": "gemini",
        "status": CallStatus.SUCCESS,
        "started_at": NOW,
        "cost_amount": 0.0,
        "currency": "USD",
    }
    fields.update(overrides)
    return ApiCall(**fields)


async def _client_of(session, call_id: int) -> str | None:
    return (await session.execute(select(ApiCall.client).where(ApiCall.id == call_id))).scalar_one()


async def test_call_for_a_task_takes_the_task_source(async_session):
    async_session.add(_task("t-director", source="director"))
    await async_session.commit()
    repo = UsageRepository(async_session)

    with call_client_scope("webui"):
        call_id = await repo.start_call(
            project_name="demo", call_type="image", model="m", provider="gemini", task_id="t-director"
        )

    assert await _client_of(async_session, call_id) == "director"


async def test_taskless_call_takes_the_scope_client_and_is_empty_outside_any_scope(async_session):
    repo = UsageRepository(async_session)

    with call_client_scope("flow"):
        scoped = await repo.start_call(project_name="demo", call_type="text", model="m", provider="gemini")
    unscoped = await repo.start_call(project_name="demo", call_type="text", model="m", provider="gemini")

    assert await _client_of(async_session, scoped) == "flow"
    assert await _client_of(async_session, unscoped) is None


async def test_explicit_client_wins(async_session):
    async_session.add(_task("t-1", source="webui"))
    await async_session.commit()

    call_id = await UsageRepository(async_session).start_call(
        project_name="demo", call_type="image", model="m", provider="gemini", task_id="t-1", client="mcp"
    )

    assert await _client_of(async_session, call_id) == "mcp"


async def _seed_filter_rows(session) -> None:
    session.add(
        GenerationBatch(
            batch_id="b-1",
            project_name="demo",
            operation="generate_storyboards",
            requested_json="{}",
            blocked_json="[]",
            source="director",
            created_at=NOW,
        )
    )
    session.add_all(
        [
            _task("t-e1", source="director", script_file="episode_1.json"),
            _task("t-e2", source="webui", script_file="episode_2.json"),
            _task("t-sheet", source="webui"),
        ]
    )
    await session.flush()
    session.add(BatchTask(batch_id="b-1", task_id="t-e1", unit_id="E1S01"))
    session.add_all(
        [
            _call(task_id="t-e1", client="director", cost_amount=0.07, segment_id="e1-paid"),
            _call(task_id="t-e1", client="director", cost_amount=null(), segment_id="e1-unpriced"),
            _call(task_id="t-e1", client="director", status=CallStatus.PENDING, segment_id="e1-pending"),
            _call(task_id="t-e2", client="webui", cost_amount=0.5, segment_id="e2"),
            _call(task_id="t-sheet", client="webui", cost_amount=0.2, segment_id="sheet"),
            _call(client=None, call_type="text", segment_id="old"),
        ]
    )
    await session.commit()


async def _segments(repo: UsageRepository, filters: UsageFilters) -> set[str]:
    page = await repo.list_records(filters=filters, limit=50)
    return {item["segment_id"] for item in page["items"]}


async def test_record_filters_by_task_batch_episode_and_client(async_session):
    await _seed_filter_rows(async_session)
    repo = UsageRepository(async_session)

    assert await _segments(repo, UsageFilters(task_ids=("t-e2", "t-sheet"))) == {"e2", "sheet"}
    # Pending rows with a task stay hidden from the list (the task represents them), as for every other filter.
    # 有任务的 pending 行照旧不出现在列表里（由任务代表），与其余筛选一致。
    assert await _segments(repo, UsageFilters(batch_id="b-1")) == {"e1-paid", "e1-unpriced"}
    assert await _segments(repo, UsageFilters(episode=2)) == {"e2"}
    assert await _segments(repo, UsageFilters(clients=("director",))) == {"e1-paid", "e1-unpriced"}
    assert await _segments(repo, UsageFilters(clients=("webui",), episode=1)) == set()


async def test_record_projection_exposes_the_client(async_session):
    await _seed_filter_rows(async_session)

    page = await UsageRepository(async_session).list_records(filters=UsageFilters(task_ids=("t-e2",)))

    assert [item["client"] for item in page["items"]] == ["webui"]


async def test_summary_rows_follow_the_same_filters(async_session):
    await _seed_filter_rows(async_session)

    rows = await UsageRepository(async_session).fetch_summary_rows(filters=UsageFilters(clients=("webui",)))

    assert sorted(row.segment_id for row in rows) == ["e2", "sheet"]


async def test_batch_cost_sums_priced_terminal_calls_and_counts_the_rest(async_session):
    await _seed_filter_rows(async_session)

    cost = await UsageRepository(async_session).get_batch_cost("b-1")

    assert cost == {"calls": 2, "cost": {"USD": 0.07}, "unpriced_calls": 1, "pending_calls": 1}
