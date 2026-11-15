"""Tests guarding against concurrent read-modify-write races on reference-to-video scripts.

Covers that `ProjectManager.locked_script` loses no updates when two kinds of concurrent writers operate
on `video_units` at the same time:
1. appending new units (the router's add_unit)
2. writing back generated_assets of existing units (the executor's _update_unit_assets)

参考生视频剧本的并发读-改-写竞态防护测试。

覆盖 `ProjectManager.locked_script` 在两类并发写者同时操作 `video_units` 时不丢更新：
  1. 追加新 unit（对应 router 的 add_unit）
  2. 回写已有 unit 的 generated_assets（对应执行器的 _update_unit_assets）
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from lib.project.project_manager import ProjectManager
from lib.project.project_schema import CURRENT_PROJECT_SCHEMA_VERSION


def _seed_reference_video_project(pm: ProjectManager, name: str, n_units: int) -> None:
    """Create a project + one reference-to-video episode_1 script, preloaded with n_units units.

    创建项目 + 一个 参考生视频的 episode_1 剧本，预置 n_units 个 unit。"""
    pm.create_project(name)
    pm.save_project(
        name,
        {
            "name": name,
            # Production projects are always on the current schema: saving the script registers artifacts in
            # the manifest.
            # 生产项目一律处于当前 schema：剧本保存会把产物登记进清单。
            "schema_version": CURRENT_PROJECT_SCHEMA_VERSION,
            "episodes": [],
            "metadata": {"created_at": "2025-01-01", "updated_at": "2025-01-01"},
        },
    )
    script = {
        "episode": 1,
        "title": "Episode 1",
        "content_mode": "drama",
        "generation_mode": "reference_video",
        "video_units": [
            {
                "unit_id": f"E1U{i}",
                "text": "镜头",
                "duration_seconds": 4,
                "generated_assets": {"video_clip": None, "status": "pending"},
            }
            for i in range(1, n_units + 1)
        ],
    }
    # Testing concurrent RMW with simplified stand-in units
    pm.save_script(name, script, "episode_1.json", validate=False)  # 测并发 RMW 用简化替身 unit


class TestReferenceVideoConcurrentRMW:
    def test_concurrent_append_and_asset_writeback_preserve_all(self, tmp_path: Path) -> None:
        """Concurrently appending new units and writing back assets of existing units; neither is lost.

        并发追加新 unit 与回写已有 unit 资产，二者都不丢。"""
        pm = ProjectManager(tmp_path)
        name = "proj-refvid-rmw"
        n_existing = 12
        _seed_reference_video_project(pm, name, n_existing)

        appenders = 12  # Append E1U13..E1U24 / 追加 E1U13..E1U24
        total = n_existing + appenders
        barrier = threading.Barrier(total)

        def _append(idx: int) -> None:
            """Simulate add_unit: fresh-load -> append -> save inside locked_script.

            模拟 add_unit：locked_script 内 fresh-load → append → save。"""
            barrier.wait()
            with pm.locked_script(name, "episode_1.json", validate=False) as script:
                # Access by schema directly, missing fields raise (fail-loud); do not mask corruption with
                # setdefault
                # 直接按 schema 访问，缺字段即报错（fail-loud），不用 setdefault 掩盖损坏
                script["video_units"].append(
                    {
                        "unit_id": f"E1U{idx}",
                        "text": "镜头",
                        "duration_seconds": 4,
                        "generated_assets": {"video_clip": None, "status": "pending"},
                    }
                )

        def _writeback(unit_id: str) -> None:
            """Simulate the executor's _update_unit_assets: locate the unit and write generated_assets.

            模拟执行器的 _update_unit_assets：定位 unit 写 generated_assets。"""
            barrier.wait()
            with pm.locked_script(name, "episode_1.json", validate=False) as script:
                for u in script["video_units"]:
                    if u.get("unit_id") == unit_id:
                        ga = u.setdefault("generated_assets", {})
                        ga["video_clip"] = f"reference_videos/{unit_id}.mp4"
                        ga["status"] = "completed"
                        break

        with ThreadPoolExecutor(max_workers=total) as pool:
            futures = [pool.submit(_append, i) for i in range(n_existing + 1, total + 1)]
            futures += [pool.submit(_writeback, f"E1U{i}") for i in range(1, n_existing + 1)]
            for fut in as_completed(futures):
                fut.result()

        loaded = pm.load_script(name, "episode_1.json")
        units = {u["unit_id"]: u for u in loaded["video_units"]}

        # All appended units are present / 所有追加的 unit 都在
        assert len(units) == total, f"期望 {total} 个 unit，实际 {sorted(units)}"
        for i in range(n_existing + 1, total + 1):
            assert f"E1U{i}" in units, f"追加的 E1U{i} 丢失"

        # Asset write-backs of all existing units took effect / 所有已有 unit 的资产回写都生效
        for i in range(1, n_existing + 1):
            ga = units[f"E1U{i}"].get("generated_assets") or {}
            assert ga.get("video_clip") == f"reference_videos/E1U{i}.mp4", f"E1U{i} 资产回写丢失：{ga}"
            assert ga.get("status") == "completed"
