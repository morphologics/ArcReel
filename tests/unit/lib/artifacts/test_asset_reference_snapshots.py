"""Asset sheet versions keep their reference image; Make current puts it back."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from lib.artifacts.asset_reference_snapshots import (
    REFERENCE_IMAGE_SNAPSHOT_FIELD,
    restore_reference_image,
    snapshot_reference_image,
)


class FakeManager:
    def __init__(self, path: Path, project: dict[str, Any]):
        self.path = path
        self.project = project

    def get_project_path(self, _name: str) -> Path:
        return self.path

    def load_project(self, _name: str) -> dict[str, Any]:
        return self.project

    def update_prop_reference_image(self, _project: str, name: str, ref: str) -> None:
        self.project["props"][name]["reference_image"] = ref

    def update_character_reference_image(self, _project: str, name: str, ref: str) -> None:
        self.project["characters"][name]["reference_image"] = ref

    def clear_asset_reference_image(self, _project: str, asset_type: str, name: str) -> None:
        self.project[f"{asset_type}s"][name]["reference_image"] = ""


def _project(tmp_path: Path, content: bytes | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"description": "glass", "reference_image": ""}
    if content is not None:
        (tmp_path / "props/refs").mkdir(parents=True)
        (tmp_path / "props/refs/Glass.png").write_bytes(content)
        entry["reference_image"] = "props/refs/Glass.png"
    return {"props": {"Glass": entry}}


def test_snapshot_copies_the_reference_once(tmp_path):
    project = _project(tmp_path, b"first")
    meta = snapshot_reference_image(tmp_path, project, "prop", "Glass")
    digest = hashlib.sha256(b"first").hexdigest()
    assert meta == {REFERENCE_IMAGE_SNAPSHOT_FIELD: f"versions/props/refs/{digest}.png"}
    assert (tmp_path / meta[REFERENCE_IMAGE_SNAPSHOT_FIELD]).read_bytes() == b"first"
    assert snapshot_reference_image(tmp_path, project, "prop", "Glass") == meta


def test_snapshot_without_reference_records_none(tmp_path):
    assert snapshot_reference_image(tmp_path, _project(tmp_path, None), "prop", "Glass") == {
        REFERENCE_IMAGE_SNAPSHOT_FIELD: ""
    }


def test_snapshot_skips_types_without_a_single_reference(tmp_path):
    assert snapshot_reference_image(tmp_path, {"scenes": {}}, "scene", "Hall") == {}


def test_restore_puts_the_old_reference_back(tmp_path):
    project = _project(tmp_path, b"first")
    record = snapshot_reference_image(tmp_path, project, "prop", "Glass")
    (tmp_path / "props/refs/Glass.png").write_bytes(b"second")
    manager = FakeManager(tmp_path, project)
    assert restore_reference_image(manager, "demo", "prop", "Glass", record) == "restored"
    assert (tmp_path / project["props"]["Glass"]["reference_image"]).read_bytes() == b"first"
    assert restore_reference_image(manager, "demo", "prop", "Glass", record) == "unchanged"


def test_restore_clears_when_the_version_had_none(tmp_path):
    project = _project(tmp_path, b"first")
    manager = FakeManager(tmp_path, project)
    assert restore_reference_image(manager, "demo", "prop", "Glass", {REFERENCE_IMAGE_SNAPSHOT_FIELD: ""}) == "cleared"
    assert project["props"]["Glass"]["reference_image"] == ""


def test_older_versions_fall_back_to_the_recorded_digest(tmp_path):
    project = _project(tmp_path, b"current")
    manager = FakeManager(tmp_path, project)

    def basis(*digests: str) -> dict[str, Any]:
        refs = [{"kind": "original", "sha256": d} for d in digests]
        return {"artifact_image_basis": {"inputs": {"references": refs}}}

    same = hashlib.sha256(b"current").hexdigest()
    assert restore_reference_image(manager, "demo", "prop", "Glass", basis(same)) == "unchanged"
    assert restore_reference_image(manager, "demo", "prop", "Glass", basis("0" * 64)) == "unavailable"
    assert restore_reference_image(manager, "demo", "prop", "Glass", basis()) == "cleared"
