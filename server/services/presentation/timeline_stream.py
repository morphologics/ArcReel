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
endpoint reports which pieces are ready, and the player keeps using its two-video playback until all are.
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
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from lib.artifacts.rendered_artifact import timeline_renders_dir
from lib.final_cut.errors import FinalCutError
from lib.final_cut.ffmpeg_render import render_stream_piece
from lib.final_cut.render_plan import RenderPlan, RenderSegment, stream_profile
from lib.final_cut.service import FinalCutService
from lib.infra.ffmpeg import ffmpeg_executable
from lib.project.project_manager import ProjectManager
from server.services.presentation.project_state_cache import PROJECT_STATE_CACHE

logger = logging.getLogger(__name__)

#: Bump when the piece encoding changes: every piece gets a new name and is rebuilt.
STREAM_ENCODING_VERSION = 3
STREAM_DIRNAME = "stream"
#: The stream's codecs for ``MediaSource.addSourceBuffer`` (H.264 High, AAC-LC); browsers read the exact profile and
#: level from each piece's own header.
STREAM_CODECS = 'video/mp4; codecs="avc1.640028, mp4a.40.2"'

#: One ffmpeg at a time per server process: the members' studios share the box's CPU.
_BUILD_SLOTS = asyncio.Semaphore(1)
#: Running builds by (project, timeline): a poll while one runs doesn't start another.
_BUILDS: dict[tuple[str, str], asyncio.Task[None]] = {}
#: Piece builds yield the CPU: on a busy box the server's answers and other work come first (an edit's rebuild
#: competing at full priority made the Edit view's own requests take 5–13 s).
_BUILD_NICENESS = 10


async def _spawn_low_priority(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(*args, **kwargs)
    with contextlib.suppress(AttributeError, OSError):  # no setpriority on Windows; a vanished process is fine
        os.setpriority(os.PRIO_PROCESS, process.pid, _BUILD_NICENESS)
    return process


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

    async def stream(self, project_name: str, timeline_id: str) -> TimelineStream:
        """The latest revision's stream: its pieces and which are ready. Starts building the missing ones."""
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
            self._ensure_build(project_name, timeline_id, plan, keys, stream_dir)
        return TimelineStream(
            available=True,
            timeline_id=timeline_id,
            revision=revision,
            duration=plan.total_frames / fps,
            ready=ready,
            pieces=pieces,
        )

    def _ensure_build(
        self, project_name: str, timeline_id: str, plan: RenderPlan, keys: list[str], stream_dir: Path
    ) -> None:
        build = (project_name, timeline_id)
        running = _BUILDS.get(build)
        if running is not None and not running.done():
            return
        task = asyncio.create_task(_build(plan, keys, stream_dir))
        _BUILDS[build] = task
        task.add_done_callback(lambda done: _BUILDS.pop(build, None) if _BUILDS.get(build) is done else None)


async def _build(plan: RenderPlan, keys: list[str], stream_dir: Path) -> None:
    """Render the missing pieces in order, each written atomically; then drop pieces no longer in the plan."""
    try:
        ffmpeg = await asyncio.to_thread(ffmpeg_executable)
        await asyncio.to_thread(stream_dir.mkdir, parents=True, exist_ok=True)
        for segment, key in zip(plan.segments, keys, strict=True):
            target = stream_dir / f"{key}.mp4"
            if await asyncio.to_thread(target.is_file):
                continue
            async with _BUILD_SLOTS:
                workspace = stream_dir / f".work-{key}"
                await asyncio.to_thread(workspace.mkdir, exist_ok=True)
                try:
                    partial = workspace / "piece.mp4"
                    await render_stream_piece(ffmpeg, segment, plan, partial, workspace, spawn=_spawn_low_priority)
                    await asyncio.to_thread(os.replace, partial, target)
                finally:
                    await asyncio.to_thread(shutil.rmtree, workspace, True)
        wanted = {f"{key}.mp4" for key in keys}

        def prune() -> None:
            for entry in stream_dir.iterdir():
                if entry.is_file() and entry.suffix == ".mp4" and entry.name not in wanted:
                    entry.unlink(missing_ok=True)  # hard-delete: cache -- an old revision's stream piece

        await asyncio.to_thread(prune)
    except Exception:
        # the player keeps its two-video playback; the next poll starts a new build
        logger.exception("edit timeline stream build failed in %s", stream_dir)


__all__ = ["STREAM_CODECS", "StreamPiece", "TimelineStream", "TimelineStreamService"]
