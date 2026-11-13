"""Answers of read-only project endpoints, cached by the exact state of the project files they are computed from.

The Edit view's reads (preview media, the timeline readout with its issues, the episode's edit overview) rebuild every clip's
presentation from the project, its version records, the artifact manifest, the episode scripts and the timelines.
For a long project that re-parses large JSON files several times per clip on every request: seconds per endpoint,
and, being CPU work, it holds up the server's other requests (a page firing five of them waited 9–30 s).

A cached answer is reused only while none of those files changed: their stat identities (device, inode, size, mtime
in ns, as for file digests) form the key, so a regeneration, upload, restore, script or timeline edit, or a new
render (all rewrite one of them) recomputes. Media files themselves change only together with the version records. Files changed within
the digest cache's racy window make the state uncacheable for that moment.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from pathlib import Path
from typing import TypeVar

from lib.artifacts.artifact_manifest import MANIFEST_FILENAME
from lib.artifacts.rendered_artifact import RENDERS_DIRNAME
from lib.edit_timeline.store import EDIT_TIMELINES_DIRNAME
from lib.infra.content_digest import FILE_DIGEST_RACY_WINDOW_NS, file_identity

T = TypeVar("T")

#: Files a project's presentations and timelines are computed from (relative to the project directory).
_STATE_FILES = ("project.json", "versions/versions.json", MANIFEST_FILENAME)
#: Directories (and file patterns, searched recursively) that count too: episode scripts, edit timelines, and the
#: rendered final cuts (the episode overview reports which renders exist and are out of date).
_STATE_DIRS = (("scripts", "*.json"), (EDIT_TIMELINES_DIRNAME, "*.json"), (RENDERS_DIRNAME, "final_cut.*.mp4"))


def project_state_identity(project_dir: Path) -> tuple[Hashable, ...] | None:
    """The stat identities of the files a project's read answers depend on; ``None`` (don't cache) when the project
    directory doesn't exist or one of the files changed so recently that a cached answer could miss a write in the
    same mtime tick."""

    if not project_dir.is_dir():
        return None
    now = time.time_ns()
    parts: list[Hashable] = []
    paths = [project_dir / name for name in _STATE_FILES]
    for directory, pattern in _STATE_DIRS:
        root = project_dir / directory
        if root.is_dir():
            paths.extend(sorted(root.rglob(pattern)))
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            parts.append((str(path), None))
            continue
        if now - stat.st_mtime_ns < FILE_DIGEST_RACY_WINDOW_NS:
            return None
        parts.append((str(path), file_identity(stat)))
    return tuple(parts)


class ProjectStateCache:
    """Process-wide LRU of answers keyed by (request key, project state identity). Thread-safe."""

    def __init__(self, capacity: int = 256) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[tuple[Hashable, ...], object] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[Hashable, ...]) -> object | None:
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, key: tuple[Hashable, ...], value: object) -> None:
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    async def get_or_compute(
        self,
        project_dir: Path | None,
        request_key: tuple[Hashable, ...],
        compute: Callable[[], Awaitable[T]],
        identity: Callable[[Path], tuple[Hashable, ...] | None] = project_state_identity,
    ) -> T:
        """The cached answer when the project's files are as they were when it was computed; otherwise compute it,
        and keep it only if the files did not change while it was being computed. ``project_dir`` ``None``: no
        caching, just compute."""

        import asyncio

        if project_dir is None:
            return await compute()
        before = await asyncio.to_thread(identity, project_dir)
        if before is not None:
            hit = self.get((*request_key, before))
            if hit is not None:
                return hit  # type: ignore[return-value]
        value = await compute()
        if before is not None:
            after = await asyncio.to_thread(identity, project_dir)
            if after == before:
                self.put((*request_key, before), value)
        return value


#: Shared by the presentation and timeline read services.
PROJECT_STATE_CACHE = ProjectStateCache()


def reset_project_state_cache_for_tests() -> None:
    """Empty the shared cache (test isolation)."""

    PROJECT_STATE_CACHE.clear()
