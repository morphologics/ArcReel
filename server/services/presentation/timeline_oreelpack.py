"""An edit timeline as an OpenReel project backup (``.oreelpack``), for the experimental "Open in OpenReel" of the
Edit view (YouReel Studio hosts an OpenReel fork next to the studio; deploy/studio/OPENREEL.md).

The pack is OpenReel's own backup format: a 16-byte header (``OREELPACK01\\n`` + the manifest length, uint32 little
endian), a JSON manifest ``{version: 1, projectJson, assets: [{id, size, type}]}`` and the media bytes in asset order.
The project JSON (OpenReel schema 1.2.0) is built from the same data as the Edit view (the timeline readout and the
preview media):

- clips back to back on one video track: trims, per-clip volume (muted where the provider sound is off), ArcReel ids
  in ``clip.metadata.arcreel`` (for the way back);
- an end hold becomes a **still image** of the shot's last frame right after it (OpenReel stores freeze frames but
  its player doesn't render them);
- transitions mapped to OpenReel's (dissolve → crossfade, fade to black → dipToBlack, …; squeeze, curtains and the
  gradient wipe approximated); both models centre a transition on the cut;
- subtitles, narration (dialogue track) and BGM (music track, volume × the loudness gain, fades).
"""

from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.edit_timeline.readout import ClipView, EditTimelineReadout
from lib.edit_timeline.service import EditTimelineService
from lib.infra.ffmpeg import ffmpeg_executable
from lib.project.project_manager import ProjectManager
from server.services.presentation.timeline_preview import TimelinePreviewMedia, TimelinePreviewService

MAGIC = b"OREELPACK01\n"
SCHEMA_VERSION = "1.2.0"
FPS = 30
CHUNK = 1024 * 1024
#: Content types of the media a pack holds (no ``mimetypes``: it reads the system tables from disk on first use).
CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}

TRANSITIONS: dict[str, tuple[str, dict[str, Any]]] = {
    "dissolve": ("crossfade", {}),
    "fade_black": ("dipToBlack", {}),
    "fade_white": ("dipToWhite", {}),
    "push_left": ("push", {"direction": "left"}),
    "push_right": ("push", {"direction": "right"}),
    "push_up": ("push", {"direction": "up"}),
    "push_down": ("push", {"direction": "down"}),
    "wipe_left": ("wipe", {"direction": "left"}),
    "wipe_right": ("wipe", {"direction": "right"}),
    "wipe_up": ("wipe", {"direction": "up"}),
    "wipe_down": ("wipe", {"direction": "down"}),
    "circle": ("circleReveal", {}),
    "curtain_horizontal": ("splitReveal", {"orientation": "horizontal"}),
    "curtain_vertical": ("splitReveal", {"orientation": "vertical"}),
    "mosaic": ("mosaic", {}),
    "blur": ("blur", {}),
    "radial": ("radialWipe", {}),
    "gradient_wipe": ("wipe", {"direction": "left", "softness": 0.5}),
    "squeeze": ("zoom", {}),
}
TRANSFORM = {
    "position": {"x": 0, "y": 0},
    "scale": {"x": 1, "y": 1},
    "rotation": 0,
    "anchor": {"x": 0.5, "y": 0.5},
    "opacity": 1,
    "rotate3d": {"x": 0, "y": 0, "z": 0},
    "perspective": 1000,
    "transformStyle": "flat",
}
SUBTITLE_STYLE = {
    "fontFamily": "Inter",
    "fontSize": 44,
    "color": "#ffffff",
    "backgroundColor": "rgba(0,0,0,0.45)",
    "position": "bottom",
}


def _r(value: float) -> float:
    return round(value, 3)


@dataclass(frozen=True)
class _Asset:
    id: str
    type: str
    size: int
    path: Path


@dataclass(frozen=True)
class OreelPack:
    """A pack ready to stream: header + manifest bytes, then the assets' files."""

    filename: str
    head: bytes
    assets: tuple[_Asset, ...]
    workdir: tempfile.TemporaryDirectory[str]

    @property
    def size(self) -> int:
        return len(self.head) + sum(asset.size for asset in self.assets)

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            yield self.head
            for asset in self.assets:
                handle = await asyncio.to_thread(asset.path.open, "rb")
                try:
                    while chunk := await asyncio.to_thread(handle.read, CHUNK):
                        yield chunk
                finally:
                    await asyncio.to_thread(handle.close)
        finally:
            await asyncio.to_thread(self.workdir.cleanup)


def _clip(clip_id: str, media_id: str, track_id: str, start: float, duration: float, in_point: float, **extra: Any):
    return {
        "id": clip_id,
        "mediaId": media_id,
        "trackId": track_id,
        "startTime": _r(start),
        "duration": _r(duration),
        "inPoint": _r(in_point),
        "outPoint": _r(in_point + duration),
        "effects": [],
        "audioEffects": [],
        "transform": dict(TRANSFORM),
        "keyframes": [],
        **extra,
    }


def _track(track_id: str, kind: str, role: str, name: str, clips: list[dict], transitions: list[dict] | None = None):
    return {
        "id": track_id,
        "type": kind,
        "role": role,
        "name": name,
        "clips": clips,
        "transitions": transitions or [],
        "locked": False,
        "hidden": False,
        "muted": False,
        "solo": False,
    }


class TimelineOreelPackService:
    def __init__(self, projects: ProjectManager) -> None:
        self._projects = projects
        self._timelines = EditTimelineService(projects)
        self._preview = TimelinePreviewService(projects)

    async def build(self, project_name: str, timeline_id: str) -> OreelPack:
        readout = await self._timelines.read(project_name, timeline_id)
        media = await self._preview.media(project_name, timeline_id)
        project_dir = await asyncio.to_thread(self._projects.get_project_path, project_name)
        project = await asyncio.to_thread(self._projects.load_project, project_name)
        workdir = await asyncio.to_thread(tempfile.TemporaryDirectory, prefix="oreelpack-")
        try:
            return await self._build(project_name, project_dir, project, readout, media, workdir)
        except BaseException:
            await asyncio.to_thread(workdir.cleanup)
            raise

    async def _build(
        self,
        project_name: str,
        project_dir: Path,
        project: dict,
        readout: EditTimelineReadout,
        media: TimelinePreviewMedia,
        workdir: tempfile.TemporaryDirectory[str],
    ) -> OreelPack:
        portrait = str(project.get("aspect_ratio") or "9:16").startswith("9")
        width, height = (1080, 1920) if portrait else (1920, 1080)
        units = {unit.unit_id: unit for unit in media.units}
        items: list[dict] = []
        assets: list[_Asset] = []
        ids: dict[str, str] = {}

        async def add_media(key: str, name: str, kind: str, path: Path, **meta: Any) -> str:
            if key in ids:
                return ids[key]
            media_id = str(uuid.uuid4())
            ids[key] = media_id
            size = (await asyncio.to_thread(path.stat)).st_size
            content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
            items.append({
                "id": media_id, "name": name, "type": kind, "fileHandle": None, "blob": None,
                "thumbnailUrl": None, "waveformData": None, "isPlaceholder": False,
                "sourceFile": {"name": name, "size": size, "lastModified": int(time.time() * 1000)},
                "metadata": {
                    "duration": 0, "width": 0, "height": 0, "frameRate": 0, "codec": "",
                    "sampleRate": 48000, "channels": 2, "fileSize": size, **meta,
                },
            })
            assets.append(_Asset(id=media_id, type=content_type, size=size, path=path))
            return media_id

        def video_path(unit_id: str) -> Path:
            plain = project_dir / "videos" / f"scene_{unit_id}.mp4"
            reference = project_dir / "reference_videos" / f"{unit_id}.mp4"
            return plain if plain.is_file() or not reference.is_file() else reference

        video_track = str(uuid.uuid4())
        video_clips: list[tuple[ClipView | None, dict]] = []   # (ArcReel clip or None for a hold still, OpenReel clip)
        subtitles: list[dict] = []
        transitions: list[dict] = []
        cursor = 0.0
        for clip in readout.clips:
            if clip.status != "ready" or clip.duration <= 0:
                # a clip without video keeps its time on the timeline (as in the Edit view): a gap here
                if clip.status == "video_missing":
                    cursor = _r(cursor + clip.duration)
                continue
            unit = units.get(clip.unit_id)
            path = await asyncio.to_thread(video_path, clip.unit_id)
            media_id = await add_media(
                f"{clip.unit_id}@{clip.video_version}", f"{clip.unit_id}.mp4", "video", path,
                duration=clip.source_duration or 0, width=width, height=height, frameRate=FPS, codec="avc1",
                hasVideo=True, hasAudio=True,
            )
            source_in = clip.trim.source_in if clip.trim else 0.0
            picture = _r(max(0.0, clip.duration - clip.hold))
            volume = 0.0 if unit is not None and not unit.provider_audio else clip.source_volume
            video_clips.append((clip, _clip(
                str(uuid.uuid4()), media_id, video_track, cursor, picture, source_in, volume=volume,
                metadata={"arcreel": {
                    "project": project_name, "timeline_id": readout.timeline.id, "revision": readout.revision,
                    "clip_id": clip.id, "unit_id": clip.unit_id, "video_version": clip.video_version,
                    "reason": clip.reason,
                }},
            )))
            # the clip's subtitles, on the timeline (as the Edit view places them)
            for cue in unit.subtitles if unit else ():
                follows = unit.subtitles_follow_narration and clip.narration is not None
                anchor = clip.narration.start if follows and clip.narration else clip.start - source_in
                start = max(clip.start, anchor + cue.start)
                end = min(clip.start + clip.duration, anchor + cue.start + cue.duration)
                if end > start:
                    subtitles.append({
                        "id": str(uuid.uuid4()), "text": cue.text, "startTime": _r(start), "endTime": _r(end),
                        "style": dict(SUBTITLE_STYLE),
                    })
            cursor = _r(cursor + picture)
            if clip.hold > 0:
                still = Path(workdir.name) / f"hold-{clip.id}.jpg"
                await _last_frame(path, source_in + picture, still)
                still_id = await add_media(
                    f"still:{clip.id}", f"{clip.unit_id}-hold.jpg", "image", still,
                    duration=3600, width=width, height=height, hasVideo=True, hasAudio=False,
                )
                video_clips.append((None, _clip(
                    str(uuid.uuid4()), still_id, video_track, cursor, clip.hold, 0.0, volume=0,
                    metadata={
                        "arcreel": {"clip_id": clip.id, "unit_id": clip.unit_id, "kind": "end_hold", "hold": clip.hold}
                    },
                )))
                cursor = _r(cursor + clip.hold)
        # a clip's transition joins its last item (the hold still, if any) to the next clip
        for index, (arc, _) in enumerate(video_clips):
            if arc is None or not arc.transition_to_next:
                continue
            last = index + 1 if index + 1 < len(video_clips) and video_clips[index + 1][0] is None else index
            if last + 1 >= len(video_clips):
                continue
            kind, params = TRANSITIONS.get(arc.transition_to_next.type, ("crossfade", {}))
            transitions.append({
                "id": str(uuid.uuid4()),
                "clipAId": video_clips[last][1]["id"],
                "clipBId": video_clips[last + 1][1]["id"],
                "type": kind,
                "duration": arc.transition_to_next.duration,
                "params": {**params, "arcreel_type": arc.transition_to_next.type},
            })
        cut = [openreel_clip for _, openreel_clip in video_clips]
        tracks = [_track(video_track, "video", "general", "Video (YouReel cut)", cut, transitions)]

        if media.narration == "with_narration":
            track_id = str(uuid.uuid4())
            narration_clips = []
            for clip, _ in video_clips:
                unit = units.get(clip.unit_id) if clip else None
                if clip is None or not clip.carries_narration or not clip.narration:
                    continue
                if unit is None or unit.narration_audio is None:
                    continue
                path = project_dir / unit.narration_audio.path
                media_id = await add_media(
                    f"narration:{clip.unit_id}@{unit.narration_audio.version}",
                    f"{clip.unit_id}-narration{path.suffix}",
                    "audio",
                    path,
                    hasAudio=True,
                    hasVideo=False,
                )
                end = clip.narration.end if clip.narration.end is not None else clip.start + clip.duration
                narration_clips.append(_clip(
                    str(uuid.uuid4()), media_id, track_id, clip.narration.start, end - clip.narration.start, 0.0,
                    volume=1, metadata={"arcreel": {"clip_id": clip.id, "unit_id": clip.unit_id, "kind": "narration"}},
                ))
            tracks.append(_track(track_id, "audio", "dialogue", "Narration", narration_clips))

        if readout.bgm:
            track_id = str(uuid.uuid4())
            sources = {bgm.bgm_id: bgm for bgm in media.bgm}
            bgm_clips = []
            for bgm in readout.bgm:
                source = sources.get(bgm.bgm_id)
                if source is None:
                    continue
                path = project_dir / source.path
                media_id = await add_media(
                    f"bgm:{bgm.bgm_id}", bgm.name or bgm.bgm_id, "audio", path, hasAudio=True, hasVideo=False
                )
                bgm_clips.append(_clip(
                    str(uuid.uuid4()), media_id, track_id, bgm.start, bgm.end - bgm.start, bgm.source_in,
                    volume=_r(min(1.0, bgm.volume * source.gain)),
                    fade={"fadeIn": bgm.fade_in, "fadeOut": bgm.fade_out},
                    metadata={"arcreel": {"bgm_clip_id": bgm.id, "bgm_id": bgm.bgm_id}},
                ))
            tracks.append(_track(track_id, "audio", "music", "BGM", bgm_clips))

        now = int(time.time() * 1000)
        title = project.get("title") or project_name
        project_file = {
            "version": SCHEMA_VERSION,
            "project": {
                "id": str(uuid.uuid4()),
                "name": (
                    f"{title} · {readout.timeline.name} (episode {readout.timeline.episode}, rev {readout.revision})"
                ),
                "createdAt": now,
                "modifiedAt": now,
                "settings": {"width": width, "height": height, "frameRate": FPS, "sampleRate": 48000, "channels": 2},
                "mediaLibrary": {"items": items},
                "timeline": {"tracks": tracks, "subtitles": subtitles, "duration": readout.duration, "markers": []},
            },
        }
        manifest = json.dumps({
            "version": 1,
            "projectJson": json.dumps(project_file, ensure_ascii=False),
            "assets": [{"id": a.id, "size": a.size, "type": a.type} for a in assets],
        }, ensure_ascii=False).encode("utf-8")
        head = MAGIC + struct.pack("<I", len(manifest)) + manifest
        filename = f"{project_name}-ep{readout.timeline.episode}-{readout.timeline.id}-r{readout.revision}.oreelpack"
        return OreelPack(filename=filename, head=head, assets=tuple(assets), workdir=workdir)


async def _last_frame(video: Path, at: float, output: Path) -> None:
    """The last frame shown before ``at`` seconds, as JPEG: decodes the half second before it and keeps the final
    frame (``-update``), so an out point at the very end of the file works too (a seek to just before the end can
    land past the last video frame and yield nothing)."""
    ffmpeg = await asyncio.to_thread(ffmpeg_executable)
    start = max(0.0, at - 0.5)
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{at - start:.3f}", "-an",
        "-update", "1", "-q:v", "2", str(output),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0 or not await asyncio.to_thread(output.is_file):
        raise RuntimeError(f"could not take the last frame of {video.name}: {stderr.decode(errors='replace')[-300:]}")


__all__ = ["OreelPack", "TimelineOreelPackService"]
