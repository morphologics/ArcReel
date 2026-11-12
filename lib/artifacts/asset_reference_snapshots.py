"""A copy of the reference image each asset sheet version was made from, so Make current can bring it back.

A character's or prop's reference image is one uploaded file (``{subdir}/refs/{name}.png``); a new upload replaces
it in place. Each sheet generation keeps a content-addressed copy under ``versions/{subdir}/refs/{sha256}{ext}`` and
records its path on the version (``reference_image_snapshot``; ``""`` when the sheet was made without one). Restoring
a version puts that image back as the asset's reference image. Versions from before this field only carry the image's
digest (in their ``artifact_image_basis``): their reference can be restored only while the current file still has
that digest, or cleared when they were made without one.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from lib.infra.content_digest import sha256_file
from lib.project.asset_types import ASSET_SPECS, resolve_asset_key

#: Version record field: project path of the reference image copy, or "" when the sheet had no reference image.
REFERENCE_IMAGE_SNAPSHOT_FIELD = "reference_image_snapshot"

#: Asset types with one replaceable reference image.
SINGLE_REFERENCE_TYPES = ("character", "prop")

RestoreOutcome = Literal["restored", "cleared", "unchanged", "unavailable"]


def _entry(project: Mapping[str, Any], asset_type: str, name: str) -> dict[str, Any] | None:
    bucket = project.get(ASSET_SPECS[asset_type].bucket_key) or {}
    key = resolve_asset_key(bucket, name)
    entry = bucket.get(key) if key is not None else None
    return entry if isinstance(entry, dict) else None


def _current_reference(project_path: Path, entry: Mapping[str, Any] | None) -> Path | None:
    ref = (entry or {}).get("reference_image")
    if not isinstance(ref, str) or not ref:
        return None
    path = project_path / ref
    return path if path.is_file() else None


def snapshot_reference_image(
    project_path: Path, project: Mapping[str, Any], asset_type: str, name: str
) -> dict[str, str]:
    """Version metadata for a sheet about to be generated: a copy of its current reference image (if any).

    Empty for asset types without a single reference image, or when the copy fails (the version then behaves like
    an older one).
    """
    if asset_type not in SINGLE_REFERENCE_TYPES:
        return {}
    source = _current_reference(project_path, _entry(project, asset_type, name))
    if source is None:
        return {REFERENCE_IMAGE_SNAPSHOT_FIELD: ""}
    try:
        relative = f"versions/{ASSET_SPECS[asset_type].subdir}/refs/{sha256_file(source)}{source.suffix.lower()}"
        target = project_path / relative
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp")
            shutil.copyfile(source, tmp)
            tmp.replace(target)
    except OSError:
        return {}
    return {REFERENCE_IMAGE_SNAPSHOT_FIELD: relative}


def _basis_reference_digests(record: Mapping[str, Any]) -> list[str] | None:
    """Digests of the uploaded originals a version was made from (its recorded basis); None when not recorded."""
    basis = record.get("artifact_image_basis")
    inputs = basis.get("inputs") if isinstance(basis, Mapping) else None
    refs = inputs.get("references") if isinstance(inputs, Mapping) else None
    if not isinstance(refs, list):
        return None
    return [r["sha256"] for r in refs if isinstance(r, Mapping) and r.get("kind") == "original" and r.get("sha256")]


def restore_reference_image(
    project_manager: Any, project_name: str, asset_type: str, name: str, record: Mapping[str, Any]
) -> RestoreOutcome:
    """Make the asset's reference image the one ``record`` (a sheet version) was made from."""
    if asset_type not in SINGLE_REFERENCE_TYPES:
        raise ValueError(f"asset type has no single reference image: {asset_type}")
    project_path = Path(project_manager.get_project_path(project_name))
    entry = _entry(project_manager.load_project(project_name), asset_type, name)
    if entry is None:
        raise KeyError(name)
    current = _current_reference(project_path, entry)
    current_digest = sha256_file(current) if current is not None else None

    snapshot = record.get(REFERENCE_IMAGE_SNAPSHOT_FIELD)
    if isinstance(snapshot, str):
        wanted_path = project_path / snapshot if snapshot else None
        if wanted_path is not None and not wanted_path.is_file():
            return "unavailable"
        wanted_digest = sha256_file(wanted_path) if wanted_path is not None else None
    else:
        digests = _basis_reference_digests(record)
        if digests is None or len(digests) > 1:
            return "unavailable"
        wanted_path = None
        wanted_digest = digests[0] if digests else None
        if wanted_digest is not None and wanted_digest != current_digest:
            return "unavailable"

    if wanted_digest == current_digest:
        return "unchanged"
    if wanted_digest is None:
        project_manager.clear_asset_reference_image(project_name, asset_type, name)
        return "cleared"

    assert wanted_path is not None
    spec = ASSET_SPECS[asset_type]
    relative = f"{spec.subdir}/refs/{name}{wanted_path.suffix.lower()}"
    target = project_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    shutil.copyfile(wanted_path, tmp)
    tmp.replace(target)
    if asset_type == "character":
        project_manager.update_character_reference_image(project_name, name, relative)
    else:
        project_manager.update_prop_reference_image(project_name, name, relative)
    return "restored"
