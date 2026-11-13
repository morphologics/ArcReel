"""Duration probes of files on disk are cached by the file's stat identity."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from lib.infra.media_probe import MediaProbe
from lib.speech import audio_utils


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    audio_utils.reset_probe_cache_for_tests()


def _old_file(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    old = time.time_ns() - 10 * 1_000_000_000
    os.utime(path, ns=(old, old))


async def test_probe_is_reused_until_the_file_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "clip.mp4"
    _old_file(clip, b"first")
    calls: list[Path] = []

    async def fake_probe(path: Path, **_kwargs: object) -> MediaProbe:
        calls.append(path)
        return MediaProbe(container_formats=frozenset({"mp4"}), streams=(), duration_seconds=float(len(calls)))

    monkeypatch.setattr(audio_utils, "probe_media", fake_probe)

    assert (await audio_utils._probe_or_none(clip)).duration_seconds == 1.0
    assert (await audio_utils._probe_or_none(clip)).duration_seconds == 1.0
    assert len(calls) == 1

    _old_file(clip, b"regenerated")   # new size → new identity
    assert (await audio_utils._probe_or_none(clip)).duration_seconds == 2.0


async def test_a_just_written_file_is_probed_every_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fresh")   # mtime = now: inside the racy window
    calls = 0

    async def fake_probe(path: Path, **_kwargs: object) -> MediaProbe:
        nonlocal calls
        calls += 1
        return MediaProbe(container_formats=frozenset({"mp4"}), streams=(), duration_seconds=1.0)

    monkeypatch.setattr(audio_utils, "probe_media", fake_probe)
    await audio_utils._probe_or_none(clip)
    await audio_utils._probe_or_none(clip)
    assert calls == 2
