"""Result versions of generation records (API G16): read from versions.json, re-read when the file changes.

生成记录的结果版本（API G16）：从 versions.json 读取，文件变化后重新读取。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from lib.generation.generation_records import legacy_request, load_result_versions


def _write(project: Path, version_file: str, *, mtime_ns: int) -> None:
    path = project / "versions" / "versions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"storyboards": {"E1S01": {"current_version": 1, "versions": [{"version": 1, "file": version_file}]}}}
        ),
        encoding="utf-8",
    )
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _task() -> dict:
    return {
        "task_id": "T1",
        "project_name": "demo",
        "task_type": "storyboard",
        "resource_type": None,
        "resource_id": "E1S01",
        "status": "succeeded",
        "result": {"version": 1, "file_path": "storyboards/scene_E1S01.png"},
    }


def test_result_version_is_found_and_a_rewritten_file_is_read_again(tmp_path: Path) -> None:
    _write(tmp_path, "versions/storyboards/a.png", mtime_ns=1_000_000_000)
    first = load_result_versions([_task()], lambda _name: tmp_path)
    assert first[("demo", "T1", 1)]["file"] == "versions/storyboards/a.png"

    _write(tmp_path, "versions/storyboards/bb.png", mtime_ns=2_000_000_000)
    second = load_result_versions([_task()], lambda _name: tmp_path)
    assert second[("demo", "T1", 1)]["file"] == "versions/storyboards/bb.png"


def test_unfinished_tasks_and_missing_histories_have_no_result(tmp_path: Path) -> None:
    queued = {**_task(), "status": "queued", "result": None}
    assert load_result_versions([queued], lambda _name: tmp_path) == {}
    assert load_result_versions([_task()], lambda _name: tmp_path / "nowhere") == {}


def test_legacy_request_keeps_the_payload_prompt_and_declared_options() -> None:
    request = legacy_request({"prompt": "p", "seed": 4, "video_model_override": "v"}, "gemini")
    assert request == {
        "schema": 0,
        "taken_at": None,
        "prompt": {"editable": None, "source": None, "request": "p"},
        "model": {"provider": "gemini", "model": "v"},
        "options": {"seed": 4},
        "references": [],
    }
