"""Task signals on the project event stream (G5): coalesced into one ``changes`` message without re-reading the
project, batch members folded into ``batch_queued``, and flushed before a later terminal change.

项目事件流上的任务信号（G5）：合并成一条 ``changes`` 消息且不重读项目，批次成员合并为 ``batch_queued``，并在之后的
终态变更之前先发出。
"""

from __future__ import annotations

import asyncio

from lib.project.project_change_hints import emit_project_change_batch, emit_project_signal_batch
from lib.project.project_manager import ProjectManager
from server.services.project.project_events import ProjectEventService, read_project_state


async def _next_event(stream, *, timeout: float) -> tuple[str, dict]:  # noqa: ASYNC109 -- test polling limit / 测试轮询上限
    async def _pull() -> tuple[str, dict]:
        async for item in stream:
            if isinstance(item, dict):
                continue
            return item
        raise AssertionError("stream ended")

    return await asyncio.wait_for(_pull(), timeout=timeout)


def _signal(task_id: str, action: str = "task_queued", batch_id: str | None = None) -> dict:
    return {
        "entity_type": "task",
        "action": action,
        "entity_id": task_id,
        "task_id": task_id,
        "label": task_id,
        "focus": None,
        "important": False,
        "task_type": "storyboard",
        "batch_id": batch_id,
        "status": "queued" if action == "task_queued" else "running",
    }


def _project(tmp_path) -> ProjectManager:
    pm = ProjectManager(tmp_path / "projects")
    pm.create_project("demo")
    pm.create_project_metadata("demo", "Demo", "Anime", "narration")
    return pm


async def test_signals_are_coalesced_into_one_message_without_rereading(tmp_path):
    pm = _project(tmp_path)
    reads: list[str] = []

    def reader(name: str):
        reads.append(name)
        return read_project_state(pm, name)

    service = ProjectEventService(tmp_path, poll_interval=30.0, read_state=reader)
    await service.start()
    try:
        async with service.stream_events("demo", idle_timeout=0.05) as stream:
            event_name, snapshot = await anext(stream)
            assert event_name == "snapshot"
            reads_before = len(reads)

            for index in range(5):
                emit_project_signal_batch("demo", [_signal(f"m{index}", batch_id="b1")])
            emit_project_signal_batch("demo", [_signal("solo", action="task_started")])

            event_name, payload = await _next_event(stream, timeout=2.0)
            assert event_name == "changes"
            assert payload["fingerprint"] == snapshot["fingerprint"]
            actions = [change["action"] for change in payload["changes"]]
            assert actions == ["batch_queued", "task_started"]
            assert payload["changes"][0]["count"] == 5
            assert payload["changes"][0]["task_ids"] == [f"m{index}" for index in range(5)]
            assert len(reads) == reads_before
    finally:
        await service.shutdown()


async def test_pending_signals_go_out_before_a_terminal_change(tmp_path):
    _project(tmp_path)
    service = ProjectEventService(tmp_path, poll_interval=30.0)
    await service.start()
    try:
        async with service.stream_events("demo", idle_timeout=0.05) as stream:
            await anext(stream)

            emit_project_signal_batch("demo", [_signal("t1", action="task_started")])
            terminal = {**_signal("t1"), "action": "task_succeeded", "status": "succeeded"}
            emit_project_change_batch("demo", [terminal], source="worker")

            _name, first = await _next_event(stream, timeout=2.0)
            _name, second = await _next_event(stream, timeout=2.0)
            assert [c["action"] for c in first["changes"]] == ["task_started"]
            assert [c["action"] for c in second["changes"]] == ["task_succeeded"]
    finally:
        await service.shutdown()


async def test_signals_without_subscribers_are_dropped(tmp_path):
    _project(tmp_path)
    service = ProjectEventService(tmp_path, poll_interval=30.0)
    await service.start()
    try:
        emit_project_signal_batch("demo", [_signal("t1")])
        await asyncio.sleep(0)
        assert "demo" not in service._channels  # noqa: SLF001 -- no channel is created for a signal / 信号不建通道
    finally:
        await service.shutdown()
