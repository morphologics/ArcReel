"""Storage service for Agent memory directories: single implementation of list / read / upsert / delete / clear.

Parameterized by directory, so project memory and user memory share one class -- both levels have exactly the same
file name rules, size limits, index over-limit rules and frontmatter parsing; separate copies would make the two REST
routes' behaviour diverge over time. The index's line / byte counts and over-limit check come from
``lib.agent.agent_memory_index``, the same source as the truncation rules used when session assembly injects the user
memory index.

A missing directory is always treated as empty: memory is created by the Agent on demand, and a creator opening the
memory page before the Agent writes its first memory should not see an error.

No locks, no conflict detection: single-file writes go through a temp file in the same directory + rename for atomic
persistence; of two concurrent PUTs the later write wins, and reads always see one complete write, never half a file.
Deleting and clearing project memory moves it into the project trash under the project lock (``docs/adr/0107``); user
memory is deleted directly.

Agent 记忆目录的存储服务：列表 / 读 / upsert / 删 / 清空的单一实现。

按目录参数化，项目记忆与用户记忆共用同一个类——两级记忆的文件名规则、大小上限、
索引超限口径与 frontmatter 解析完全相同，各写一份会让两根 REST 路由的行为随时间分叉。
索引的行数 / 字节数与超限判定取自 ``lib.agent.agent_memory_index``，与会话装配注入用户记忆
索引时的截断口径同源。

目录不存在一律视同空目录：记忆是 Agent 按需创建的，创作者在 Agent 写下第一条记忆
之前打开记忆页不应看到错误。

无锁、无冲突检测：单文件写入走同目录临时文件 + rename 原子落盘，并发的两次 PUT
后写胜出，读到的永远是某一次完整写入的内容而不是半截文件。项目记忆的删除与清空持项目锁
移进项目回收站（``docs/adr/0107``），用户记忆直接删除。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from lib.agent.agent_memory_index import INDEX_FILENAME, memory_index_stats
from lib.agent.agent_memory_paths import project_memory_dir
from lib.artifacts.formal_write import project_metadata_lock
from lib.infra.api_errors import BadRequestError, NotFoundError
from lib.infra.json_io import atomic_write_bytes
from lib.infra.path_safety import PathTraversalError, safe_join, try_safe_join
from lib.project import project_trash
from lib.project.project_activity import record_project_activity

#: Body size limit for a single memory file. Over the limit the write is rejected, so the creator splits it rather than the Agent reading a big file every turn.
#: 单个记忆文件的正文上限。超限拒绝写入，让创作者拆分而不是让 Agent 每轮都读进一个大文件。
MAX_FILE_BYTES = 256 * 1024

#: Valid values of frontmatter ``type``. Anything else is treated as no tag, without error. / frontmatter ``type`` 的合法取值。取值之外的一律当作没有标签，不报错。
MEMORY_TYPES = frozenset({"user", "feedback", "project", "reference"})

#: File name limit: together with ``_FILENAME_PATTERN`` it forms one yardstick for visibility and readability/writability.
#: 文件名上限：与 ``_FILENAME_PATTERN`` 一起构成可见性与可读写性的同一把尺子。
FILENAME_MAX_LENGTH = 100

# Spaces and parentheses are allowed: when the trash restores to an occupied original name it becomes ``name (n).md``.
# 允许空格与括号：回收站恢复到被占用的原名时换成 ``name (n).md``。
_FILENAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._() -]*\.md$"


def is_valid_memory_filename(filename: str) -> bool:
    """Whether ``filename`` is a valid top-level memory file name.

    The first character is restricted to alphanumerics, so hidden files starting with ``.`` and temp files of atomic
    writes are both invalid: temp files neither appear in the list nor get read/written as memory.

    ``filename`` 是否为合法的顶层记忆文件名。

    首字符限定字母数字，因此 ``.`` 开头的隐藏文件与原子写入的临时文件都不合法：
    临时文件既不会出现在列表里，也不会被当成记忆读写。
    """
    return len(filename) <= FILENAME_MAX_LENGTH and re.fullmatch(_FILENAME_PATTERN, filename) is not None


def parse_memory_frontmatter(raw: bytes) -> dict[str, Any] | None:
    """Parse a memory file's YAML frontmatter, returning ``{name, description, type}``.

    This is the only YAML parsing point of the memory domain: parsing happens server-side, the frontend only consumes
    results already judged valid. Missing frontmatter, YAML syntax errors, non-objects, or ``type`` not in
    :data:`MEMORY_TYPES` all return ``None`` -- memory files are written freely by the Agent, incomplete metadata is normal
    rather than an error; the file is simply untagged and should not fail the whole list request.

    解析记忆文件的 YAML frontmatter，返回 ``{name, description, type}``。

    这是记忆域唯一的 YAML 解析点：解析发生在服务端，前端只消费已判定合法的结果。
    缺 frontmatter、YAML 语法错、不是对象、``type`` 不在 :data:`MEMORY_TYPES` 内
    一律返回 ``None``——记忆文件由 Agent 自由书写，元数据不全是常态而非错误，
    该文件只是不带标签，不该让整个列表请求失败。
    """
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    try:
        loaded: Any = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None

    memory_type = loaded.get("type")
    if not isinstance(memory_type, str) or memory_type not in MEMORY_TYPES:
        return None
    return {
        "name": _optional_text(loaded.get("name")),
        "description": _optional_text(loaded.get("description")),
        "type": memory_type,
    }


def _optional_text(value: object) -> str | None:
    """Optional text field in frontmatter: non-strings or blanks are treated as not set. / frontmatter 里的可选文本字段：非字符串或空白一律当作没写。"""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


@dataclass(frozen=True)
class AgentMemoryStore:
    """Read/write facade for one memory directory. ``directory`` is derived by ``lib.agent.agent_memory_paths``.

    ``project_dir`` is given only for project memory: after a successful write, delete or clear, the project's last activity time is advanced.

    一个记忆目录的读写门面。``directory`` 由 ``lib.agent.agent_memory_paths`` 派生。

    ``project_dir`` 只在项目记忆上给出：写、删与清空成功后推进该项目的最近活动时间。
    """

    directory: Path
    project_dir: Path | None = None

    @classmethod
    def for_project(cls, project_dir: Path) -> AgentMemoryStore:
        """Read/write facade for project memory. / 项目记忆的读写门面。"""
        return cls(project_memory_dir(project_dir), project_dir=project_dir)

    def overview(self) -> dict[str, Any]:
        """List response: directory path, index stats and all visible memory entries.

        ``path`` is the absolute server-side path, so the creator can open the memory directory directly in a file
        manager; it is a path, not UI copy, so it does not go through i18n.

        An entry may be deleted between enumeration and per-item reading (a delete or clear in another tab); that entry
        is skipped and the rest are listed as usual -- one file disappearing should not make the whole memory page error.

        列表响应：目录路径、索引统计与全部可见记忆条目。

        ``path`` 是服务端绝对路径，供创作者在文件管理器里直接打开记忆目录；它是路径
        而不是提示文案，因此不进 i18n。

        枚举与逐项读取之间条目可能已被删除（另一个标签页的删除或清空），该条目跳过，
        其余照常列出——单个文件的消失不该让整个记忆页报错。
        """
        index = {"exists": False, "line_count": 0, "byte_size": 0, "over_limit": False}
        files: list[dict[str, Any]] = []
        for path in self._visible_paths():
            if path.name == INDEX_FILENAME:
                index = _index_stats(path)
                continue
            stat = _stat_or_none(path)
            if stat is None:
                continue
            files.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                    "frontmatter": parse_memory_frontmatter(_read_bytes_or_empty(path)),
                }
            )
        return {"path": str(self.directory), "index": index, "files": files}

    def read(self, filename: str) -> bytes:
        """Read the raw bytes of a single memory file.

        Bytes are returned as is without decoding first: memory files are written freely by the Agent, so when non-UTF-8
        bytes sneak in, the body can still be returned for the creator to fix, rather than the whole read path degrading to 500.

        读取单个记忆文件的原始字节。

        原样回字节而不先解码：记忆文件由 Agent 自由书写，混入非 UTF-8 字节时正文照样
        能回给创作者去修，而不是整条读路径退化成 500。
        """
        path = self._resolve(filename)
        if not path.is_file():
            raise NotFoundError("memory_file_not_found", filename=filename)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise NotFoundError("memory_file_not_found", filename=filename) from exc

    def write(self, filename: str, content: bytes) -> None:
        """Idempotent upsert: create the directory if missing; reject when the body exceeds :data:`MAX_FILE_BYTES`.

        If the target name is occupied by a directory, reject as an invalid file name, consistent with how read, delete
        and list treat a same-named directory: the memory directory is writable by the Agent, and after it drops a
        ``notes.md/`` subdirectory ``os.replace`` would raise ``IsADirectoryError``; the creator could neither save nor
        delete it in the file cabinet, leaving only clearing the whole directory.

        幂等 upsert：目录不存在则创建，正文超过 :data:`MAX_FILE_BYTES` 时拒绝。

        目标名被目录占用时按非法文件名拒绝，与读、删、列表对同名目录的处置一致：
        记忆目录对 Agent 可写，落一个 ``notes.md/`` 子目录后 ``os.replace`` 会抛
        ``IsADirectoryError``，创作者在文件柜里既存不进也删不掉，只剩清空整个目录。
        """
        path = self._resolve(filename, ensure_directory=True)
        if path.is_dir():
            raise BadRequestError("memory_invalid_filename", filename=filename)
        if len(content) > MAX_FILE_BYTES:
            raise BadRequestError("memory_file_too_large", filename=filename, limit_kib=MAX_FILE_BYTES // 1024)
        atomic_write_bytes(path, content)
        self._record_activity()

    def delete(self, filename: str) -> None:
        """Delete a single memory file; the index ``MEMORY.md`` can be deleted too. / 删除单个记忆文件；索引 ``MEMORY.md`` 同样可删。"""
        path = self._resolve(filename)
        if not path.is_file():
            raise NotFoundError("memory_file_not_found", filename=filename)
        try:
            if self.project_dir is None:
                path.unlink()  # hard-delete: out-of-scope -- user memory does not belong to any project / 用户记忆不属于任何项目
            else:
                rel = self._project_rel(self.project_dir, filename)
                with project_metadata_lock(self.project_dir):
                    project_trash.record_deletion(self.project_dir, {"kind": "memory", "path": rel}, [rel])
        except FileNotFoundError as exc:
            raise NotFoundError("memory_file_not_found", filename=filename) from exc
        self._record_activity()

    def clear(self) -> None:
        """Clear the whole memory directory, then recreate it empty without generating an empty index.

        The index is rebuilt by the Agent when it writes its first memory; pre-creating an empty ``MEMORY.md`` would only
        make the list show a 0-line index, indistinguishable from "nothing there".

        清空整个记忆目录后重建空目录，不生成空索引。

        索引由 Agent 在写第一条记忆时重建；预置一个空 ``MEMORY.md`` 只会让列表显示
        一个 0 行的索引，与「什么都没有」不可区分。
        """
        if self.project_dir is not None and self.directory.is_dir() and not self.directory.is_symlink():
            with project_metadata_lock(self.project_dir):
                files = [
                    self._project_rel(self.project_dir, path.relative_to(self.directory).as_posix())
                    for path in sorted(self.directory.rglob("*"))
                    if path.is_file() and not path.is_symlink()
                ]
                if files:
                    obj = {"kind": "memory", "path": self.directory.relative_to(self.project_dir).as_posix()}
                    project_trash.record_deletion(self.project_dir, obj, files)
        if self.project_dir is None:
            # hard-delete: out-of-scope -- user memory does not belong to any project / 用户记忆不属于任何项目
            shutil.rmtree(self.directory, ignore_errors=True)
        # Project memory can be written concurrently by the Agent; the directory is kept after moving to the trash, to avoid hard-deleting content written after enumeration.
        # 项目记忆可被 Agent 并发写入；回收后保留目录，避免硬删枚举之后新写入的内容。
        self.directory.mkdir(parents=True, exist_ok=True)
        self._record_activity()

    def _project_rel(self, project_dir: Path, name: str) -> str:
        return f"{self.directory.relative_to(project_dir).as_posix()}/{name}"

    def _record_activity(self) -> None:
        if self.project_dir is not None:
            record_project_activity(self.project_dir)

    def _visible_paths(self) -> list[Path]:
        """Visible memory files in the directory sorted by name: top-level, valid file name, real files that don't escape the directory.

        Each item goes through ``try_safe_join`` (realpath semantics) rather than just ``is_file()``: a symlink pointing
        outside the directory is true under ``is_file()`` but unreadable, and listing it would give the creator an entry that
        always 404s when opened. Subdirectories and non-``.md`` entries are kept out by the file name rules.

        目录内按名称排序的可见记忆文件：顶层、合法文件名、未逃出目录的真实文件。

        逐项过 ``try_safe_join``（realpath 语义）而不是只判 ``is_file()``：指向目录外的
        symlink 在 ``is_file()`` 下为真、却读不出来，列出它等于给创作者一个点开必然
        404 的条目。子目录与非 ``.md`` 条目由文件名规则挡在外面。
        """
        try:
            names = sorted(entry.name for entry in self.directory.iterdir())
        except OSError:
            return []
        resolved: list[Path] = []
        for name in names:
            if not is_valid_memory_filename(name):
                continue
            path = try_safe_join(self.directory, name, require_file=True)
            if path is not None:
                resolved.append(path)
        return resolved

    def _resolve(self, filename: str, *, ensure_directory: bool = False) -> Path:
        """Resolve an untrusted ``filename`` into an absolute path within the directory.

        File name rules are checked first, then ``safe_join``: the former is a product rule (top-level ``*.md``), the
        latter an out-of-bounds fallback; both map to the same ``memory_invalid_filename`` -- for the creator "this file
        name can't be used" is the same thing.

        把不可信的 ``filename`` 解析为目录内的绝对路径。

        文件名规则先判，``safe_join`` 再判：前者是产品规则（顶层 ``*.md``），后者是
        越界兜底，两者都归到同一个 ``memory_invalid_filename``——对创作者而言
        「这个文件名不能用」是同一件事。
        """
        if not is_valid_memory_filename(filename):
            raise BadRequestError("memory_invalid_filename", filename=filename)
        if ensure_directory:
            self.directory.mkdir(parents=True, exist_ok=True)
        try:
            return safe_join(self.directory, filename)
        except PathTraversalError as exc:
            raise BadRequestError("memory_invalid_filename", filename=filename) from exc


def _index_stats(path: Path) -> dict[str, Any]:
    """Index line / byte counts and over-limit check, using the same rules as session assembly truncation.

    If it can't be read or isn't UTF-8 it counts as zero: assembly can't read such an index either, and reporting its
    on-disk size would make the file cabinet show an index the Agent can't actually get. Over-limit is only a hint; writes
    and reads are unaffected.

    索引的行数 / 字节数与超限判定，口径同会话装配的截断。

    读不出来或不是 UTF-8 时统计为零：装配也读不进这样的索引，报出它在磁盘上的
    规模会让文件柜显示一份 Agent 其实拿不到的索引。超限只是提示，写入与读取都不受影响。
    """
    stats = memory_index_stats(_read_text_or_empty(path))
    return {
        "exists": True,
        "line_count": stats.line_count,
        "byte_size": stats.byte_size,
        "over_limit": stats.over_limit,
    }


def _stat_or_none(path: Path) -> os.stat_result | None:
    """Get the entry's stat; returns ``None`` if it vanished after enumeration or can't be read. / 取条目的 stat；枚举之后已消失或读不到的返回 ``None``。"""
    try:
        return path.stat()
    except OSError:
        return None


def _read_bytes_or_empty(path: Path) -> bytes:
    """Unreadable files are treated as empty: the list endpoint does not fail as a whole because of one file's I/O fault.

    读不出来的文件按空内容处理：列表接口不因单个文件的 I/O 故障整体失败。"""
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _read_text_or_empty(path: Path) -> str:
    """Read text as UTF-8; unreadable or undecodable files are treated as empty. / 按 UTF-8 读文本；读不出来或解码失败一律按空内容处理。"""
    try:
        return _read_bytes_or_empty(path).decode("utf-8")
    except UnicodeDecodeError:
        return ""
