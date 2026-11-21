"""Task lifecycle on the project event buses (G5): task_queued on the signal bus, task facts on terminal changes,
batch_queued coalescing and batch_done once per completed batch.

项目事件总线上的任务生命周期（G5）：信号总线上的 task_queued、终态变更上的任务事实、batch_queued 合并，以及每个
完成批次只发一次的 batch_done。
"""

from __future__ import annotations

import pytest

from lib.generation.generation_batch import GenerationBatchRequestedItem, GenerationBatchRequestSnapshot
from lib.generation.generation_queue import GenerationQueue
from lib.generation.generation_result import GenerationSelectionMode
from lib.generation.task_failure import encode_failure
from lib.generation.task_lifecycle_events import (
    batch_submission,
    build_task_lifecycle_change,
    coalesce_task_signals,
)
from lib.project.project_change_hints import (
    register_project_change_batch_listener,
    register_project_signal_listener,
)

PROJECT = "demo"


@pytest.fixture
async def queue(db_factory):
    return GenerationQueue(session_factory=db_factory)


@pytest.fixture
def signals():
    captured: list[tuple[str, list[dict]]] = []
    unregister = register_project_signal_listener(lambda name, changes: captured.append((name, list(changes))))
    yield captured
    unregister()


@pytest.fixture
def batches():
    captured: list[tuple[str, list[dict]]] = []
    unregister = register_project_change_batch_listener(
        lambda name, _source, changes: captured.append((name, [dict(c) for c in changes]))
    )
    yield captured
    unregister()


def _changes(captured, action: str) -> list[dict]:
    return [c for _, changes in captured for c in changes if c.get("action") == action]


async def _enqueue(queue: GenerationQueue, resource_id: str, **kwargs) -> dict:
    return await queue.enqueue_task(
        project_name=PROJECT,
        task_type="storyboard",
        media_type="image",
        resource_id=resource_id,
        payload={},
        script_file="episode_1.json",
        source=kwargs.pop("source", "director"),
        **kwargs,
    )


async def _batch(queue: GenerationQueue, units: tuple[str, ...]) -> str:
    return await queue.create_generation_batch(
        project_name=PROJECT,
        operation="generate_storyboards",
        requested=GenerationBatchRequestSnapshot(
            selection=GenerationSelectionMode.EXPLICIT,
            requested=[GenerationBatchRequestedItem(unit_id=unit) for unit in units],
        ),
        blocked=[],
        source="director",
    )


class TestTaskQueued:
    async def test_enqueue_emits_task_queued_with_task_facts(self, queue, signals, batches):
        task = await _enqueue(queue, "E1S01")

        queued = _changes(signals, "task_queued")
        assert len(queued) == 1
        change = queued[0]
        assert signals[0][0] == PROJECT
        assert change["entity_type"] == "task"
        assert change["entity_id"] == change["task_id"] == task["task_id"]
        assert change["status"] == "queued"
        assert change["task_type"] == "storyboard"
        assert change["resource_id"] == "E1S01"
        assert change["source"] == "director"
        assert change["script_file"] == "episode_1.json"
        assert change["queued_at"]
        assert change["important"] is False and change["focus"] is None
        # The signal bus, not the change-batch bus: nothing is re-read for a queued task.
        # 走信号总线而非变更批总线：任务入队不触发任何重读。
        assert batches == []

    async def test_deduplicated_enqueue_emits_nothing(self, queue, signals):
        await _enqueue(queue, "E1S01")
        signals.clear()

        again = await _enqueue(queue, "E1S01")

        assert again["deduped"] is True
        assert signals == []

    async def test_claim_alone_is_not_a_start(self, queue, signals):
        """task_started is announced by the worker at dispatch, not at claim (a claim may go straight back).

        task_started 由 worker 在派发时通告，而不是认领时（认领后可能立刻回队）。"""
        await _enqueue(queue, "E1S01")
        signals.clear()

        await queue.claim_next_task("image")

        assert _changes(signals, "task_started") == []

    def test_running_record_maps_to_task_started(self):
        change = build_task_lifecycle_change(
            {"task_id": "t1", "status": "running", "task_type": "video", "started_at": "2026-11-21T12:00:00Z"}
        )
        assert change is not None
        assert change["action"] == "task_started"
        assert change["started_at"] == "2026-11-21T12:00:00Z"
        assert build_task_lifecycle_change({"task_id": "t1", "status": "succeeded"}) is None


class TestTerminalFacts:
    async def test_failed_change_carries_error_code_and_batch(self, queue, batches):
        batch_id = await _batch(queue, ("E1S01", "E1S02"))
        task = await _enqueue(queue, "E1S01", batch_id=batch_id, batch_unit_id="E1S01")
        await queue.claim_next_task("image")

        reason = encode_failure("provider_unsupported_media", provider_id="p", media_type="image")
        await queue.mark_task_failed(task["task_id"], reason)

        failed = _changes(batches, "task_failed")
        assert len(failed) == 1
        assert failed[0]["error_code"] == "provider_unsupported_media"
        assert failed[0]["batch_id"] == batch_id
        assert failed[0]["status"] == "failed"
        assert failed[0]["finished_at"]

    async def test_cancelled_change_carries_cancelled_by(self, queue, batches):
        task = await _enqueue(queue, "E1S01")

        await queue.cancel_task(task["task_id"])

        cancelled = _changes(batches, "task_cancelled")
        assert cancelled[0]["cancelled_by"] == "user"


class TestBatchDone:
    async def test_batch_done_once_after_the_last_member(self, queue, batches):
        units = ("E1S01", "E1S02")
        batch_id = await _batch(queue, units)
        tasks = [await _enqueue(queue, unit, batch_id=batch_id, batch_unit_id=unit) for unit in units]
        await queue.claim_next_task("image")
        await queue.claim_next_task("image")

        await queue.mark_task_succeeded(tasks[0]["task_id"], {})
        assert _changes(batches, "batch_done") == []

        await queue.mark_task_failed(tasks[1]["task_id"], "boom")
        done = _changes(batches, "batch_done")
        assert len(done) == 1
        assert done[0]["entity_id"] == batch_id
        assert done[0]["operation"] == "generate_storyboards"
        assert done[0]["source"] == "director"
        assert done[0]["counts"] == {
            "queued": 0,
            "running": 0,
            "succeeded": 1,
            "failed": 1,
            "cancelled": 0,
            "blocked": 0,
            "total": 2,
        }
        # Same change batch as the terminal change that completed it, after it.
        # 与完成它的那条终态变更同批，且在其后。
        last = batches[-1][1]
        assert [c["action"] for c in last] == ["task_failed", "batch_done"]

        # A repeated read of the complete batch announces nothing more. / 已完成批次的再次读取不再通告。
        await queue.announce_batch_if_done(batch_id)
        assert len(_changes(batches, "batch_done")) == 1

    async def test_cancelling_the_queued_rest_completes_the_batch(self, queue, batches):
        units = ("E1S01", "E1S02", "E1S03")
        batch_id = await _batch(queue, units)
        for unit in units:
            await _enqueue(queue, unit, batch_id=batch_id, batch_unit_id=unit)

        result = await queue.cancel_generation_batch(project_name=PROJECT, batch_id=batch_id)

        assert len(result.cancelled) == 3
        done = _changes(batches, "batch_done")
        assert len(done) == 1
        assert done[0]["counts"]["cancelled"] == 3

    async def test_held_back_during_submission_then_announced(self, queue, batches):
        """A member finishing before the next one is enqueued must not complete the batch early.

        成员在下一个入队之前完成，不得让批次提前完成。"""
        units = ("E1S01", "E1S02")
        batch_id = await _batch(queue, units)
        with batch_submission(batch_id):
            first = await _enqueue(queue, "E1S01", batch_id=batch_id, batch_unit_id="E1S01")
            await queue.cancel_task(first["task_id"])
            assert _changes(batches, "batch_done") == []
            second = await _enqueue(queue, "E1S02", batch_id=batch_id, batch_unit_id="E1S02")
            await queue.cancel_task(second["task_id"])
            assert _changes(batches, "batch_done") == []

        await queue.announce_batch_if_done(batch_id)

        done = _changes(batches, "batch_done")
        assert len(done) == 1
        assert done[0]["counts"]["cancelled"] == 2

    async def test_tasks_outside_batches_send_no_batch_done(self, queue, batches):
        task = await _enqueue(queue, "E1S01")
        await queue.cancel_task(task["task_id"])

        assert _changes(batches, "batch_done") == []


class TestCoalesce:
    def test_batch_members_fold_into_one_batch_queued(self):
        def queued(task_id: str, batch_id: str | None, task_type: str = "storyboard", **extra) -> dict:
            return {
                "entity_type": "task",
                "action": "task_queued",
                "entity_id": task_id,
                "task_id": task_id,
                "batch_id": batch_id,
                "task_type": task_type,
                "source": "director",
                **extra,
            }

        changes = [
            queued("a", "b1"),
            queued("solo", None),
            queued("b", "b1", task_type="video"),
            {"entity_type": "task", "action": "task_started", "entity_id": "x", "batch_id": "b1"},
            queued("c", "b1", requeued=True),
        ]

        result = coalesce_task_signals(changes)

        assert [c["action"] for c in result] == ["batch_queued", "task_queued", "task_started", "task_queued"]
        folded = result[0]
        assert folded["entity_id"] == folded["batch_id"] == "b1"
        assert folded["count"] == 2
        assert folded["task_ids"] == ["a", "b"]
        assert folded["task_types"] == ["storyboard", "video"]
        assert folded["source"] == "director"
