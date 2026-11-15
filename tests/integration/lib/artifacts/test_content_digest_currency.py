"""How the content digest cache affects artifact currency verdicts: the digest is still a content hash, the cache only
saves repeated reads.

Assertions are on external verdicts (artifacts depending on an asset design sheet are stale / current); the one
exception is the performance convention "the same file is hashed only once while its stat is unchanged": at the
file-system boundary, count per inode how many times the artifact content is read in full.

内容摘要缓存对产物时效结论的影响：摘要仍是内容哈希，缓存只省重复的读。

断言的是外部结论（依赖资产设计图的产物判 stale / current）；只有「stat 不变时同一文件只哈希一次」
这条性能约定例外：在文件系统边界上按 inode 记录产物内容被整读的次数。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import IO, Any

import pytest

from lib.infra.content_digest import reset_file_digest_cache_for_tests
from lib.infra.json_io import atomic_write_json
from lib.project.project_manager import ProjectManager
from lib.workflow.workflow_state import WorkflowStateService
from tests.integration.lib.workflow.test_workflow_state import (
    _make_project,
    _register_produced_artifacts,
    _valid_ad_shot,
    _write_artifact,
)

SHEET = "characters/Alice.png"
STORYBOARD = "storyboards/scene_E1S01.png"


@pytest.fixture(autouse=True)
def fresh_digest_cache() -> None:
    reset_file_digest_cache_for_tests()


def _age(path: Path, *, seconds: int = 3600) -> None:
    """Set mtime far enough back: freshly written files do not enter the digest cache, so to observe caching they must be
    made "old" first.

    把 mtime 拨到足够久以前：刚写的文件不入摘要缓存，要观察缓存行为得先让它「老」。"""

    aged = time.time_ns() - seconds * 1_000_000_000
    os.utime(path, ns=(aged, aged))


def _storyboard_project(tmp_path: Path) -> tuple[ProjectManager, Path]:
    """One character design sheet + one storyboard image referencing it, already backfilled into the artifact manifest,
    with both images' mtime made "old".

    一张角色设计图 + 一张引用它的分镜图，已补录进产物清单，两张图的 mtime 都已「老」。"""

    pm, project_path = _make_project(tmp_path, "ad")
    _write_artifact(project_path, SHEET)
    _write_artifact(project_path, STORYBOARD)
    pm.update_project(
        "demo",
        lambda project: project.update(characters={"Alice": {"description": "red coat", "character_sheet": SHEET}}),
    )
    atomic_write_json(
        project_path / "scripts" / "episode_1.json",
        {
            "episode": 1,
            "title": "广告",
            "content_mode": "ad",
            "shots": [_valid_ad_shot(characters_in_shot=["Alice"], generated_assets={"storyboard_image": STORYBOARD})],
        },
    )
    _register_produced_artifacts(project_path)
    _age(project_path / SHEET)
    _age(project_path / STORYBOARD)
    reset_file_digest_cache_for_tests()
    return pm, project_path


def _storyboard_state(pm: ProjectManager) -> dict[str, list[str]]:
    return WorkflowStateService(pm).get_status("demo").artifacts["storyboards"]


def _count_content_reads(monkeypatch: pytest.MonkeyPatch, *targets: Path) -> dict[str, int]:
    """Count at the file-system boundary how many times the content of ``targets`` is read in full in total: each open by
    path (streaming hash) counts once; reads by descriptor count only for descriptors that read more than 1 byte (the
    presence probe reads only 1 byte, not a hash).

    在文件系统边界上数 ``targets`` 的内容合计被整读几次：按路径打开（流式哈希）一次算一次，
    按描述符读的则只算读到超过 1 字节的描述符（在场探针只读 1 字节，不是哈希）。"""

    identities = {(target.stat().st_dev, target.stat().st_ino) for target in targets}
    counts = {"content_reads": 0}
    fd_reads: dict[int, int] = {}
    original_path_open = Path.open
    original_os_open = os.open
    original_os_read = os.read
    original_os_close = os.close

    def _is_target(path: Path) -> bool:
        try:
            info = path.stat()
        except OSError:
            return False
        return (info.st_dev, info.st_ino) in identities

    def _counted_path_open(self: Path, *args: Any, **kwargs: Any) -> IO[Any]:
        if _is_target(self):
            counts["content_reads"] += 1
        return original_path_open(self, *args, **kwargs)

    def _counted_os_open(path: object, *args: object, **kwargs: object) -> int:
        fd = original_os_open(path, *args, **kwargs)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) in identities:
            fd_reads[fd] = 0
        return fd

    def _counted_os_read(fd: int, length: int, /) -> bytes:
        data = original_os_read(fd, length)
        if fd in fd_reads:
            previous = fd_reads[fd]
            fd_reads[fd] = previous + len(data)
            if previous <= 1 < fd_reads[fd]:
                counts["content_reads"] += 1
        return data

    def _counted_os_close(fd: int, /) -> None:
        fd_reads.pop(fd, None)
        original_os_close(fd)

    monkeypatch.setattr(Path, "open", _counted_path_open)
    monkeypatch.setattr(os, "open", _counted_os_open)
    monkeypatch.setattr(os, "read", _counted_os_read)
    monkeypatch.setattr(os, "close", _counted_os_close)
    return counts


@pytest.mark.parametrize("replacement", [b"ARTIFACT", b"a redrawn design sheet"], ids=["same-size", "different-size"])
def test_replaced_design_sheet_stales_the_storyboard_that_uses_it(tmp_path: Path, replacement: bytes) -> None:
    """First load the old image into the digest cache, then replace the image content: whether or not the size happens to
    be the same, downstream is judged stale.

    先让摘要缓存装进旧图，再替换图内容：不论大小是否碰巧相同，下游都判 stale。"""

    pm, project_path = _storyboard_project(tmp_path)
    assert _storyboard_state(pm)["current_ids"] == ["E1S01"]
    sheet = project_path / SHEET
    assert (len(replacement) == len(sheet.read_bytes())) is (replacement == b"ARTIFACT")

    sheet.write_bytes(replacement)

    state = _storyboard_state(pm)
    assert state["stale_ids"] == ["E1S01"]
    assert state["current_ids"] == []


def test_a_copy_that_changes_the_mtime_keeps_the_storyboard_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copy, import and restore change mtime (sometimes the inode too): rehash, and if the content did not change it is
    still current.

    复制、导入、恢复会改 mtime（有时连 inode 一起换）：重新哈希，内容没变就仍是 current。"""

    pm, project_path = _storyboard_project(tmp_path)
    sheet = project_path / SHEET
    assert _storyboard_state(pm)["current_ids"] == ["E1S01"]

    # Change mtime in place
    # 原地改 mtime
    _age(sheet, seconds=1800)
    assert _storyboard_state(pm)["current_ids"] == ["E1S01"]

    # A same-content copy with a new inode
    # 换一个新 inode 的同内容副本
    duplicate = sheet.with_name("Alice.copy.png")
    shutil.copyfile(sheet, duplicate)
    _age(duplicate, seconds=900)
    duplicate.replace(sheet)
    state = _storyboard_state(pm)
    assert state["current_ids"] == ["E1S01"]
    assert state["stale_ids"] == []


def test_unchanged_stat_hashes_the_design_sheet_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Performance convention: while stat is unchanged, repeatedly reading artifact status reads the design sheet in full
    only once; it is read again only when stat changes.

    性能约定：stat 不变时，反复读产物状态也只整读一次设计图；stat 变了才再读。"""

    pm, project_path = _storyboard_project(tmp_path)
    sheet = project_path / SHEET
    counts = _count_content_reads(monkeypatch, sheet)

    for _ in range(3):
        assert _storyboard_state(pm)["current_ids"] == ["E1S01"]
    assert counts["content_reads"] == 1

    _age(sheet, seconds=1800)
    assert _storyboard_state(pm)["current_ids"] == ["E1S01"]
    assert counts["content_reads"] == 2
