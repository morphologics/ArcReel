"""Naming conventions, intermediate directory cleanup and disk pre-check for the v5->v6 directory swap.

The v6 migration rewrites a staging tree in the same parent directory, and after everything succeeds swaps the
project directory for the staging tree with two ``os.replace`` calls: first rename the original directory to the
rollback directory, then rename the staging tree to the project directory. The ``finally`` inside the migrator
process changes a failed swap back; a process hard-killed between the two renames is an abnormal condition with no
automatic recovery (ADR 0022); the cleanup side only guarantees not to delete the rollback directory when the
project directory is missing, leaving it for manual restoration.

v5→v6 目录交换的命名约定、中间目录清理与磁盘预检。

v6 迁移在同父目录 staging 树上改写，全部成功后用两次 ``os.replace`` 把项目目录换成 staging 树：
先把原目录改名为 rollback 目录，再把 staging 树改名为项目目录。迁移器进程内的 ``finally`` 负责把
失败的交换改回；两次改名之间进程被强杀属于非常规条件，不做自动恢复（ADR 0022），只保证清理端
不删项目目录缺失时的 rollback 目录，留给人工改回。
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_ROLLBACK_INFIX = ".v6-rollback-"
_STAGING_INFIX = ".v6-"
# Both kinds of directory names take their suffix from uuid4().hex; the reverse-parsing side uses this to decide
# whether a hidden directory belongs to this convention.
# 两类目录名的后缀都取自 uuid4().hex，反解端据此判定一个隐藏目录是否属于本约定。
_SUFFIX_CHARS = "0123456789abcdef"
# uuid4().hex has a fixed length; checking only the character set would misjudge "custom directories that happen to
# end in hex characters" as products of this convention
_SUFFIX_LENGTH = 32  # uuid4().hex 定长；仅校验字符集会把「恰好以十六进制字符结尾的自定义目录」误判为本约定产物

# The staging tree is a full copy of the original project directory, so during the swap window both the original tree
# and the copy exist in the parent directory. Reserve a 10% margin (minimum 32 MiB) to cover metadata overhead and
# concurrent writes during copytree.
# staging 树是原项目目录的整树副本，交换窗口内父目录上同时存在原树与副本。
# 预留 10% 余量（下限 32 MiB）覆盖 copytree 期间的元数据开销与并发写入。
_HEADROOM_PERCENT = 10
_MIN_HEADROOM_BYTES = 32 * 1024 * 1024


class MigrationDiskSpaceError(OSError):
    """The pre-migration disk capacity check failed. The project directory was not rewritten.

    Inherits ``OSError`` so it falls into the same category as a real ENOSPC during migration, and the import path's
    existing migration failure handling needn't fork.

    迁移前磁盘容量预检不通过。项目目录未被改写。

    继承 ``OSError`` 与迁移期间真实的 ENOSPC 归为一类，导入路径既有的迁移失败处理无需分叉。"""


def new_rollback_dir(project_dir: Path) -> Path:
    """Generate a unique rollback directory path for one swap (not created). / 为一次交换生成唯一的 rollback 目录路径（不创建）。"""
    return project_dir.parent / f".{project_dir.name}{_ROLLBACK_INFIX}{uuid.uuid4().hex}"


def new_staging_dir(project_dir: Path) -> Path:
    """Create and return this swap's staging directory.

    The name has the same structure as the rollback directory (hex suffix), so the cleanup side can reverse-parse the
    project name by the same convention. ``mkdir``'s exclusive semantics replace ``tempfile.mkdtemp``: the uuid suffix
    already guarantees uniqueness, and a name clash is an exception.

    创建并返回本次交换的 staging 目录。

    名字与 rollback 目录同构（十六进制后缀），清理端才能按同一条约定反解出项目名。
    ``mkdir`` 的独占语义顶替 ``tempfile.mkdtemp``：uuid 后缀已保证唯一，重名即异常。"""
    staging = project_dir.parent / f".{project_dir.name}{_STAGING_INFIX}{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    return staging


def _project_name_from_swap_dir(dir_name: str, infix: str) -> str | None:
    """Reverse-parse the project name from a swap intermediate directory name; return None if it doesn't match the naming
    convention or the name is unsafe.

    从交换中间目录名反解项目名；不匹配命名约定或名字不安全时返回 None。"""
    if not dir_name.startswith("."):
        return None
    head, separator, suffix = dir_name.rpartition(infix)
    if not separator or len(suffix) != _SUFFIX_LENGTH or not all(char in _SUFFIX_CHARS for char in suffix):
        return None
    name = head[1:]
    if not name or name in {".", ".."} or "/" in name or "\\" in name or os.sep in name:
        return None
    return name


def rollback_project_name(dir_name: str) -> str | None:
    """rollback directory name -> project name; None for non-rollback directories. / rollback 目录名 → 项目名；非 rollback 目录返回 None。"""
    return _project_name_from_swap_dir(dir_name, _ROLLBACK_INFIX)


def staging_project_name(dir_name: str) -> str | None:
    """staging directory name -> project name; None for rollback directories and non-staging directories.

    staging 目录名 → 项目名；rollback 目录与非 staging 目录返回 None。"""
    if rollback_project_name(dir_name) is not None:
        return None
    return _project_name_from_swap_dir(dir_name, _STAGING_INFIX)


def _candidate_dirs(projects_dir: Path) -> list[Path]:
    """Subdirectories of projects_dir eligible for name reverse-parsing: real directories, excluding symlinks.

    Valid names always start with ``.``, so the string prefix filter goes first and short-circuits the
    ``is_dir``/``is_symlink`` system calls for non-candidates -- startup scans the whole projects_dir unconditionally,
    and with many projects or a slow file system (network mounts, Docker shared directories) the volume of these two
    stat calls gets amplified.

    projects_dir 下可参与命名反解的子目录：真目录、排除符号链接。

    合法命名一律以 ``.`` 开头，字符串前缀过滤先行、短路掉非候选项的 ``is_dir``/``is_symlink``
    系统调用——启动期无条件扫描整个 projects_dir，项目数多或文件系统慢（网络挂载、Docker
    共享目录）时这两次 stat 调用的量级会被放大。"""
    try:
        children = sorted(projects_dir.iterdir())
    except OSError:
        return []
    return [child for child in children if child.name.startswith(".") and child.is_dir() and not child.is_symlink()]


def cleanup_completed_swap_dirs(projects_dir: Path, cutoff: float) -> None:
    """Delete leftover rollback / staging directories whose swap has completed and whose mtime is older than cutoff.

    If the project directory exists, the swap counts as completed and the intermediate directories only take disk
    space; when the project directory is missing, the rollback directory is the only project data and is never deleted.
    The age gate follows the backup cleanup policy and also ensures a running migration's staging tree isn't deleted by
    mistake.

    删除交换已完成、且 mtime 早于 cutoff 的 rollback / staging 遗留目录。

    项目目录存在即视为交换已完成，此时中间目录只占磁盘；项目目录缺失时 rollback 目录是唯一的
    项目数据，一律不删。年龄闸口既沿用备份清理
    策略，也保证正在运行的迁移的 staging 树不会被误删。"""
    if not projects_dir.is_dir():
        return
    for child in _candidate_dirs(projects_dir):
        name = rollback_project_name(child.name) or staging_project_name(child.name)
        if name is None:
            continue
        if not (projects_dir / name).is_dir():
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        # rollback and staging swap directories after migration completes
        shutil.rmtree(child, ignore_errors=True)  # hard-delete: infra -- 迁移完成后的回滚与暂存交换目录
        if child.exists():
            logger.warning("无法删除迁移中间目录：%s", child)


def tree_size(root: Path) -> int:
    """Bytes occupied by files in a directory tree, without following symlinks; unreadable entries are skipped.

    目录树里文件占用的字节数，不跟随符号链接；读不到的条目跳过。"""
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def ensure_disk_headroom(project_dir: Path) -> None:
    """Pre-migration check: the parent directory's free space must fit a full-tree copy; if not, fail without writing a
    single byte.

    迁移前预检：父目录可用空间须容得下整树副本，不足则失败且一个字节都不写。"""
    try:
        free = shutil.disk_usage(project_dir.parent).free
    except OSError:
        logger.warning("无法读取 %s 的磁盘可用空间，跳过迁移前容量预检", project_dir.parent, exc_info=True)
        return
    size = tree_size(project_dir)
    required = size + max(size * _HEADROOM_PERCENT // 100, _MIN_HEADROOM_BYTES)
    if free >= required:
        return
    megabyte = 1024 * 1024
    raise MigrationDiskSpaceError(
        f"磁盘空间不足，无法迁移项目 {project_dir.name}："
        f"迁移需在 {project_dir.parent} 复制整个项目目录，"
        f"至少需要 {required // megabyte} MiB，当前可用 {free // megabyte} MiB。"
        "请清理磁盘后重试，项目目录未被改动。"
    )


__all__ = [
    "MigrationDiskSpaceError",
    "cleanup_completed_swap_dirs",
    "ensure_disk_headroom",
    "new_rollback_dir",
    "new_staging_dir",
    "rollback_project_name",
    "staging_project_name",
    "tree_size",
]
