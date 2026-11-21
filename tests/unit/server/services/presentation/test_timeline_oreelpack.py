"""An edit timeline as an OpenReel project backup (.oreelpack) for the experimental "Open in OpenReel"."""

from __future__ import annotations

import asyncio
import json
import re
import struct
from pathlib import Path
from typing import Any

import pytest

from lib.edit_timeline.readout import EditTimelineReadout
from server.services.presentation import timeline_oreelpack
from server.services.presentation.timeline_oreelpack import TimelineOreelPackService
from server.services.presentation.timeline_preview import PreviewNarrationAudio, TimelinePreviewMedia


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
            return {"title": "Demo", "aspect_ratio": "16:9", "episodes": [{"episode": 3, "script_file": "scripts/episode_3.json"}]}

        def load_script_readonly(self, name: str, filename: str) -> dict:
            assert filename == "scripts/episode_3.json"
            return {"scenes": [
                {"scene_id": "E3S01", "scene_description": "A hallway", "image_prompt": {"composition": {"lighting": "dim"}}},
                {"scene_id": "E3S02", "scene_description": "A door", "scenes": ["Hallway"]},
            ]}

    async def read(self, project_name, timeline_id, *, revision=None):
        return READOUT

    async def media(self, project_name, timeline_id):
        return MEDIA

    async def last_frame(video: Path, at: float, output: Path) -> None:
        await asyncio.to_thread(output.write_bytes, f"jpg:{video.name}@{at:.2f}".encode())

    monkeypatch.setattr(timeline_oreelpack.EditTimelineService, "read", read)
    monkeypatch.setattr(timeline_oreelpack.TimelinePreviewService, "media", media)
    monkeypatch.setattr(timeline_oreelpack, "_last_frame", last_frame)
    return TimelineOreelPackService(_Projects())  # type: ignore[arg-type]  # test double: only the two methods the service calls


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
    assert re.fullmatch(r"demo-ep3-tl-1-r4-m[0-9a-f]{8}\.oreelpack", filename)
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


async def test_clips_carry_their_shot_from_the_episode_script(service: TimelineOreelPackService) -> None:
    _, project_file, _, _ = await _unpack(service)
    video = project_file["project"]["timeline"]["tracks"][0]
    first, second = video["clips"][0]["metadata"]["arcreel"], video["clips"][1]["metadata"]["arcreel"]
    assert first["episode"] == 3
    assert first["shot"]["image_prompt"]["composition"]["lighting"] == "dim"
    assert second["shot"]["scene_description"] == "A door"
    assert (first["shot_label"], second["shot_label"]) == ("Shot 1", "Shot 2 · Hallway")
    # E3S03 has no script entry: the clip keeps its link without a shot
    assert "shot" not in video["clips"][3]["metadata"]["arcreel"]


async def test_media_ids_are_stable_across_builds(service: TimelineOreelPackService) -> None:
    """The same file gets the same id in every pack (the editor reuses media it already holds)."""
    first, _, _, _ = await _unpack(service)
    second, _, _, _ = await _unpack(service)
    assert [a["id"] for a in first["assets"]] == [a["id"] for a in second["assets"]]
    assert all(a["id"].startswith("m") and len(a["id"]) == 25 for a in first["assets"])


async def test_cached_pack_serves_its_manifest_and_media(service: TimelineOreelPackService) -> None:
    timeline_oreelpack._CACHE.clear()
    pack = await service.cached("demo", "tl-1")
    assert await service.cached("demo", "tl-1") is pack  # the same build while cached
    manifest = json.loads(pack.manifest)
    assert [a["id"] for a in manifest["assets"]] == [a.id for a in pack.assets]
    asset = await service.media_file("demo", "tl-1", pack.assets[0].id)
    assert asset is not None and asset.path.is_file() and asset.size == pack.assets[0].size
    assert await service.media_file("demo", "tl-1", "m-not-in-this-pack") is None
    timeline_oreelpack._CACHE.clear()
    await asyncio.to_thread(pack.workdir.cleanup)


async def test_new_media_names_a_new_pack_at_the_same_revision(
    service: TimelineOreelPackService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YouReel (owner 2026-11-21): a video generated (or regenerated) since the last build makes no timeline revision;
    the media fingerprint in the filename and the cache key makes the pack newer all the same, while the same media
    keeps the same name and the cached build."""
    timeline_oreelpack._CACHE.clear()
    first = await service.cached("demo", "tl-1")
    again = await service.cached("demo", "tl-1")
    assert again is first and again.filename == first.filename

    # E3S03 had no video: now it has one (still revision 4)
    missing = READOUT.model_copy(update={"clips": (
        *READOUT.clips[:2], READOUT.clips[2].model_copy(update={"status": "video_missing", "video_version": None}),
    )})
    regenerated = READOUT.model_copy(update={"clips": (
        *READOUT.clips[:2], READOUT.clips[2].model_copy(update={"video_version": 2}),
    )})
    current = {"readout": missing}

    async def read(self, project_name, timeline_id, *, revision=None):
        return current["readout"]

    monkeypatch.setattr(timeline_oreelpack.EditTimelineService, "read", read)
    gap = await service.cached("demo", "tl-1")
    current["readout"] = READOUT
    filled = await service.cached("demo", "tl-1")
    current["readout"] = regenerated
    newer = await service.cached("demo", "tl-1")

    names = [gap.filename, filled.filename, newer.filename]
    assert all(name.startswith("demo-ep3-tl-1-r4-m") for name in names)
    assert len(set(names)) == 3
    assert filled is first  # the same media as the first build: the cached pack again
    assert len({key[3] for key in timeline_oreelpack._CACHE}) == 3
    assert json.loads(gap.manifest)["assets"] != json.loads(newer.manifest)["assets"]
    # a medium of any cached pack of the timeline is still served
    assert await service.media_file("demo", "tl-1", gap.assets[0].id) is not None
    timeline_oreelpack._CACHE.clear()
    for pack in (gap, filled, newer):
        await asyncio.to_thread(pack.workdir.cleanup)


def test_the_fingerprint_follows_narration_and_bgm(tmp_path: Path) -> None:
    (tmp_path / "bgm").mkdir()
    (tmp_path / "bgm" / "bgm-1.mp3").write_bytes(b"mp3")
    base = timeline_oreelpack.media_fingerprint(tmp_path, READOUT, MEDIA)
    assert base == timeline_oreelpack.media_fingerprint(tmp_path, READOUT, MEDIA)
    voiced = MEDIA.model_copy(update={"units": (
        MEDIA.units[0].model_copy(update={"narration_audio": PreviewNarrationAudio(path="narration/E3S01.mp3", version=2)}),
        *MEDIA.units[1:],
    )})
    assert timeline_oreelpack.media_fingerprint(tmp_path, READOUT, voiced) != base
    # subtitles are not media: a cue change keeps the fingerprint
    retitled = MEDIA.model_copy(update={"units": (MEDIA.units[0].model_copy(update={"subtitles": ()}), *MEDIA.units[1:])})
    assert timeline_oreelpack.media_fingerprint(tmp_path, READOUT, retitled) == base
    # a BGM has no version: its file's size and time stand in
    (tmp_path / "bgm" / "bgm-1.mp3").write_bytes(b"another mp3")
    assert timeline_oreelpack.media_fingerprint(tmp_path, READOUT, MEDIA) != base


async def test_video_media_name_their_shots_still_for_the_clip_thumbnail(
    service: TimelineOreelPackService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YouReel (owner 2026-11-21, Performance): the editor takes each clip's thumbnail from the studio's small image of
    the shot (the video thumbnail, else the storyboard image, resized by the files route) instead of decoding the
    video; none when the shot has neither. Not part of the media's identity."""
    project = tmp_path / "demo"
    (project / "thumbnails").mkdir()
    (project / "storyboards").mkdir()
    (project / "thumbnails" / "scene_E3S01.jpg").write_bytes(b"jpg")
    (project / "storyboards" / "scene_E3S02.png").write_bytes(b"png")
    timeline_oreelpack._CACHE.clear()
    before = await service.cached("demo", "tl-1")
    timeline_oreelpack._CACHE.clear()

    shots = {
        "E3S01": {"scene_id": "E3S01", "generated_assets": {"storyboard_image": "storyboards/scene_E3S02.png"}},
        "E3S02": {"scene_id": "E3S02", "generated_assets": {"storyboard_image": "storyboards/scene_E3S02.png"}},
        # an untrusted path out of the project is never named
        "E3S03": {"scene_id": "E3S03", "generated_assets": {"storyboard_image": "../outside.png"}},
    }
    (tmp_path / "outside.png").write_bytes(b"png")
    monkeypatch.setattr(TimelineOreelPackService, "_episode_shots", lambda self, *args: shots)
    manifest, project_file, _, filename = await _unpack(service)
    items = {item["name"]: item for item in project_file["project"]["mediaLibrary"]["items"]}

    thumb = project / "thumbnails" / "scene_E3S01.jpg"
    board = project / "storyboards" / "scene_E3S02.png"
    assert items["E3S01.mp4"]["thumbnail_url"] == (
        f"/api/v1/files/demo/thumbnails/scene_E3S01.jpg?w=320&v={thumb.stat().st_mtime_ns}"
    )
    assert items["E3S02.mp4"]["thumbnail_url"] == (
        f"/api/v1/files/demo/storyboards/scene_E3S02.png?w=320&v={board.stat().st_mtime_ns}"
    )
    assert items["E3S03.mp4"]["thumbnail_url"] is None
    # stills and audio carry none; thumbnailUrl stays empty (older editors decode as before)
    assert all("thumbnail_url" not in item for item in items.values() if item["type"] != "video")
    assert all(item["thumbnailUrl"] is None for item in items.values())

    # a new still alone doesn't change the media's ids or the pack's name
    thumb.write_bytes(b"another jpg")
    again, _, _, again_name = await _unpack(service)
    assert [a["id"] for a in again["assets"]] == [a["id"] for a in manifest["assets"]]
    assert again_name == filename == before.filename
    await asyncio.to_thread(before.workdir.cleanup)
