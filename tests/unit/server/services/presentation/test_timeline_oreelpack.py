"""An edit timeline as an OpenReel project backup (.oreelpack) for the experimental "Open in OpenReel"."""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from lib.edit_timeline.readout import EditTimelineReadout
from server.services.presentation import timeline_oreelpack
from server.services.presentation.timeline_oreelpack import TimelineOreelPackService
from server.services.presentation.timeline_preview import TimelinePreviewMedia


def _clip(clip_id: str, unit: str, start: float, duration: float, **fields: Any) -> dict[str, Any]:
    return {
        "id": clip_id, "unit_id": unit, "status": "ready", "start": start, "duration": duration, "video_version": 1,
        "source_duration": 6.0, "trim": None, "source_volume": 1.0, "hold": 0.0, "carries_narration": False,
        "narration": None, "reason": None, "transition_to_next": None, **fields,
    }


READOUT = EditTimelineReadout.model_validate({
    "timeline": {"id": "tl-1", "name": "Full cut", "episode": 3},
    "revision": 4,
    "latest_revision": 4,
    "duration": 9.5,
    "clips": [
        _clip("c1", "E3S01", 0.0, 4.0, trim={"source_in": 1.0, "source_out": 5.0, "basis_version": 1},
              transition_to_next={"type": "dissolve", "duration": 0.5}),
        _clip("c2", "E3S02", 4.0, 3.5, hold=1.5, transition_to_next={"type": "fade_black", "duration": 0.6}),
        _clip("c3", "E3S03", 7.5, 2.0, source_volume=0.4),
    ],
    "bgm": [{"id": "b1", "bgm_id": "bgm-1", "name": "Rain", "start": 0.0, "end": 9.5, "source_in": 2.0,
             "source_out": 11.5, "volume": 0.5, "fade_in": 1.0, "fade_out": 1.0}],
    "issues": [],
})
MEDIA = TimelinePreviewMedia.model_validate({
    "timeline_id": "tl-1",
    "revision": 4,
    "narration": "without_narration",
    "units": [
        {"unit_id": "E3S01", "provider_audio": True, "narration_audio": None, "subtitles_follow_narration": False,
         "subtitles": [{"start": 1.5, "duration": 2.0, "text": "Hello"}]},
        {"unit_id": "E3S02", "provider_audio": False, "narration_audio": None, "subtitles_follow_narration": False,
         "subtitles": []},
        {"unit_id": "E3S03", "provider_audio": True, "narration_audio": None, "subtitles_follow_narration": False,
         "subtitles": []},
    ],
    "bgm": [{"bgm_id": "bgm-1", "path": "bgm/bgm-1.mp3", "gain": 1.6}],
})


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TimelineOreelPackService:
    project = tmp_path / "demo"
    (project / "videos").mkdir(parents=True)
    (project / "bgm").mkdir()
    for unit in ("E3S01", "E3S02", "E3S03"):
        (project / "videos" / f"scene_{unit}.mp4").write_bytes(f"mp4:{unit}".encode())
    (project / "bgm" / "bgm-1.mp3").write_bytes(b"mp3")

    class _Projects:
        def get_project_path(self, name: str) -> Path:
            return tmp_path / name

        def load_project(self, name: str) -> dict:
            return {"title": "Demo", "aspect_ratio": "16:9"}

    async def read(self, project_name, timeline_id, *, revision=None):
        return READOUT

    async def media(self, project_name, timeline_id):
        return MEDIA

    async def last_frame(video: Path, at: float, output: Path) -> None:
        await asyncio.to_thread(output.write_bytes, f"jpg:{video.name}@{at:.2f}".encode())

    monkeypatch.setattr(timeline_oreelpack.EditTimelineService, "read", read)
    monkeypatch.setattr(timeline_oreelpack.TimelinePreviewService, "media", media)
    monkeypatch.setattr(timeline_oreelpack, "_last_frame", last_frame)
    return TimelineOreelPackService(_Projects())  # type: ignore[arg-type]


async def _unpack(service: TimelineOreelPackService) -> tuple[dict, dict, bytes, str]:
    pack = await service.build("demo", "tl-1")
    data = b"".join([chunk async for chunk in pack.chunks()])
    assert len(data) == pack.size
    assert data[:12] == b"OREELPACK01\n"
    length = struct.unpack_from("<I", data, 12)[0]
    manifest = json.loads(data[16 : 16 + length])
    return manifest, json.loads(manifest["projectJson"]), data[16 + length :], pack.filename


async def test_the_pack_follows_openreels_backup_format(service: TimelineOreelPackService) -> None:
    manifest, project_file, media, filename = await _unpack(service)

    assert manifest["version"] == 1 and project_file["version"] == "1.2.0"
    project = project_file["project"]
    assert project["settings"]["width"] == 1920 and project["settings"]["frameRate"] == 30
    ids = [item["id"] for item in project["mediaLibrary"]["items"]]
    assert [asset["id"] for asset in manifest["assets"]] == ids
    assert sum(asset["size"] for asset in manifest["assets"]) == len(media)
    assert filename == "demo-ep3-tl-1-r4.oreelpack"
    clips = [clip for track in project["timeline"]["tracks"] for clip in track["clips"]]
    assert all(clip["mediaId"] in ids for clip in clips)


async def test_clips_holds_transitions_subtitles_and_bgm_map_to_openreel(service: TimelineOreelPackService) -> None:
    _, project_file, _, _ = await _unpack(service)
    project = project_file["project"]
    video, music = project["timeline"]["tracks"]
    items = {item["id"]: item for item in project["mediaLibrary"]["items"]}
    kinds = [items[clip["mediaId"]]["type"] for clip in video["clips"]]

    # c1 trimmed, c2 then its 1.5 s hold as a still, c3; back to back
    assert kinds == ["video", "video", "image", "video"]
    starts = [(clip["startTime"], clip["duration"]) for clip in video["clips"]]
    assert starts == [(0.0, 4.0), (4.0, 2.0), (6.0, 1.5), (7.5, 2.0)]
    assert (video["clips"][0]["inPoint"], video["clips"][0]["outPoint"]) == (1.0, 5.0)
    assert video["clips"][1]["volume"] == 0.0   # provider sound off
    assert video["clips"][3]["volume"] == 0.4
    assert video["clips"][0]["metadata"]["arcreel"]["clip_id"] == "c1"
    hold = video["clips"][2]["metadata"]["arcreel"]
    assert hold == {"clip_id": "c2", "unit_id": "E3S02", "kind": "end_hold", "hold": 1.5}

    # the dissolve joins c1 → c2; c2's fade to black starts from its hold still
    joins = [(t["type"], t["clipAId"], t["clipBId"], t["duration"]) for t in video["transitions"]]
    ids = [clip["id"] for clip in video["clips"]]
    assert joins == [("crossfade", ids[0], ids[1], 0.5), ("dipToBlack", ids[2], ids[3], 0.6)]

    # c1's cue at 1.5 s of its source, trimmed from 1.0 s: 0.5 s on the timeline
    cues = [(s["text"], s["startTime"], s["endTime"]) for s in project["timeline"]["subtitles"]]
    assert cues == [("Hello", 0.5, 2.5)]

    assert music["role"] == "music"
    bgm = music["clips"][0]
    assert (bgm["inPoint"], bgm["duration"], bgm["volume"]) == (2.0, 9.5, 0.8)
    assert bgm["fade"] == {"fadeIn": 1.0, "fadeOut": 1.0}
