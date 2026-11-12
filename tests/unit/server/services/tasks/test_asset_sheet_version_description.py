"""An asset sheet's version records the description it was made from (Make current brings it back)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from lib.artifacts.asset_reference_snapshots import REFERENCE_IMAGE_SNAPSHOT_FIELD
from lib.artifacts.version_manager import SCRIPT_PROMPT_VERSION_FIELD
from server.services.tasks import formal_image_commit


async def _plan_for(monkeypatch, project: dict[str, Any], tmp_path):
    captured: dict[str, Any] = {}

    async def fake_run(**kwargs):
        captured["plan"] = kwargs["plan"]
        return {}

    monkeypatch.setattr(formal_image_commit, "run_formal_image_task", fake_run)
    frozen = SimpleNamespace(prompt="prompt", basis=None, references=None, warnings=())
    await formal_image_commit.run_asset_sheet_image_task(
        asset_type="prop",
        project_name="demo",
        resource_id="Wine Glass",
        payload={},
        user_id="u",
        task_id=None,
        project=project,
        frozen=frozen,  # type: ignore[arg-type]
        context=SimpleNamespace(),  # type: ignore[arg-type]
        project_manager=SimpleNamespace(get_project_path=lambda _name: tmp_path),  # type: ignore[arg-type]
    )
    return captured["plan"]


@pytest.mark.asyncio
async def test_version_keeps_the_description(monkeypatch, tmp_path):
    project = {"props": {"Wine Glass": {"description": "A tall glass, cracked rim"}}}
    plan = await _plan_for(monkeypatch, project, tmp_path)
    assert plan.version_metadata[SCRIPT_PROMPT_VERSION_FIELD] == "A tall glass, cracked rim"
    # no reference image: recorded as none, so Make current clears a later one
    assert plan.version_metadata[REFERENCE_IMAGE_SNAPSHOT_FIELD] == ""


@pytest.mark.asyncio
async def test_no_description_adds_no_prompt(monkeypatch, tmp_path):
    plan = await _plan_for(monkeypatch, {"props": {"Wine Glass": {"description": ""}}}, tmp_path)
    assert SCRIPT_PROMPT_VERSION_FIELD not in plan.version_metadata
