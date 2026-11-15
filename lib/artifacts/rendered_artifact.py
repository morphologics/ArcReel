"""Writing and registering locally rendered artifacts: basis snapshot → render to a temp file inside the project →
acceptance check → atomically replace the formal file and register.

Artifacts produced by local rendering, such as final cuts and JianYing drafts, share this flow (``docs/adr/0095``):

1. The caller takes the generation basis snapshot when the task starts and passes it as ``basis``;
2. Rendering writes to a hidden temp file in the formal file's directory, with intermediate files in an equally
   hidden work directory;
3. The acceptance callback checks the temp file and raises on failure; the formal file and registration stay untouched;
4. After acceptance, the old registration is removed first, then the formal file is atomically replaced, the
   version record is written, and finally it is registered with the snapshot basis.

If registration fails after replacement, the formal file already has the new content but no registration; it reads
as missing and is never mistaken for current.
Each artifact identity keeps only the latest file; the version number is kept in the render record next to the
formal file and incremented on each registration.
Rendered artifacts live under the project's ``renders/`` and are not included in project archives.

本地渲染产物的落盘与登记：依据快照 → 渲染到项目内临时文件 → 验收 → 原子替换正式文件并登记。

成片与剪映草稿这类由本地渲染得到的产物共用这一流程（``docs/adr/0095``）：

1. 调用方在任务开始时取好生成依据快照，传入 ``basis``；
2. 渲染写到正式文件同目录下的隐藏临时文件，中间文件放在同样隐藏的工作目录里；
3. 验收回调检查临时文件，未通过时抛异常，正式文件与登记都不动；
4. 验收通过后先撤下旧登记，再原子替换正式文件，先写版本记录，最后按快照依据登记。

替换后登记失败时正式文件已是新内容，但没有登记，读时判为 missing，不会被误判为时新。
每个产物身份只保留最新文件；版本号记在正式文件旁的渲染记录里，每次登记加一。
渲染产物放在项目的 ``renders/`` 下，不进项目归档。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from lib.artifacts.artifact_manifest import (
    ArtifactBasis,
    ArtifactKey,
    ArtifactManifest,
    ProjectArtifactManifestAdapter,
)
from lib.artifacts.formal_write import project_metadata_lock
from lib.infra.async_thread import run_sync_transaction
from lib.infra.json_io import atomic_write_json
from lib.project.project_activity import record_project_activity

RENDERS_DIRNAME = "renders"
"""本地渲染产物的项目根目录：可随时重新渲染，项目归档不包含它。"""

_RECORD_SUFFIX = ".render.json"


def timeline_renders_dir(episode: int, timeline_id: str) -> str:
    """Project-relative directory holding all rendered artifacts of one edit timeline; timeline IDs are not reused and the directory is removed with the timeline.

    一条剪辑时间线全部渲染产物所在的项目内相对目录；时间线 ID 不复用，目录随时间线删除。"""
    return f"{RENDERS_DIRNAME}/episode_{episode}/{timeline_id}"


@dataclass(frozen=True, slots=True)
class RenderRecord:
    """Render record next to the formal file: version number, registration time and the basis used for registration.

    正式文件旁的渲染记录：版本号、登记时间与登记所用依据。"""

    version: int
    rendered_at: str
    basis_digest: str


@dataclass(frozen=True, slots=True)
class RenderedArtifact[AcceptanceT]:
    key: ArtifactKey
    artifact_path: str
    record: RenderRecord
    acceptance: AcceptanceT


type RenderStep = Callable[[Path, Path], Awaitable[None]]
"""``render(output, workspace)``：把产物写到 ``output``，中间文件只放在 ``workspace`` 里。"""

type AcceptStep[AcceptanceT] = Callable[[Path], Awaitable[AcceptanceT]]
"""``accept(output)``：验收临时文件并返回验收结果，未通过时抛异常。"""


def _record_path(formal: Path) -> Path:
    return formal.with_name(formal.stem + _RECORD_SUFFIX)


def read_render_record(project_dir: Path, artifact_path: str) -> RenderRecord | None:
    """Read the render record next to the formal file; None when absent or unparseable. / 读正式文件旁的渲染记录；没有或无法解析时返回 None。"""
    try:
        payload = json.loads(_record_path(project_dir / artifact_path).read_text(encoding="utf-8"))
        return RenderRecord(
            version=int(payload["version"]),
            rendered_at=str(payload["rendered_at"]),
            basis_digest=str(payload["basis_digest"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _replace_and_register(
    project_dir: Path, key: ArtifactKey, artifact_path: str, rendered: Path, basis: ArtifactBasis
) -> RenderRecord:
    formal = project_dir / artifact_path
    with project_metadata_lock(project_dir):
        previous = read_render_record(project_dir, artifact_path)
        adapter = ProjectArtifactManifestAdapter(project_dir)
        claimed = adapter.get_entry(key)
        manifest = ArtifactManifest(adapter)
        manifest.forget_entry_transactionally(key)
        try:
            os.replace(rendered, formal)
        except BaseException:
            # The formal file was not replaced; the old file still matches the old registration. / 正式文件没被替换，旧文件仍与旧登记相符。
            if claimed is not None and adapter.get_entry(key) is None:
                adapter.put_entry(key, claimed)
            raise
        record = RenderRecord(
            version=(previous.version if previous is not None else 0) + 1,
            rendered_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            basis_digest=basis.digest,
        )
        atomic_write_json(
            _record_path(formal),
            {"version": record.version, "rendered_at": record.rendered_at, "basis_digest": record.basis_digest},
        )
        manifest.register(key, artifact_path=artifact_path, basis=basis)
        record_project_activity(project_dir)
    return record


def cleanup_interrupted_render_staging(project_dir: Path) -> None:
    """Clean up render temp files after a restart; the caller must make sure no render of this process is still running for the project.

    重启后清理渲染临时文件；调用方须确认项目没有本进程仍在执行的渲染。"""
    root = project_dir / RENDERS_DIRNAME
    for path in root.rglob(".*"):
        if re.fullmatch(r"(?:\..+\.[0-9a-f]{32}\.(?:work|partial\.[^.]+)|\.project\.[^.]+\.tmp)", path.name):
            if path.is_dir() and not path.is_symlink():
                # clean up the .work directory left by an interrupted render after restart
                shutil.rmtree(path)  # hard-delete: temp -- 重启后清理中断渲染留下的 .work 目录
            else:
                # clean up temp files left by an interrupted render after restart
                path.unlink(missing_ok=True)  # hard-delete: temp -- 重启后清理中断渲染留下的临时文件


def _discard(output: Path, workspace: Path) -> None:
    output.unlink(missing_ok=True)  # hard-delete: temp -- render output that was not adopted / 未被采纳的渲染输出
    shutil.rmtree(workspace, ignore_errors=True)  # hard-delete: temp -- render work directory / 渲染工作目录


async def commit_rendered_artifact[AcceptanceT](
    project_dir: Path,
    *,
    key: ArtifactKey,
    artifact_path: str,
    basis: ArtifactBasis,
    render: RenderStep,
    accept: AcceptStep[AcceptanceT],
) -> RenderedArtifact[AcceptanceT]:
    """Render, accept and register one locally rendered artifact from the snapshot basis; on failure at any step the temp file and work directory are cleaned up.

    按快照依据渲染、验收并登记一件本地渲染产物；任一步失败时临时文件与工作目录都被清理。"""
    formal = project_dir / artifact_path
    await asyncio.to_thread(formal.parent.mkdir, parents=True, exist_ok=True)
    token = uuid4().hex
    output = formal.with_name(f".{formal.stem}.{token}.partial{formal.suffix}")
    workspace = formal.with_name(f".{formal.stem}.{token}.work")
    try:
        await run_sync_transaction(workspace.mkdir)
        await render(output, workspace)
        acceptance = await accept(output)
        record = await run_sync_transaction(_replace_and_register, project_dir, key, artifact_path, output, basis)
    finally:
        await asyncio.to_thread(_discard, output, workspace)
    return RenderedArtifact(key=key, artifact_path=artifact_path, record=record, acceptance=acceptance)


__all__ = [
    "RENDERS_DIRNAME",
    "AcceptStep",
    "RenderRecord",
    "RenderStep",
    "RenderedArtifact",
    "cleanup_interrupted_render_staging",
    "commit_rendered_artifact",
    "read_render_record",
]
