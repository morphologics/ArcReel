import asyncio
import contextlib
import json
import logging
import threading

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lib.artifacts.artifact_activation import register_current_artifact, register_current_resource_artifact
from lib.artifacts.artifact_manifest import ArtifactKey
from lib.artifacts.version_manager import MANUAL_UPLOAD_VERSION_SOURCE, VersionManager
from lib.edit_timeline import EditTimelineService
from lib.generation.generation_queue import GenerationQueue
from lib.project.project_change_hints import (
    emit_project_change_batch,
    emit_project_change_hint,
    project_change_source,
)
from lib.project.project_manager import ProjectManager
from lib.script.draft_quarantine import QUARANTINE_KIND_NARRATION_SCRIPT_PLAN, quarantine_path
from lib.workflow.workflow_plan import WorkflowPlanRequest
from lib.workflow.workflow_state import WorkflowStateService, workflow_finished
from server.agent_toolset.edit_timelines import CREATE_TIMELINE
from server.dependencies import require_project_migration_ok
from server.routers import edit_timelines
from server.services.project.project_events import (
    ASSISTANT_SESSION_RESUMED_EVENT,
    PROJECT_DELETED_EVENT,
    ProjectEventService,
    read_project_state,
)
from server.services.project.project_state_projection import ProjectState
from server.services.project.workflow_planner import WorkflowPlanner
from tests.auth_deps import override_auth
from tests.factories import make_test_video
from tests.integration.server.agent_tool_support import ToolHarness, run_declared_tool


class _ControlledReader:
    """Test adapter for the read_state seam: delegates to the real read-only load; once ``hook`` is set, every disk
    read is let through by it.

    read_state seam 的测试 adapter：委托真实只读加载；设了 ``hook`` 后每次读盘经它放行。"""

    def __init__(self, pm: ProjectManager):
        self._pm = pm
        self.hook = None

    def __call__(self, project_name: str) -> ProjectState:
        def read() -> ProjectState:
            return read_project_state(self._pm, project_name)

        return read() if self.hook is None else self.hook(read)


def _pending_assets() -> dict:
    return {
        "storyboard_image": None,
        "video_clip": None,
        "video_uri": None,
        "status": "pending",
    }


def _terminal_task_change(task_id: str) -> dict:
    """Task terminal-state event: triggers an explicit rebuild and describes no file-side entity change.

    任务终态事件：触发显式重建，且不描述任何文件侧实体变更。"""
    return {
        "entity_type": "task",
        "action": "task_succeeded",
        "entity_id": task_id,
        "label": task_id,
        "focus": None,
        "important": False,
        "task_type": "video",
    }


async def _next_event(stream, *, timeout: float) -> tuple[str, dict]:  # noqa: ASYNC109 -- Wait limit of a test polling helper, not production cancellation semantics / 测试轮询 helper 的等待上限，非生产取消语义
    """Pull the next real (event_name, payload) tuple, skipping ``_idle`` sentinels."""

    async def _pull() -> tuple[str, dict]:
        async for item in stream:
            if isinstance(item, dict):
                if item.get("type") == "_idle":
                    continue
                raise AssertionError(f"unexpected dict sentinel: {item}")
            return item
        raise AssertionError("stream ended before a real event arrived")

    return await asyncio.wait_for(_pull(), timeout=timeout)


@pytest.mark.parametrize("entry", ["agent", "http"])
async def test_timeline_creation_refreshes_project_events_and_completes_workflow(tmp_path, db_factory, entry):
    pm = ProjectManager(tmp_path / "projects")
    pm.create_project("demo")
    pm.create_project_metadata(
        "demo", "Demo", "", "ad", target_duration=4, extras={"generation_mode": "reference_video"}
    )
    project_path = pm.get_project_path("demo")
    pm.save_script(
        "demo",
        {
            "episode": 1,
            "title": "广告",
            "content_mode": "ad",
            "generation_mode": "reference_video",
            "video_units": [
                {
                    "unit_id": "E1U01",
                    "text": "镜头",
                    "duration_seconds": 4,
                    "generated_assets": {"video_clip": "reference_videos/E1U01.mp4"},
                }
            ],
        },
        "episode_1.json",
    )
    register_current_artifact(project_path, ArtifactKey.episode_script(1))
    staged = tmp_path / "video.mp4"
    make_test_video(staged, duration_sec=1)
    VersionManager(project_path).commit_staged_version(
        "reference_videos",
        "E1U01",
        "",
        staged_file=staged,
        current_file=project_path / "reference_videos/E1U01.mp4",
        source=MANUAL_UPLOAD_VERSION_SOURCE,
    )
    register_current_resource_artifact(
        project_path, resource_type="reference_videos", resource_id="E1U01", script_file="scripts/episode_1.json"
    )
    queue = GenerationQueue(session_factory=db_factory, project_manager=pm)
    planner = WorkflowPlanner(pm)
    request = WorkflowPlanRequest(episode_id=1)
    before = await planner.get_plan("demo", request, queue=queue)
    assert before.next_action.type == "create_edit_timeline"
    assert before.steps[-1].state == "ready"

    service = ProjectEventService(tmp_path, poll_interval=30)
    await service.start()
    try:
        async with service.stream_events("demo") as stream:
            assert (await anext(stream))[0] == "snapshot"
            # The Web entry's source is marked by the route itself; test cases do not wrap it for it.
            # Web 入口的来源由路由自己标记，用例不替它套上。
            with project_change_source("worker") if entry == "agent" else contextlib.nullcontext():
                if entry == "agent":
                    created = await run_declared_tool(
                        CREATE_TIMELINE,
                        ToolHarness("demo", tmp_path / "projects", pm, queue=queue),
                        {"from": "script", "episode": 1, "name": "完整版"},
                    )
                    assert created.problem is None
                else:
                    app = FastAPI()
                    override_auth(app)
                    app.dependency_overrides[require_project_migration_ok] = lambda: None
                    app.dependency_overrides[edit_timelines.get_edit_timeline_service] = lambda: EditTimelineService(pm)
                    app.dependency_overrides[edit_timelines.get_workflow_state_service] = lambda: WorkflowStateService(
                        pm
                    )
                    app.include_router(edit_timelines.router)
                    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                        response = await client.post(
                            "/projects/demo/episodes/1/edit-timelines", json={"from": "script", "name": "完整版"}
                        )
                    assert response.status_code == 201
            event_name, payload = await _next_event(stream, timeout=1)
            assert event_name == "changes"
            assert payload["source"] == ("worker" if entry == "agent" else "webui")
            assert payload["changes"][0]["entity_type"] == "episode"
            assert payload["changes"][0]["episode"] == 1
            after = await planner.get_plan("demo", request, queue=queue)
            assert after.next_action.type == "none"
            assert workflow_finished(after.status)
            assert after.steps[-1].state == "completed"
    finally:
        await service.shutdown()


class TestProjectEventService:
    @pytest.mark.asyncio
    async def test_emitted_batch_is_broadcast_without_waiting_for_snapshot_diff(self, tmp_path):
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        service = ProjectEventService(tmp_path, poll_interval=1.0)
        await service.start()

        async with service.stream_events("demo", idle_timeout=0.1) as stream:
            event_name, snapshot = await anext(stream)
            assert event_name == "snapshot"
            assert snapshot["fingerprint"]

            emit_project_change_batch(
                "demo",
                [
                    {
                        "entity_type": "segment",
                        "action": "storyboard_ready",
                        "entity_id": "E1S01",
                        "label": "分镜「E1S01」",
                        "focus": None,
                        "important": True,
                    }
                ],
                source="worker",
            )

            event_name, payload = await _next_event(stream, timeout=1.0)
            assert event_name == "changes"
            assert payload["source"] == "worker"
            assert payload["fingerprint"] == snapshot["fingerprint"]
            assert payload["changes"][0]["action"] == "storyboard_ready"

        await service.shutdown()

    async def test_assistant_session_resumed_is_broadcast_to_project_subscribers(self, tmp_path):
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        service = ProjectEventService(tmp_path, poll_interval=1.0)
        await service.start()
        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, _snapshot = await anext(stream)
                assert event_name == "snapshot"

                service.publish_assistant_session_resumed("demo", "sess-1")

                event_name, payload = await _next_event(stream, timeout=1.0)
                assert event_name == ASSISTANT_SESSION_RESUMED_EVENT
                assert payload == {"project_name": "demo", "session_id": "sess-1", "status": "running"}
        finally:
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_stale_rebuild_does_not_broadcast_reverse_diff(self, tmp_path):
        """When two explicit rebuilds with different disk-read times settle out of order, the one that read the older
        disk does not broadcast a reverse change.

        The follow-up sweep diffs against the baseline, so writing a stale snapshot back to the baseline would diff
        entities that actually exist into ``deleted``. The rebuild lock chains "read disk -> settle baseline ->
        broadcast" into one read-modify-write; the later one waits until the earlier one has settled before
        reading the disk, so what it reads is necessarily newer.

        两次显式重建读盘耗时不同而乱序结算时，先读到旧盘的一方不广播反向变更。

        补扫对基线做 diff，故陈旧快照被写回基线会把实际存在的实体 diff 成 ``deleted``。
        重建锁把「读盘 → 结算基线 → 广播」串成一段读-改-写，后发起的一方等到前一方
        结算完才读盘，读到的必然更新。
        """
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        reader = _ControlledReader(pm)
        service = ProjectEventService(tmp_path, poll_interval=30.0, read_state=reader)
        await service.start()

        release_first = asyncio.Event()

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, _snapshot = await anext(stream)
                assert event_name == "snapshot"

                calls = 0
                first_read_done = asyncio.Event()
                second_read_done = asyncio.Event()
                loop = asyncio.get_running_loop()

                def _read_holding_first(read):
                    nonlocal calls
                    calls += 1
                    index = calls
                    result = read()
                    if index == 1:
                        # First disk read done but not yet settled: yield control and wait for the orchestration to release it
                        # 首次读盘已完成但尚未结算：交还控制权，等编排放行
                        loop.call_soon_threadsafe(first_read_done.set)
                        asyncio.run_coroutine_threadsafe(release_first.wait(), loop).result(timeout=5)
                    else:
                        loop.call_soon_threadsafe(second_read_done.set)
                    return result

                reader.hook = _read_holding_first

                emit_project_change_batch("demo", [_terminal_task_change("task-1")], source="worker")
                await asyncio.wait_for(first_read_done.wait(), timeout=5.0)

                # After the first disk read, write a new character to disk: rewrite project.json directly, no hint
                # 首次读盘之后落盘一个新角色：直接改写 project.json，不发 hint
                project_json = pm.get_project_path("demo") / "project.json"
                data = json.loads(project_json.read_text(encoding="utf-8"))
                data.setdefault("characters", {})["新角色"] = {"name": "新角色", "description": "d"}
                project_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

                # Second rebuild: without the lock it could read the new disk meanwhile and settle first; wait for its
                # read-done signal instead of a fixed sleep (if scheduling is slower than the sleep the second rebuild has not
                # started yet, and removing the lock would not reveal a regression).
                # With the lock it is held outside, so the signal can never arrive; the timeout itself is then the evidence
                # the lock works.
                # 第二次重建：无锁时它能在此期间读到新盘并抢先结算，等它的读盘完成信号，
                # 不用固定 sleep（调度慢于 sleep 时第二次重建还没开始，摘掉锁也测不出回归）。
                # 有锁时它被挡在锁外、信号必然等不到，这时超时本身就是锁生效的证据。
                emit_project_change_batch("demo", [_terminal_task_change("task-2")], source="worker")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(second_read_done.wait(), timeout=1.0)
                release_first.set()

                # Collect both rebuilds' batch broadcasts and any sweep broadcasts before looking at the full picture of the
                # character change
                # 两次重建各自的批次广播与可能的补扫广播都要收齐，再看角色变更的全貌
                broadcast_changes = []
                for _ in range(2):
                    event_name, payload = await _next_event(stream, timeout=5.0)
                    assert event_name == "changes"
                    broadcast_changes.extend(payload["changes"])
                while True:
                    try:
                        event_name, payload = await _next_event(stream, timeout=0.5)
                    except TimeoutError:
                        break
                    assert event_name == "changes"
                    broadcast_changes.extend(payload["changes"])

                character_changes = [change for change in broadcast_changes if change["entity_type"] == "character"]
                assert [change["action"] for change in character_changes] == ["created"]
        finally:
            # If an assertion or _next_event fails early, the deliberately suspended rebuild thread is still stuck on
            # release_first; only after releasing it can shutdown reap the watch task and the global listener
            # registration of project_change_hints.
            # 断言或 _next_event 提前失败时，被刻意挂起的重建线程仍卡在 release_first 上，
            # 放行后 shutdown 才收得掉 watch task 与 project_change_hints 的全局监听注册。
            release_first.set()
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_emitted_batch_broadcasts_concurrent_change_swept_into_rebuild(self, tmp_path):
        """When an explicit rebuild's disk read picks up a file change not belonging to this batch, that change is
        broadcast by the follow-up sweep right after.

        An explicit rebuild writes the new snapshot back to the baseline. If a change landed on disk before the read
        but is not among this batch's changes, it is merged into the baseline without ever being broadcast - later
        poll scans see no difference from the baseline, and no mechanism will send it again. Here a terminal event
        unrelated to that change triggers the rebuild to cover this path.

        The sweep uses its own payload: source is a payload-level field, and the piggybacked change's source may
        differ from this batch's; merging it into this batch would put the frontend's webui suppression decision on
        the wrong side.

        The concurrent change is "rewrite an already indexed script file in place": such a change does not trigger
        the episode index sync, so no hint is sent to wake the poll scan, and the loss is final.

        显式重建读盘捎带了不属于本批的文件变更时，该变更由紧随其后的补扫广播发出。

        显式重建把新快照写回基线。若某个变更落盘于读盘之前却不在本批 changes 里，
        它会被并入基线而从未广播——后续轮询扫描与基线已无差异，再没有任何机制
        为它补发。这里用「与该变更无关的终态事件」触发重建来覆盖这条路径。

        补扫走独立 payload：source 是 payload 级字段，捎带进来的变更来源与本批未必
        相同，并入本批会让前端的 webui 抑制判断落到错误的一侧。

        并发变更取「原地改写已索引的剧本文件」：这类变更不触发剧集索引同步，
        因而不会顺带发出 hint 唤醒轮询扫描，丢失是终局的。
        """
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        script = {
            "episode": 1,
            "title": "第一集",
            "content_mode": "narration",
            "segments": [
                {
                    "segment_id": "E1S01",
                    "narration_text": "开场",
                    "image_prompt": "p",
                    "video_prompt": "v",
                    "characters_in_scene": [],
                    "scenes": [],
                    "props": [],
                    "generated_assets": _pending_assets(),
                }
            ],
        }
        with project_change_source("filesystem"):
            pm.save_script("demo", script, "episode_1.json", validate=False)

        # poll_interval far exceeds the test duration: isolate the explicit rebuild path and rule out a poll scan
        # broadcasting the change first
        # poll_interval 远超用例时长：隔离显式重建路径，排除轮询扫描抢先广播该变更
        service = ProjectEventService(tmp_path, poll_interval=30.0)
        await service.start()

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, _snapshot = await anext(stream)
                assert event_name == "snapshot"

                # Write to disk directly, no hint: simulates the change reaching disk before this rebuild's read
                # 直接落盘、不发 hint：模拟变更早于本次重建读盘抵达磁盘
                script["segments"][0]["generated_assets"]["storyboard_image"] = "storyboards/E1S01.png"
                script_path = pm.get_project_path("demo") / "scripts" / "episode_1.json"
                script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")

                emit_project_change_batch("demo", [_terminal_task_change("task-1")], source="worker")

                # This batch's own terminal event has a payload to itself (timing and structure of the normal path are
                # unaffected by the sweep)
                # 本批自身的终态事件独占一条 payload（正常路径的时序与结构不受补扫影响）
                event_name, payload = await _next_event(stream, timeout=2.0)
                assert event_name == "changes"
                assert [change["action"] for change in payload["changes"]] == ["task_succeeded"]
                assert payload["source"] == "worker"

                # The piggybacked change is then broadcast separately, its source resolved from its own hint marker
                # 捎带进来的变更随后单独广播，来源按自己的 hint 标记解析
                event_name, swept = await _next_event(stream, timeout=2.0)
                assert event_name == "changes"
                assert any(
                    change["action"] == "storyboard_ready" and change["entity_id"] == "E1S01"
                    for change in swept["changes"]
                )
                assert swept["source"] == "filesystem"
        finally:
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_emitted_batch_does_not_duplicate_change_it_already_describes(self, tmp_path):
        """When the asset is written to disk before this batch's event is sent (the real order of the worker success
        path), the sweep does not broadcast the same change twice.

        资产已落盘再发本批事件（worker 成功路径的真实次序）时，补扫不重复广播同一条变更。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        script = {
            "episode": 1,
            "title": "第一集",
            "content_mode": "narration",
            "segments": [
                {
                    "segment_id": "E1S01",
                    "narration_text": "开场",
                    "image_prompt": "p",
                    "video_prompt": "v",
                    "characters_in_scene": [],
                    "scenes": [],
                    "props": [],
                    "generated_assets": _pending_assets(),
                }
            ],
        }
        with project_change_source("filesystem"):
            pm.save_script("demo", script, "episode_1.json", validate=False)

        service = ProjectEventService(tmp_path, poll_interval=30.0)
        await service.start()

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, _snapshot = await anext(stream)
                assert event_name == "snapshot"

                # Worker order: write the asset to disk first, then send the matching completion event
                # worker 次序：先落盘资产，再发对应的完成事件
                script["segments"][0]["generated_assets"]["storyboard_image"] = "storyboards/E1S01.png"
                script_path = pm.get_project_path("demo") / "scripts" / "episode_1.json"
                script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")

                emit_project_change_batch(
                    "demo",
                    [
                        {
                            "entity_type": "segment",
                            "action": "storyboard_ready",
                            "entity_id": "E1S01",
                            "label": "分镜「E1S01」",
                            "focus": None,
                            "important": True,
                            "script_file": "episode_1.json",
                            "episode": 1,
                        }
                    ],
                    source="worker",
                )

                # Drain all payloads before counting: the sweep uses its own payload, so on a dedup regression the duplicate
                # lands in the next payload; looking only at the first would make the guard meaningless
                # 排空所有 payload 再统计：补扫走独立 payload，去重回归时重复的那条会落在
                # 紧随其后的另一条 payload 里，只看第一条的话护栏形同虚设
                broadcast_changes = []
                event_name, payload = await _next_event(stream, timeout=2.0)
                assert event_name == "changes"
                broadcast_changes.extend(payload["changes"])
                while True:
                    try:
                        event_name, payload = await _next_event(stream, timeout=0.5)
                    except TimeoutError:
                        break
                    assert event_name == "changes"
                    broadcast_changes.extend(payload["changes"])

                ready = [
                    change
                    for change in broadcast_changes
                    if change["action"] == "storyboard_ready" and change["entity_id"] == "E1S01"
                ]
                assert len(ready) == 1
        finally:
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_reference_video_completion_is_broadcast_once(self, tmp_path):
        """Reference-to-video completion is called ``reference_video_ready`` by the publisher and ``video_ready`` in the
        snapshot diff; the sweep yields to this batch as the same event, broadcasting only once.

        参考生视频完成在发布方叫 ``reference_video_ready``、在快照差分里是 ``video_ready``，
        补扫按同一件事让给本批，只广播一次。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration", extras={"generation_mode": "reference_video"})

        script = {
            "episode": 1,
            "title": "第一集",
            "content_mode": "narration",
            "generation_mode": "reference_video",
            "video_units": [
                {"unit_id": "E1U01", "duration_seconds": 8, "text": "开场", "generated_assets": _pending_assets()}
            ],
        }
        with project_change_source("filesystem"):
            pm.save_script("demo", script, "episode_1.json", validate=False)

        service = ProjectEventService(tmp_path, poll_interval=30.0)
        await service.start()

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, _snapshot = await anext(stream)
                assert event_name == "snapshot"

                script["video_units"][0]["generated_assets"]["video_clip"] = "videos/E1U01.mp4"
                script_path = pm.get_project_path("demo") / "scripts" / "episode_1.json"
                script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
                emit_project_change_batch(
                    "demo",
                    [
                        {
                            "entity_type": "reference_unit",
                            "action": "reference_video_ready",
                            "entity_id": "E1U01",
                            "label": "视频单元「E1U01」",
                            "focus": None,
                            "important": True,
                        }
                    ],
                    source="worker",
                )

                broadcast_changes = []
                while True:
                    try:
                        event_name, payload = await _next_event(stream, timeout=0.5)
                    except TimeoutError:
                        break
                    assert event_name == "changes"
                    broadcast_changes.extend(payload["changes"])

                assert [change["action"] for change in broadcast_changes if change["entity_id"] == "E1U01"] == [
                    "reference_video_ready"
                ]
        finally:
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_project_manager_writes_outside_the_old_snapshot_fields_are_broadcast(self, tmp_path):
        """Adding a product or changing a dialogue line via the project manager: subscribers all receive the
        corresponding project events.

        经项目管理器新增商品、改台词，订阅者都收到对应的项目事件。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "drama")
        scene = {
            "scene_id": "E1S01",
            "duration_seconds": 8,
            "utterances": [{"kind": "dialogue", "speaker": "甲", "text": "旧台词"}],
            "generated_assets": _pending_assets(),
        }
        with project_change_source("filesystem"):
            pm.save_script(
                "demo",
                {"episode": 1, "title": "第一集", "content_mode": "drama", "scenes": [scene]},
                "episode_1.json",
                validate=False,
            )

        service = ProjectEventService(tmp_path, poll_interval=30.0)
        await service.start()

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, _snapshot = await anext(stream)
                assert event_name == "snapshot"

                pm.upsert_assets("demo", "products", {"咖啡": {"description": "冷萃"}})
                with pm.locked_script("demo", "episode_1.json", validate=False) as script:
                    script["scenes"][0]["utterances"][0]["text"] = "新台词"

                expected = {("product", "created", "咖啡"), ("drama_scene", "updated", "E1S01")}
                seen: set[tuple] = set()
                while not expected <= seen:
                    event_name, payload = await _next_event(stream, timeout=2.0)
                    assert event_name == "changes"
                    seen |= {(c["entity_type"], c["action"], c["entity_id"]) for c in payload["changes"]}
        finally:
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_data_root_kwarg_overrides_default_subdir(self, tmp_path):
        """When data_root is passed explicitly (production entries pass the configured data root), the event stream
        reads projects under that data root.

        显式传 data_root 时（生产入口传配置的数据根），事件流读的是该数据根下的项目。"""
        custom_projects = tmp_path / "external-data"
        pm = ProjectManager(custom_projects)
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        service = ProjectEventService(tmp_path, data_root=custom_projects, poll_interval=30.0)
        await service.start()

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, snapshot = await anext(stream)
                assert event_name == "snapshot"
                assert snapshot["fingerprint"]
        finally:
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_sweep_leaves_change_described_by_another_inflight_batch(self, tmp_path):
        """When another batch's artifact is picked up by this rebuild's disk read, the sweep leaves it to that batch to
        broadcast; the same event is sent only once.

        When two generation tasks complete almost simultaneously, whoever takes the rebuild lock first reads the
        other's already written artifact. If the sweep broadcast it too, the other's own queued batch would still
        send it again unconditionally; the frontend does not dedupe across batches, so the user would see two
        completions.

        另一批次的产物被本次重建读盘捎带时，补扫让给该批次广播，同一件事只发一次。

        两个生成任务几乎同时完成时，先取得重建锁的一方会读到对方已落盘的产物。补扫若把它
        一并广播，对方自己排队的批次仍会无条件再发一次，前端不跨批去重，用户看到两次完成。
        """
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        script = {
            "episode": 1,
            "title": "第一集",
            "content_mode": "narration",
            "segments": [
                {
                    "segment_id": "E1S01",
                    "narration_text": "开场",
                    "image_prompt": "p",
                    "video_prompt": "v",
                    "characters_in_scene": [],
                    "scenes": [],
                    "props": [],
                    "generated_assets": _pending_assets(),
                }
            ],
        }
        with project_change_source("filesystem"):
            pm.save_script("demo", script, "episode_1.json", validate=False)

        reader = _ControlledReader(pm)
        service = ProjectEventService(tmp_path, poll_interval=30.0, read_state=reader)
        await service.start()

        release_first = asyncio.Event()

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, _snapshot = await anext(stream)
                assert event_name == "snapshot"

                calls = 0
                first_read_done = asyncio.Event()
                loop = asyncio.get_running_loop()

                def _read_holding_first(read):
                    nonlocal calls
                    calls += 1
                    index = calls
                    if index == 1:
                        # Pause once before reading the disk: let the orchestration write B's artifact and send B's batch first,
                        # so this disk read necessarily picks up B's artifact
                        # 读盘前停一次：让编排先把 B 的产物落盘并发出 B 的批次，
                        # 本次读盘因而必然捎带 B 的产物
                        loop.call_soon_threadsafe(first_read_done.set)
                        asyncio.run_coroutine_threadsafe(release_first.wait(), loop).result(timeout=5)
                    return read()

                reader.hook = _read_holding_first

                # Batch A: a terminal event unrelated to shots, only to trigger the rebuild
                # 批次 A：与分镜无关的终态事件，只为触发重建
                emit_project_change_batch("demo", [_terminal_task_change("task-a")], source="worker")
                await asyncio.wait_for(first_read_done.wait(), timeout=5.0)

                # Batch B's artifact is written first, then B's own batch is sent (the real order of the worker success path)
                # 批次 B 的产物先落盘，再发 B 自己的批次（worker 成功路径的真实次序）
                script["segments"][0]["generated_assets"]["storyboard_image"] = "storyboards/E1S01.png"
                script_path = pm.get_project_path("demo") / "scripts" / "episode_1.json"
                script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
                emit_project_change_batch(
                    "demo",
                    [
                        {
                            "entity_type": "segment",
                            "action": "storyboard_ready",
                            "entity_id": "E1S01",
                            "label": "分镜「E1S01」",
                            "focus": None,
                            "important": True,
                            "script_file": "episode_1.json",
                            "episode": 1,
                        }
                    ],
                    source="worker",
                )
                release_first.set()

                broadcast_changes = []
                while True:
                    try:
                        event_name, payload = await _next_event(stream, timeout=1.0)
                    except TimeoutError:
                        break
                    assert event_name == "changes"
                    broadcast_changes.extend(payload["changes"])

                ready = [
                    change
                    for change in broadcast_changes
                    if change["action"] == "storyboard_ready" and change["entity_id"] == "E1S01"
                ]
                assert len(ready) == 1
                # It is broadcast by B's own batch: batch B explicitly passes focus=None; the sweep copy's focus is built from
                # the diff
                # 广播它的是 B 自己那一批：B 批显式传 focus=None，补扫副本的 focus 由差分构造
                assert ready[0]["focus"] is None
        finally:
            release_first.set()
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_hint_of_a_write_the_scan_read_missed_settles_the_next_scan(self, tmp_path):
        """The later step of a multi-step Web write lands after the scan's disk read, and its hint arrives before this
        round settles: the next scan reads it and still broadcasts it as webui.

        The earlier step's hint wakes the scan; after the scan has read the disk but before it settles, the later
        step writes and sends a hint. This scan did not read the later step, so its source marker must be kept for
        the next round and not cleared with this round's settling, otherwise the change read next round would be
        marked filesystem.

        多步 Web 写入的后一步落在扫描读盘之后、hint 在这轮结算前送达：下一轮扫描读到它，仍按 webui 广播。

        前一步的 hint 唤醒扫描；扫描读完盘、尚未结算时，后一步写盘并发出 hint。这轮扫描没读到后一步，
        它的来源标记要留给下一轮，不能随这轮结算一并清掉，否则下一轮读到的变更会被标成 filesystem。
        """
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")
        reader = _ControlledReader(pm)
        service = ProjectEventService(tmp_path, poll_interval=30.0, read_state=reader)
        await service.start()
        loop = asyncio.get_running_loop()
        read_done = asyncio.Event()
        release = threading.Event()

        def _read_then_hold(read):
            reader.hook = None
            state = read()
            loop.call_soon_threadsafe(read_done.set)
            assert release.wait(timeout=5)
            return state

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                assert (await anext(stream))[0] == "snapshot"
                reader.hook = _read_then_hold

                emit_project_change_hint("demo", source="webui")
                await asyncio.wait_for(read_done.wait(), timeout=5)
                # The write thread posts the hint to the event loop before returning, so waiting for it to return guarantees
                # the hint arrives before this round settles
                # 写盘线程返回前已把 hint 投给事件循环，等它返回即保证 hint 先于这轮结算送达
                with project_change_source("webui"):
                    await asyncio.to_thread(pm.update_project, "demo", lambda project: project.update(style="水彩"))
                release.set()

                event_name, payload = await _next_event(stream, timeout=5)
                assert event_name == "changes"
                assert [change["entity_type"] for change in payload["changes"]] == ["project"]
                assert payload["source"] == "webui"
        finally:
            release.set()
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_hint_arriving_during_a_rebuild_read_settles_the_change_it_swept(self, tmp_path):
        """A write landing during an explicit rebuild's disk read is read by that read and its hint arrives before
        settling: the sweep broadcasts it with that hint's source.

        显式重建读盘期间落盘的写入被这次读盘读到、它的 hint 也在结算前送达：补扫按该 hint 的来源广播。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")
        reader = _ControlledReader(pm)
        service = ProjectEventService(tmp_path, poll_interval=30.0, read_state=reader)
        await service.start()
        loop = asyncio.get_running_loop()
        read_started = asyncio.Event()
        release = threading.Event()

        def _hold_then_read(read):
            reader.hook = None
            loop.call_soon_threadsafe(read_started.set)
            assert release.wait(timeout=5)
            return read()

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                assert (await anext(stream))[0] == "snapshot"
                reader.hook = _hold_then_read

                emit_project_change_batch("demo", [_terminal_task_change("task-1")], source="worker")
                await asyncio.wait_for(read_started.wait(), timeout=5)
                with project_change_source("webui"):
                    await asyncio.to_thread(pm.update_project, "demo", lambda project: project.update(style="水彩"))
                release.set()

                event_name, own = await _next_event(stream, timeout=5)
                assert (event_name, own["source"]) == ("changes", "worker")
                assert [change["entity_type"] for change in own["changes"]] == ["task"]
                event_name, swept = await _next_event(stream, timeout=5)
                assert event_name == "changes"
                assert [change["entity_type"] for change in swept["changes"]] == ["project"]
                assert swept["source"] == "webui"
        finally:
            release.set()
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_hint_source_repeated_during_rebuild_read_is_not_dropped(self, tmp_path):
        """Source markers arriving again during the rebuild's disk read are not cleared on write-back; the next scan
        still resolves the source from them.

        Markers registered before the read are swapped out as a whole rather than subtracted afterwards: when the
        same source hints again within the window, with set semantics "subtract the old set" would wipe the new
        marker too, and that change would be mismarked as filesystem in the next scan, bypassing the frontend's
        notification suppression for WebUI's own edits.

        重建读盘期间重复到达的来源标记不被写回时清掉，下一轮扫描仍据它解析来源。

        读盘前在册的标记整体换出而非事后减去：同一来源在窗口内再次 hint 时，集合语义下
        「减去旧集合」会把新标记一并抹掉，那条变更会在下一轮扫描被误标成 filesystem，
        绕过前端对 WebUI 自身编辑的通知抑制。
        """
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        reader = _ControlledReader(pm)
        service = ProjectEventService(tmp_path, poll_interval=30.0, read_state=reader)
        await service.start()

        release_read = asyncio.Event()

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, _snapshot = await anext(stream)
                assert event_name == "snapshot"

                channel = service._channels["demo"]
                # The first scan on subscribe clears the markers; wait for it to settle before presetting, otherwise the
                # rebuild captures an empty set
                # 订阅时的首轮扫描会清空标记，等它落定再预置，否则重建捕获到的是空集合
                await asyncio.sleep(0.1)
                assert channel.pending_sources == set()
                # Markers already registered before the rebuild starts
                # 重建发起前已在册的标记
                channel.pending_sources.add("webui")

                read_started = asyncio.Event()
                loop = asyncio.get_running_loop()

                def _read_waiting(read):
                    loop.call_soon_threadsafe(read_started.set)
                    asyncio.run_coroutine_threadsafe(release_read.wait(), loop).result(timeout=5)
                    return read()

                reader.hook = _read_waiting

                emit_project_change_batch("demo", [_terminal_task_change("task-1")], source="worker")
                await asyncio.wait_for(read_started.wait(), timeout=5.0)

                # The same source hints again within the disk-read window
                # 读盘窗口内同一来源再次 hint
                channel.pending_sources.add("webui")
                release_read.set()

                event_name, _payload = await _next_event(stream, timeout=5.0)
                assert event_name == "changes"

                # Markers within the window are kept; the next scan resolves webui from them rather than filesystem
                # 窗口内的标记留存，下一轮扫描据它解析出 webui 而非 filesystem
                assert channel.pending_sources == {"webui"}
                assert service._resolve_batch_source(channel.pending_sources) == "webui"
        finally:
            release_read.set()
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_change_landing_after_rebuild_read_is_still_broadcast(self, tmp_path):
        """A change landing on disk "after the rebuild's disk read completes but before the state write-back" is still
        broadcast to subscribers.

        The injection point is the disk-read seam: write to disk after the real read returns, to construct that
        timing window precisely.

        变更落盘于「重建读盘完成之后、状态写回之前」时，仍能广播到订阅者。

        注入点是读盘 seam：真实读盘返回后再落盘，精确构造该时序窗口。
        """
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        reader = _ControlledReader(pm)
        service = ProjectEventService(tmp_path, poll_interval=0.05, read_state=reader)
        await service.start()

        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, _snapshot = await anext(stream)
                assert event_name == "snapshot"

                armed = False

                def _read_then_land(read):
                    nonlocal armed
                    result = read()
                    if armed:
                        armed = False
                        project_json = pm.get_project_path("demo") / "project.json"
                        data = json.loads(project_json.read_text(encoding="utf-8"))
                        data.setdefault("characters", {})["新角色"] = {"description": "d"}
                        project_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    return result

                reader.hook = _read_then_land
                armed = True

                emit_project_change_batch("demo", [_terminal_task_change("task-1")], source="worker")

                # A change written within the window must still be broadcast eventually (this rebuild did not cover it; the
                # fallback scan must make up for it)
                # 窗口内落盘的变更最终仍要广播（本次重建未覆盖它，兜底扫描须补上）
                deadline = 3.0
                for _ in range(10):
                    event_name, payload = await _next_event(stream, timeout=deadline)
                    assert event_name == "changes"
                    if any(
                        (change["entity_type"], change["action"], change["entity_id"])
                        == ("character", "created", "新角色")
                        for change in payload["changes"]
                    ):
                        break
                else:
                    raise AssertionError("窗口内落盘的角色变更从未广播")
        finally:
            await service.shutdown()

    @pytest.mark.parametrize("edited_input", ["project_json", "existing_script", "new_script", "draft"])
    async def test_polling_pushes_change_to_any_input_file(self, tmp_path, edited_input):
        """After the first snapshot, any input file changing on disk (bypassing hints, found only by poll comparison of
        input identities): pushed as usual.

        首轮快照之后，任一输入文件在盘上变化（绕过 hint，只靠轮询比对输入标识发现）：照常推送。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "drama")
        scene = {
            "scene_id": "E1S01",
            "duration_seconds": 8,
            "utterances": [{"kind": "dialogue", "speaker": "甲", "text": "旧台词"}],
            "generated_assets": _pending_assets(),
        }
        script = {"episode": 1, "title": "第一集", "content_mode": "drama", "scenes": [scene]}
        with project_change_source("filesystem"):
            pm.save_script("demo", script, "episode_1.json", validate=False)
        project_path = pm.get_project_path("demo")

        def edit_on_disk() -> None:
            if edited_input == "project_json":
                path = project_path / ProjectManager.PROJECT_FILE
                data = json.loads(path.read_text(encoding="utf-8"))
                data.setdefault("characters", {})["新角色"] = {"description": "d"}
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            elif edited_input == "existing_script":
                path = project_path / "scripts" / "episode_1.json"
                data = json.loads(path.read_text(encoding="utf-8"))
                data["scenes"][0]["utterances"][0]["text"] = "新台词，比旧台词长"
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            elif edited_input == "new_script":
                other = {**script, "episode": 2, "title": "第二集"}
                (project_path / "scripts" / "episode_2.json").write_text(
                    json.dumps(other, ensure_ascii=False), encoding="utf-8"
                )
            else:
                path = quarantine_path(project_path, 1, QUARANTINE_KIND_NARRATION_SCRIPT_PLAN)
                path.parent.mkdir(parents=True)
                path.write_text("{}", encoding="utf-8")

        expected_entity_type = {
            "project_json": "character",
            "existing_script": "drama_scene",
            "new_script": "episode",
            "draft": "draft",
        }[edited_input]

        service = ProjectEventService(tmp_path, poll_interval=0.1)
        await service.start()
        try:
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                event_name, _snapshot = await anext(stream)
                assert event_name == "snapshot"

                await asyncio.to_thread(edit_on_disk)

                seen: set[str] = set()
                while expected_entity_type not in seen:
                    event_name, payload = await _next_event(stream, timeout=3.0)
                    assert event_name == "changes"
                    seen |= {change["entity_type"] for change in payload["changes"]}
        finally:
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_subscribe_cancellation_cleans_up_subscriber(self, tmp_path, monkeypatch):
        """Client disconnects during the first scan -> _subscribe is cancelled -> no subscriber or watch task leaks.

        客户端在首次扫描期间断开 → _subscribe 被取消 → 订阅者与 watch task 不泄漏。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")

        service = ProjectEventService(tmp_path, poll_interval=0.05)
        await service.start()

        # Simulate the first scan getting stuck: the watch task never sets ready_event, _subscribe parks in wait().
        # 模拟首次扫描卡住:watch task 永不 set ready_event,_subscribe 会 park 在 wait()。
        async def _never_ready(project_name, channel):
            await asyncio.sleep(3600)

        monkeypatch.setattr(service, "_watch_project", _never_ready)

        task = asyncio.create_task(service._subscribe("demo"))
        await asyncio.sleep(0.05)  # Let _subscribe register the queue and park / 让 _subscribe 注册 queue 并 park
        channel = service._channels["demo"]
        assert channel.sse.has_subscribers  # Registered / 已注册
        watch_task = channel.task

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # After cancellation: the subscriber is cleaned up, the channel popped, the watch task cancelled (no leak).
        # 取消后:订阅者被清理、channel 被弹出、watch task 被取消(不泄漏)。
        assert "demo" not in service._channels
        await asyncio.sleep(0)  # Let the watch task's cancellation settle / 让 watch task 的取消落定
        assert watch_task.cancelled() or watch_task.done()

        await service.shutdown()

    @pytest.mark.asyncio
    async def test_new_subscriber_entering_during_stop_watch_stays_registered(self, tmp_path, monkeypatch):
        """While the last subscriber's teardown is suspended at the watch task teardown wait point, a concurrently
        entering new subscriber belongs to the registry's current channel, receives hint broadcasts routed via the
        registry, and the old watch task is retired with no task detached from the registry.

        末订阅者收尾挂起在 watch task 收尾等待点时，并发进入的新订阅者归属注册表现行通道，
        收得到经注册表路由的 hint 广播，且旧 watch task 退役、无脱离注册表的 task。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        service = ProjectEventService(tmp_path, poll_interval=1.0)
        await service.start()

        entered_cancel = asyncio.Event()
        allow_exit = asyncio.Event()
        stop_task: asyncio.Task | None = None

        try:

            async def _controlled_watch(project_name, channel):
                # Release _subscribe's ready wait immediately; when cancelled it stops at the teardown point, widening the
                # race window.
                # 立即放行 _subscribe 的 ready 等待；被取消时停在收尾点，撑开竞态窗口。
                channel.ready_event.set()
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    entered_cancel.set()
                    await allow_exit.wait()
                    raise

            monkeypatch.setattr(service, "_watch_project", _controlled_watch)

            # Subscriber A brings up the old channel and old watch task.
            # 订阅者 A 拉起旧通道与旧 watch task。
            _sse_a, queue_a, _snap_a = await service._subscribe("demo")
            old_channel = service._channels["demo"]
            old_watch_task = old_channel.task

            # A unsubscribing triggers the last-subscriber hook; the hook stops at the teardown point waiting for the
            # cancelled watch task to exit.
            # A 退订触发末订阅者钩子；钩子停在等已取消 watch task 退出的收尾点。
            stop_task = asyncio.create_task(service._unsubscribe("demo", queue_a))
            await asyncio.wait_for(entered_cancel.wait(), timeout=1.0)

            # New subscriber B enters within the window.
            # 窗口内新订阅者 B 进入。
            _sse_b, queue_b, _snap_b = await service._subscribe("demo")

            # Release the old watch task and let the last-subscriber hook finish teardown.
            # 放行旧 watch task，让末订阅者钩子跑完收尾。
            allow_exit.set()
            await asyncio.wait_for(stop_task, timeout=1.0)

            # B belongs to the current channel in the registry; the old channel is retired.
            # B 归属注册表中的现行通道，旧通道已退役。
            assert "demo" in service._channels
            assert service._channels["demo"] is not old_channel

            # The hint broadcast routed via the registry reaches B.
            # 经注册表路由的 hint 广播抵达 B。
            service._apply_emitted_batch(
                "demo",
                "worker",
                (
                    {
                        "entity_type": "segment",
                        "action": "storyboard_ready",
                        "entity_id": "E1S01",
                        "label": "分镜「E1S01」",
                        "focus": None,
                        "important": True,
                    },
                ),
            )
            await asyncio.gather(*list(service._pending_batch_tasks), return_exceptions=True)
            event_name, _payload = queue_b.get_nowait()
            assert event_name == "changes"

            # The old watch task is retired and does not keep running detached from the registry.
            # 旧 watch task 退役，未脱离注册表继续运行。
            assert old_watch_task.done()
        finally:
            # If an assertion or wait_for fails early, the deliberately suspended watch/stop task must still be released,
            # otherwise background tasks and the global listener registration of project_change_hints leak across tests.
            # 断言或 wait_for 提前失败时，被刻意挂起的 watch/stop task 仍需释放，
            # 否则会跨用例泄漏后台任务与 project_change_hints 的全局监听注册。
            allow_exit.set()
            if stop_task is not None and not stop_task.done():
                await asyncio.gather(stop_task, return_exceptions=True)
            await service.shutdown()

    @pytest.mark.asyncio
    async def test_watch_terminates_stream_when_project_directory_deleted(self, tmp_path, caplog):
        """Deleting the project directory while a subscription lives: the scan terminates within one poll cycle -
        broadcasts a termination event, the stream ends normally, the channel is removed from the registry, only one
        INFO log line, no ERROR/traceback.

        订阅存续期间删除项目目录：一个轮询周期内扫描终止——广播终止事件、
        流正常结束、通道从注册表移除，仅记一条 INFO 日志，无 ERROR/traceback。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        service = ProjectEventService(tmp_path, poll_interval=0.05)
        await service.start()

        with caplog.at_level(logging.INFO, logger="server.services.project.project_events"):
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                first = await anext(stream)
                assert first[0] == "snapshot"

                pm.delete_project_directory("demo")

                event_name, payload = await _next_event(stream, timeout=1.5)
                assert event_name == PROJECT_DELETED_EVENT
                assert payload == {"project_name": "demo"}

                # After the termination event the stream ends normally (not because the consumer disconnected).
                # 终止事件之后流正常结束（不是因为消费方主动断线）。
                with pytest.raises(StopAsyncIteration):
                    await anext(stream)

        assert "demo" not in service._channels
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)
        info_records = [
            record for record in caplog.records if record.levelno == logging.INFO and "已被删除" in record.message
        ]
        assert len(info_records) == 1

        await service.shutdown()

    @pytest.mark.asyncio
    async def test_watch_keeps_error_logging_when_only_project_json_missing(self, tmp_path, caplog):
        """Project directory still exists and only project.json is missing - outside this fix's scope: keep the status
        quo, log ERROR via the generic exception fallback, channel not terminated, keeps polling and retrying.

        项目目录仍存在、仅 project.json 缺失——不属于本次修复范围：维持现状，
        按通用异常兜底记 ERROR，通道不终止、继续轮询重试。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        service = ProjectEventService(tmp_path, poll_interval=0.05)
        await service.start()

        with caplog.at_level(logging.INFO, logger="server.services.project.project_events"):
            async with service.stream_events("demo", idle_timeout=0.1):
                (pm.get_project_path("demo") / ProjectManager.PROJECT_FILE).unlink()
                # Wait at least one poll cycle so the scan hits the missing project.json.
                # 等至少一个轮询周期，让扫描命中缺失的 project.json。
                await asyncio.sleep(0.3)
                assert "demo" in service._channels  # Channel not terminated, still in the registry / 通道未终止，仍在注册表中

        assert any(record.levelno >= logging.ERROR for record in caplog.records)
        assert not any("已被删除" in record.message for record in caplog.records)

        await service.shutdown()

    @pytest.mark.asyncio
    async def test_hint_rebuild_terminates_channel_without_error_when_project_deleted(self, tmp_path, caplog):
        """The hint-triggered explicit rebuild path also does termination handling for a deleted project, with no
        ERROR log.

        hint 触发的显式重建路径对已删除项目同样走终止处理，不产生 ERROR 日志。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        # Increase the poll interval so that what we observe is the hint path, not the background poll detecting the
        # deletion first.
        # 轮询间隔调大，确保观察到的是 hint 路径而非后台轮询先行探测到删除。
        service = ProjectEventService(tmp_path, poll_interval=5.0)
        await service.start()

        with caplog.at_level(logging.INFO, logger="server.services.project.project_events"):
            async with service.stream_events("demo", idle_timeout=0.1) as stream:
                first = await anext(stream)
                assert first[0] == "snapshot"

                pm.delete_project_directory("demo")

                emit_project_change_batch(
                    "demo",
                    [
                        {
                            "entity_type": "segment",
                            "action": "updated",
                            "entity_id": "E1S01",
                            "label": "分镜「E1S01」",
                            "focus": None,
                            "important": False,
                        }
                    ],
                    source="worker",
                )

                event_name, payload = await _next_event(stream, timeout=1.5)
                assert event_name == PROJECT_DELETED_EVENT
                assert payload == {"project_name": "demo"}

        assert "demo" not in service._channels
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)

        await service.shutdown()

    @pytest.mark.asyncio
    async def test_new_subscriber_after_project_recreated_gets_fresh_channel(self, tmp_path):
        """After project deletion the old channel terminates; after a project with the same name is recreated, new
        subscriptions get a fresh channel, behaving as now.

        项目删除后原通道终止；同名项目重建后新订阅走全新通道，行为与现在一致。"""
        pm = ProjectManager(tmp_path / "projects")
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo", "Anime", "narration")

        service = ProjectEventService(tmp_path, poll_interval=0.05)
        await service.start()

        async with service.stream_events("demo", idle_timeout=0.1) as stream:
            first = await anext(stream)
            assert first[0] == "snapshot"

            pm.delete_project_directory("demo")

            event_name, _payload = await _next_event(stream, timeout=1.5)
            assert event_name == PROJECT_DELETED_EVENT

        assert "demo" not in service._channels

        # Project with the same name recreated: a new subscription should get a fresh channel and receive the snapshot
        # and later changes normally (not reuse the dying channel).
        # 同名项目重建：新订阅应走全新通道，正常收到 snapshot 与后续变更（不复用将死通道）。
        pm.create_project("demo")
        pm.create_project_metadata("demo", "Demo Reborn", "Anime", "narration")

        async with service.stream_events("demo", idle_timeout=0.1) as stream:
            first = await anext(stream)
            assert first[0] == "snapshot"
            assert "demo" in service._channels

        await service.shutdown()
