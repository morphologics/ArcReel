"""The Edit view's gapless stream: pieces per render segment, built in the background, reused across edits."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from lib.edit_timeline.model import EditClip
from lib.final_cut.errors import FinalCutError
from lib.final_cut.render_plan import OutputProfile, RenderMedia, plan_render, render_clips, stream_profile
from server.services.presentation import timeline_stream
from server.services.presentation.project_state_cache import PROJECT_STATE_CACHE
from server.services.presentation.timeline_stream import TimelineStreamService

PROFILE = OutputProfile(width=1080, height=1920, fps=30)


def _plan(durations_us: list[int], *, trim_first_end_us: int | None = None):
    clips = [
        EditClip.model_validate(
            {"id": f"c{i + 1}", "unit_id": f"U{i}", "source_volume": 1.0}
            | (
                {"trim": {"in_us": 0, "out_us": trim_first_end_us, "basis_version": 1}}
                if i == 0 and trim_first_end_us is not None
                else {}
            )
        )
        for i in range(len(durations_us))
    ]
    media = {
        f"U{i}": RenderMedia(path=Path(f"/p/videos/u{i}.mp4"), video_version=1, duration_us=d, has_audio=True)
        for i, d in enumerate(durations_us)
    }
    return plan_render(render_clips(clips, media), stream_profile(PROFILE))


class _Projects:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get_project_path(self, name: str) -> Path:
        return self.root / name


class _FinalCuts:
    def __init__(self) -> None:
        self.plan: Any = None
        self.error: FinalCutError | None = None

    async def stream_plan(self, project_name: str, timeline_id: str, profile_for: Any):
        if self.error is not None:
            raise self.error
        return 1, 7, self.plan


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "demo").mkdir()
    rendered: list[int] = []

    async def fake_render(ffmpeg, segment, plan, output, workspace, **_: Any) -> None:
        rendered.append(segment.start_frame)
        await asyncio.to_thread(output.write_bytes, b"piece")

    monkeypatch.setattr(timeline_stream, "render_stream_piece", fake_render)
    monkeypatch.setattr(timeline_stream, "ffmpeg_executable", lambda: "ffmpeg")
    # every test computes its own plan: the cache must not answer from a previous test's project state
    monkeypatch.setattr(PROJECT_STATE_CACHE, "get_or_compute", lambda _dir, _key, compute: compute())
    final_cuts = _FinalCuts()
    service = TimelineStreamService(_Projects(tmp_path), final_cuts)  # type: ignore[arg-type]
    return service, final_cuts, rendered, tmp_path / "demo" / "renders" / "episode_1" / "tl-1" / "stream"


async def _settle() -> None:
    for task in list(timeline_stream._BUILDS.values()):
        await task


def test_stream_profile_scales_the_short_edge_and_keeps_fps() -> None:
    assert stream_profile(PROFILE) == OutputProfile(width=720, height=1280, fps=30)
    small = OutputProfile(width=640, height=360, fps=24)
    assert stream_profile(small) is small


async def test_first_ask_reports_pieces_pending_and_builds_them(setup) -> None:
    service, final_cuts, rendered, stream_dir = setup
    final_cuts.plan = _plan([2_000_000, 1_000_000])

    first = await service.stream("demo", "tl-1")
    assert first.available and not first.ready and first.revision == 7
    assert [piece.path for piece in first.pieces] == [None, None]
    assert [(piece.start, piece.duration) for piece in first.pieces] == [(0.0, 2.0), (2.0, 1.0)]
    assert first.duration == 3.0

    await _settle()
    second = await service.stream("demo", "tl-1")
    assert second.ready
    assert all(piece.path and piece.path.startswith("renders/episode_1/tl-1/stream/") for piece in second.pieces)
    assert sorted(entry.name for entry in stream_dir.iterdir()) == sorted(f"{p.key}.mp4" for p in second.pieces)
    assert rendered == [0, 60]


async def test_an_edit_rebuilds_only_the_touched_piece_and_prunes_the_old_one(setup) -> None:
    service, final_cuts, rendered, stream_dir = setup
    final_cuts.plan = _plan([2_000_000, 1_000_000])
    await service.stream("demo", "tl-1")
    await _settle()
    before = (await service.stream("demo", "tl-1")).pieces
    rendered.clear()

    final_cuts.plan = _plan([2_000_000, 1_000_000], trim_first_end_us=1_000_000)
    edited = await service.stream("demo", "tl-1")
    assert edited.pieces[0].key != before[0].key and edited.pieces[0].path is None
    assert edited.pieces[1].key == before[1].key and edited.pieces[1].path is not None
    assert edited.pieces[1].start == 1.0

    await _settle()
    assert rendered == [0]
    assert sorted(entry.name for entry in stream_dir.iterdir()) == sorted(f"{p.key}.mp4" for p in edited.pieces)


async def test_a_timeline_that_cannot_render_has_no_stream(setup) -> None:
    service, final_cuts, _, _ = setup
    final_cuts.error = FinalCutError("final_cut_empty", "nothing to render")

    stream = await service.stream("demo", "tl-1")

    assert not stream.available and stream.reason == "final_cut_empty" and stream.pieces == ()
