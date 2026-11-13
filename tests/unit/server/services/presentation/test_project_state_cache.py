"""The state-keyed answer cache for the Edit view's read endpoints."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from server.services.presentation.project_state_cache import ProjectStateCache, project_state_identity

_OLD_NS = 10 * 1_000_000_000  # far outside the racy window


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    old = time.time_ns() - _OLD_NS
    os.utime(path, ns=(old, old))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    _write(tmp_path / "project.json", {"title": "p"})
    _write(tmp_path / "versions" / "versions.json", {"videos": {}})
    _write(tmp_path / "scripts" / "episode_1.json", {"segments": []})
    _write(tmp_path / "edit_timelines" / "episode_1" / "tl-1.json", {"id": "tl-1"})
    return tmp_path


def test_identity_is_stable_and_changes_when_a_file_is_rewritten(project: Path) -> None:
    first = project_state_identity(project)
    assert first is not None
    assert project_state_identity(project) == first

    _write(project / "versions" / "versions.json", {"videos": {"E1S01": {}}})
    assert project_state_identity(project) not in (None, first)


def test_new_timeline_or_render_changes_the_identity(project: Path) -> None:
    first = project_state_identity(project)
    _write(project / "edit_timelines" / "episode_1" / "tl-2.json", {"id": "tl-2"})
    second = project_state_identity(project)
    assert second not in (None, first)

    render = project / "renders" / "episode_1" / "tl-1" / "final_cut.burned.mp4"
    render.parent.mkdir(parents=True)
    render.write_bytes(b"mp4")
    old = time.time_ns() - _OLD_NS
    os.utime(render, ns=(old, old))
    assert project_state_identity(project) not in (None, second)


def test_a_just_written_file_makes_the_state_uncacheable(project: Path) -> None:
    (project / "project.json").write_text("{}", encoding="utf-8")   # mtime = now
    assert project_state_identity(project) is None


async def test_answer_is_reused_until_the_project_changes(project: Path) -> None:
    cache = ProjectStateCache()
    calls = 0

    async def compute() -> str:
        nonlocal calls
        calls += 1
        return f"answer {calls}"

    assert await cache.get_or_compute(project, ("k",), compute) == "answer 1"
    assert await cache.get_or_compute(project, ("k",), compute) == "answer 1"
    assert calls == 1

    await asyncio.to_thread(_write, project / "scripts" / "episode_1.json", {"segments": [1]})
    assert await cache.get_or_compute(project, ("k",), compute) == "answer 2"
    assert await cache.get_or_compute(project, ("other",), compute) == "answer 3"


async def test_nothing_is_cached_when_the_project_changes_during_the_computation(project: Path) -> None:
    cache = ProjectStateCache()
    calls = 0

    async def compute() -> int:
        nonlocal calls
        calls += 1
        # a different length each time: the test backdates mtimes, so size is what tells the writes apart
        await asyncio.to_thread(_write, project / "project.json", {"title": "edit" + "!" * calls})
        return calls

    assert await cache.get_or_compute(project, ("k",), compute) == 1
    assert await cache.get_or_compute(project, ("k",), compute) == 2
