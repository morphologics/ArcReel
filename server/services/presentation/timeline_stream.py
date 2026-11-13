"""The Edit view's gapless stream: an edit timeline rendered as ready-to-stream pieces, played back to back through one
``<video>`` (Media Source Extensions) instead of switching between two video elements at every cut.

One piece per segment of the final-cut render plan (hard cut to hard cut; dissolves are blended inside a segment),
rendered by the final-cut code on a smaller canvas (``stream_profile``) with identical encoder settings and without
B-frames, so the browser's decoder runs through the cuts without restarting and every piece starts exactly at its
first frame. Audio per piece is the final cut's mix of the segment's own clips; narration and BGM stay with the player.

Pieces are named by a hash of everything that shapes them (source file, trims, holds, transitions, volumes, canvas,
encoder version), so an edit re-renders only the segments it touched and reuses the rest. They live under the
timeline's renders directory (``renders/episode_N/<timeline>/stream/``): served by the files endpoint, left out of
project archives, removed with the timeline. Building runs in the background, one ffmpeg at a time per server; the
endpoint reports which pieces are ready, and the player keeps using its two-video playback until it has them.

Pieces are built ahead, not only when someone opens the Edit view: a sweep (:func:`start_stream_prebuild`) goes
through every timeline of every project once a minute and builds what is missing at idle CPU priority (only CPU
nothing else wants). A viewer's request goes first: its build runs at a normal-ish priority (nice 10) and from the
piece at the viewer's playhead.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from lib.artifacts.rendered_artifact import timeline_renders_dir
from lib.final_cut.errors import FinalCutError
from lib.final_cut.ffmpeg_render import render_stream_piece
from lib.final_cut.render_plan import RenderPlan, RenderSegment, stream_profile
from lib.edit_timeline.store import EditTimelineStore
from lib.final_cut.service import FinalCutService
from lib.infra.ffmpeg import ffmpeg_executable
from lib.project.project_manager import ProjectManager
from server.services.presentation.project_state_cache import PROJECT_STATE_CACHE

logger = logging.getLogger(__name__)

#: Bump when the piece encoding changes: every piece gets a new name and is rebuilt.
STREAM_ENCODING_VERSION = 5
STREAM_DIRNAME = "stream"
#: The stream's codecs for ``MediaSource.addSourceBuffer`` (H.264 High, AAC-LC); browsers read the exact profile and
#: level from each piece's own header.
STREAM_CODECS = 'video/mp4; codecs="avc1.640028, mp4a.40.2"'

#: One ffmpeg at a time per server process: the members' studios share the box's CPU.
_BUILD_SLOTS = asyncio.Semaphore(1)
#: Piece builds a viewer waits for yield the CPU to the server's answers (an edit's rebuild competing at full
#: priority made the Edit view's own requests take 5–13 s).
_VIEWER_NICENESS = 10
#: A build counts as watched this long after the Edit view last asked for the timeline (it polls every 2.5 s).
_WATCHED_SECONDS = 30.0
#: The prebuild sweep: first pass this long after start (a short-lived server, e.g. in tests, never sweeps), then
#: one pass per interval.
PREBUILD_FIRST_DELAY_SECONDS = 60.0
PREBUILD_INTERVAL_SECONDS = 60.0


@dataclass
class _Job:
    """What one timeline's build works from; the newest request replaces the plan and moves the playhead."""

    plan: RenderPlan
    keys: list[str]
    stream_dir: Path
    focus: float = 0.0
    watched_until: float = 0.0


#: Builds by (project, timeline): a poll while one runs updates its job instead of starting another.
_JOBS: dict[tuple[str, str], _Job] = {}
_BUILDS: dict[tuple[str, str], asyncio.Task[None]] = {}


def _spawner(watched: bool) -> Callable[..., Awaitable[asyncio.subprocess.Process]]:
    """Watched: nice 10. Background: idle scheduling (Linux ``SCHED_IDLE``), else the lowest nice value."""

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        # no such calls on Windows / macOS; a process that already ended is fine
        with contextlib.suppress(AttributeError, OSError):
            if watched:
                os.setpriority(os.PRIO_PROCESS, process.pid, _VIEWER_NICENESS)
            else:
                try:
                    os.sched_setscheduler(process.pid, os.SCHED_IDLE, os.sched_param(0))
                except (AttributeError, OSError):
                    os.setpriority(os.PRIO_PROCESS, process.pid, 19)
        return process

    return spawn


def _next_missing(plan: RenderPlan, keys: list[str], present: set[str], focus: float) -> int | None:
    """The segment to build next: the first missing one at or after ``focus`` seconds, else the first missing one."""
    fps = plan.profile.fps
    missing = [index for index, key in enumerate(keys) if key not in present]
    if not missing:
        return None
    ahead = [index for index in missing if focus * fps < plan.segments[index].start_frame + plan.segments[index].frames]
    return (ahead or missing)[0]


class StreamPiece(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    start: float
    duration: float
    #: Project-relative path once rendered; ``None`` while it is being prepared.
    path: str | None
    key: str


class TimelineStream(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool
    #: Why there is no stream (``final_cut_blocked``, ``final_cut_empty``, …) when ``available`` is false.
    reason: str | None = None
    timeline_id: str
    revision: int | None = None
    duration: float | None = None
    codecs: str = STREAM_CODECS
    ready: bool = False
    pieces: tuple[StreamPiece, ...] = ()


def _piece_key(segment: RenderSegment, plan: RenderPlan) -> str:
    spec = {
        "encoding": STREAM_ENCODING_VERSION,
        "profile": dataclasses.asdict(plan.profile),
        # position-independent: an edit earlier in the timeline moves a piece without changing it
        "segment": dataclasses.asdict(segment.at_zero()),
    }
    canonical = json.dumps(spec, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class TimelineStreamService:
    def __init__(self, projects: ProjectManager, final_cuts: FinalCutService | None = None) -> None:
        self._projects = projects
        self._final_cuts = final_cuts or FinalCutService(projects)

    async def stream(
        self, project_name: str, timeline_id: str, *, at: float | None = None, watched: bool = True
    ) -> TimelineStream:
        """The latest revision's stream: its pieces and which are ready. Starts building the missing ones, from the
        piece at ``at`` seconds (the viewer's playhead) on; ``watched`` False for the background sweep."""
        project_dir = await asyncio.to_thread(self._projects.get_project_path, project_name)
        try:
            # the plan only changes with the project's files (cheap on every poll)
            episode, revision, plan = await PROJECT_STATE_CACHE.get_or_compute(
                project_dir,
                ("stream-plan", project_name, timeline_id),
                lambda: self._final_cuts.stream_plan(project_name, timeline_id, stream_profile),
            )
        except FinalCutError as exc:
            return TimelineStream(available=False, reason=exc.code, timeline_id=timeline_id)
        stream_dir = project_dir / timeline_renders_dir(episode, timeline_id) / STREAM_DIRNAME
        relative_dir = f"{timeline_renders_dir(episode, timeline_id)}/{STREAM_DIRNAME}"
        fps = plan.profile.fps
        keys = [_piece_key(segment, plan) for segment in plan.segments]
        present = await asyncio.to_thread(lambda: {key for key in keys if (stream_dir / f"{key}.mp4").is_file()})
        pieces = tuple(
            StreamPiece(
                index=index,
                start=segment.start_frame / fps,
                duration=segment.frames / fps,
                path=f"{relative_dir}/{key}.mp4" if key in present else None,
                key=key,
            )
            for index, (segment, key) in enumerate(zip(plan.segments, keys, strict=True))
        )
        ready = len(present) == len(keys)
        if not ready:
            _ensure_build((project_name, timeline_id), plan, keys, stream_dir, at=at, watched=watched)
        return TimelineStream(
            available=True,
            timeline_id=timeline_id,
            revision=revision,
            duration=plan.total_frames / fps,
            ready=ready,
            pieces=pieces,
        )


def _ensure_build(
    build: tuple[str, str],
    plan: RenderPlan,
    keys: list[str],
    stream_dir: Path,
    *,
    at: float | None,
    watched: bool,
) -> asyncio.Task[None]:
    job = _JOBS.get(build)
    if job is None:
        job = _JOBS[build] = _Job(plan=plan, keys=keys, stream_dir=stream_dir)
    else:
        job.plan, job.keys, job.stream_dir = plan, keys, stream_dir
    if at is not None:
        job.focus = at
    if watched:
        job.watched_until = time.monotonic() + _WATCHED_SECONDS
    running = _BUILDS.get(build)
    if running is not None and not running.done():
        return running
    task = asyncio.create_task(_build(build))
    _BUILDS[build] = task

    def finished(done: asyncio.Task[None]) -> None:
        if _BUILDS.get(build) is done:
            _BUILDS.pop(build, None)
            _JOBS.pop(build, None)

    task.add_done_callback(finished)
    return task


async def _build(build: tuple[str, str]) -> None:
    """Render the job's missing pieces one by one, each written atomically, always taking the next from the job's
    latest plan and playhead (an edit or a seek while building changes them); then drop pieces no longer wanted."""
    stream_dir: Path | None = None
    try:
        ffmpeg = await asyncio.to_thread(ffmpeg_executable)
        while True:
            job = _JOBS[build]
            stream_dir = job.stream_dir
            await asyncio.to_thread(stream_dir.mkdir, parents=True, exist_ok=True)
            keys, folder = job.keys, stream_dir
            present = await asyncio.to_thread(lambda: {key for key in keys if (folder / f"{key}.mp4").is_file()})
            index = _next_missing(job.plan, job.keys, present, job.focus)
            if index is None:
                break
            segment, key, plan = job.plan.segments[index], job.keys[index], job.plan
            async with _BUILD_SLOTS:
                watched = time.monotonic() < job.watched_until
                workspace = stream_dir / f".work-{key}"
                await asyncio.to_thread(workspace.mkdir, exist_ok=True)
                try:
                    partial = workspace / "piece.mp4"
                    await render_stream_piece(ffmpeg, segment, plan, partial, workspace, spawn=_spawner(watched))
                    await asyncio.to_thread(os.replace, partial, stream_dir / f"{key}.mp4")
                finally:
                    await asyncio.to_thread(shutil.rmtree, workspace, True)
        job = _JOBS[build]
        wanted = {f"{key}.mp4" for key in job.keys}
        final_dir = job.stream_dir

        def prune() -> None:
            for entry in final_dir.iterdir():
                if entry.is_file() and entry.suffix == ".mp4" and entry.name not in wanted:
                    entry.unlink(missing_ok=True)  # hard-delete: cache -- an old revision's stream piece

        await asyncio.to_thread(prune)
    except Exception:
        # the player keeps its two-video playback; the next poll or sweep starts a new build
        logger.exception("edit timeline stream build failed in %s", stream_dir)


async def _prebuild_loop(projects: ProjectManager) -> None:
    """Build every timeline's missing pieces ahead, one timeline at a time, at idle priority (see module doc)."""
    service = TimelineStreamService(projects)
    await asyncio.sleep(PREBUILD_FIRST_DELAY_SECONDS)
    while True:
        try:
            names = await asyncio.to_thread(projects.list_projects)
        except Exception:
            logger.warning("stream prebuild: listing projects failed, next pass later", exc_info=True)
            names = []
        for name in names:
            try:
                documents = await asyncio.to_thread(EditTimelineStore(projects, name).list_documents)
            except Exception:
                logger.warning("stream prebuild: listing timelines of %s failed", name, exc_info=True)
                continue
            for document in documents:
                try:
                    await service.stream(name, document.id, watched=False)
                except Exception:
                    logger.warning("stream prebuild: %s / %s failed", name, document.id, exc_info=True)
                    continue
                # one timeline at a time: wait for its build before planning the next
                running = _BUILDS.get((name, document.id))
                if running is not None:
                    await asyncio.shield(running)
        await asyncio.sleep(PREBUILD_INTERVAL_SECONDS)


def start_stream_prebuild(projects: ProjectManager) -> asyncio.Task[None]:
    """Start the prebuild sweep (cancelled with :func:`stop_stream_prebuild` at shutdown)."""
    return asyncio.get_running_loop().create_task(_prebuild_loop(projects), name="edit-stream-prebuild")


async def stop_stream_prebuild(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


__all__ = [
    "STREAM_CODECS",
    "StreamPiece",
    "TimelineStream",
    "TimelineStreamService",
    "start_stream_prebuild",
    "stop_stream_prebuild",
]
