"""Media duration probing and audio upload validation utilities. / 媒体时长探测与音频上传校验工具。"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from lib.infra.async_thread import run_noninterruptible_sync, run_sync_transaction
from lib.infra.content_digest import FILE_DIGEST_RACY_WINDOW_NS, FileIdentity, file_identity
from lib.infra.ffmpeg import FfmpegUnavailableError
from lib.infra.media_probe import MediaProbe, MediaProbeError, probe_media
from lib.infra.path_safety import safe_resolve

logger = logging.getLogger(__name__)

# Character reference audio constraints (same rules for upload and TTS-generated samples): wav/mp3, 2-10 seconds, ≤15MB.
# Sources: see lib/backends/audio_backends/dashscope.py and the references in server/routers/files.py.
# 角色参考音频约束（上传与 TTS 生成样本同口径）：wav/mp3、2-10 秒、≤15MB。
# 出处见 lib/backends/audio_backends/dashscope.py 与 server/routers/files.py 引用处。
AUDIO_REFERENCE_MAX_BYTES = 15 * 1024 * 1024
AUDIO_REFERENCE_MIN_SECONDS = 2.0
AUDIO_REFERENCE_MAX_SECONDS = 10.0

# Upload validation runs synchronously within the request; a corrupt file must not hold the request for long. / 上传校验在请求内同步执行，损坏文件不能长时间占住请求。
_UPLOAD_PROBE_DEADLINE_SECONDS = 10.0

# The probed container format is a set of candidate demuxer names (e.g. m4a is mov,mp4,m4a,3gp,3g2,mj2);
# depending on the extension it must contain the given token,
# to stop files that "have an audio track but the container is not wav/mp3" (e.g. m4a renamed to .wav) from slipping through.
# 探测出的容器格式是一组候选 demuxer 名（如 m4a 为 mov,mp4,m4a,3gp,3g2,mj2），
# 按扩展名要求其中必须含指定 token，
# 防止「有音轨但容器不是 wav/mp3」的文件（如把 m4a 改名为 .wav）蒙混过关。
_CONTAINER_FORMAT_TOKENS = {
    ".wav": {"wav"},
    ".mp3": {"mp3"},
}


def resolve_audio_ref_path(project_dir: Path, audio_refs_dir: Path, rel_path: str | None) -> Path | None:
    """Resolve the reference_audio field value; return it only if it really lies within characters/refs_audio.

    The field can be set to any in-project string via asset PATCH (extra_string_fields only checks the type).
    safe_resolve alone only guarantees it does not escape the project directory, which is not enough to prevent
    being tricked into deleting project.json or other in-project files, so we additionally check that the parent
    directory is refs_audio. Upload replacement (files.py) and TTS sample confirmation (generate.py) share this check.

    解析 reference_audio 字段值，仅当其确实落在 characters/refs_audio 内才返回。

    该字段可经资产 PATCH 被写成项目内任意字符串（extra_string_fields 只做类型校验），
    单靠 safe_resolve 只保证不越界出项目目录，还不足以防止被诱导删除 project.json
    等项目内其它文件，故额外校验父目录命中 refs_audio。上传替换（files.py）与 TTS
    生成样本确认落盘（generate.py）共用同一份判定。
    """
    resolved = safe_resolve(project_dir, rel_path)
    if resolved is None:
        return None
    if os.path.realpath(resolved.parent) != os.path.realpath(audio_refs_dir):
        return None
    return resolved


def resolve_stale_reference_audio(
    project_dir: Path, audio_refs_dir: Path, old_audio: str | None, new_path: Path
) -> Path | None:
    """When replacing a character's reference audio, identify the old file that "no pointer refers to once replaced".

    Unlike reference images, audio is not forced to a single extension, so old and new extensions may differ on
    replacement and the old file must be cleaned up explicitly to avoid orphans.
    Returns None when no cleanup is needed: the old pointer is empty, points outside refs_audio (see
    :func:`resolve_audio_ref_path`), or on a case-insensitive file system the old pointer and the new file name differ
    only in case but point to the same inode (e.g. ``Alice.WAV`` and ``Alice.wav``) -- the new content is about to
    overwrite that file in place, so it must not be deleted as an orphan, or the freshly written sample would go too.

    替换角色参考音频时，识别出「换掉后就没有指针指向」的旧文件。

    音频不像参考图强制统一扩展名，替换时新旧扩展名可能不同，旧文件需显式清理避免孤儿。
    返回 None 表示无需清理：旧指针为空、指向 refs_audio 之外（见
    :func:`resolve_audio_ref_path`），或大小写不敏感文件系统上旧指针与新文件名只是大小写
    不同却指向同一 inode（如 ``Alice.WAV`` 与 ``Alice.wav``）——此时新内容即将原地覆盖该
    文件，不能再当孤儿删掉，否则会把刚写入的新样本一并删除。
    """
    if not isinstance(old_audio, str) or not old_audio:
        return None
    resolved_old = resolve_audio_ref_path(project_dir, audio_refs_dir, old_audio)
    if resolved_old is None:
        return None
    if new_path.exists() and resolved_old.samefile(new_path):
        return None
    return resolved_old


class _ProbeCache:
    """Process-wide LRU of successful probes of files on disk, keyed like the file digest cache by the file's stat
    identity (device, inode, size, mtime in ns): a regenerated, replaced or restored file has a new identity and is
    probed again. Thread-safe."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[FileIdentity, MediaProbe] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, identity: FileIdentity) -> MediaProbe | None:
        with self._lock:
            probe = self._entries.get(identity)
            if probe is not None:
                self._entries.move_to_end(identity)
            return probe

    def put(self, identity: FileIdentity, probe: MediaProbe) -> None:
        with self._lock:
            self._entries[identity] = probe
            self._entries.move_to_end(identity)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


#: Every clip's video and narration file is probed for its duration by several read endpoints on each Edit view
#: load (preview media, presentations, timeline issues); one ffprobe subprocess each made a 17-clip episode take
#: seconds per endpoint. MediaProbe is immutable, so a cached one can be shared.
_PROBE_CACHE = _ProbeCache(4096)


def reset_probe_cache_for_tests() -> None:
    """Empty the probe cache (test isolation)."""

    _PROBE_CACHE.clear()


async def _probe_or_none(path: Path) -> MediaProbe | None:
    """Probe a file already on disk; return None when the bundled ffmpeg is unavailable or the file cannot be parsed.

    探测已落盘文件；随包 ffmpeg 不可用或文件无法解析时返回 None。

    Successful probes are cached by the file's stat identity (``_PROBE_CACHE``), entered only when the file did not
    change during the probe and is older than the digest cache's racy window, as for file digests."""
    try:
        before = await asyncio.to_thread(path.stat)
    except OSError:
        return None
    identity = file_identity(before)
    cached = _PROBE_CACHE.get(identity)
    if cached is not None:
        return cached
    try:
        probe = await probe_media(path)
    except (FfmpegUnavailableError, MediaProbeError, OSError):
        return None
    try:
        after = await asyncio.to_thread(path.stat)
    except OSError:
        return probe
    if file_identity(after) == identity and time.time_ns() - before.st_mtime_ns >= FILE_DIGEST_RACY_WINDOW_NS:
        _PROBE_CACHE.put(identity, probe)
    return probe


def _write_probe_temp_file(content: bytes, suffix: str, created: list[Path]) -> Path:
    """Write the bytes to probe into the system temp directory. The file is registered in ``created`` as soon as it is created; the caller deletes it.

    把待探测的字节写进系统临时目录。文件一建好就登记进 ``created``，由调用方负责删除。"""
    with tempfile.NamedTemporaryFile(dir=tempfile.gettempdir(), suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        created.append(tmp_path)
        tmp.write(content)
    return tmp_path


async def probe_audio_duration_seconds(content: bytes, suffix: str) -> float | None:
    """Probe the duration (seconds) of audio bytes and confirm they really contain a decodable audio stream.

    Returns None when the bundled ffmpeg is unavailable; the caller degrades: skip the duration check, do not block the upload.

    Raises:
        ValueError: cannot be parsed, probe timed out, no audio stream in the container (e.g. a video file renamed
            to .wav/.mp3 and uploaded), or the container format does not match the extension (e.g. m4a/aac renamed
            to .wav and uploaded).

    探测音频字节的时长（秒），并确认其中确有可解码的音频流。

    随包 ffmpeg 不可用时返回 None，调用方按降级处理：跳过时长校验，不阻断上传。

    Raises:
        ValueError: 无法解析、探测超时、容器内没有音频流（如把视频文件改名为 .wav/.mp3
            上传），或容器格式与扩展名不符（如把 m4a/aac 改名为 .wav 上传）。
    """
    # If cancellation lands during the temp-file write the return value is lost; the registered path is used to clean up.
    # 取消落在写临时文件那一程时返回值会丢，靠登记的路径回收。
    created: list[Path] = []
    try:
        tmp_path = await run_sync_transaction(_write_probe_temp_file, content, suffix, created)
        try:
            probe = await probe_media(tmp_path, deadline_seconds=_UPLOAD_PROBE_DEADLINE_SECONDS)
        except (FfmpegUnavailableError, OSError):
            logger.info("随包 ffmpeg 不可用，跳过音频时长探测", exc_info=True)
            return None
        except MediaProbeError:
            raise ValueError("Audio file cannot be parsed") from None
    finally:
        for path in created:
            await run_noninterruptible_sync(path.unlink, missing_ok=True)  # hard-delete: temp -- temp file for duration probing / 时长探测的临时文件

    if probe.first_stream("audio") is None:
        raise ValueError("Audio file cannot be parsed")
    expected_tokens = _CONTAINER_FORMAT_TOKENS.get(suffix.lower())
    if expected_tokens is not None and not probe.container_formats & expected_tokens:
        raise ValueError("Audio file cannot be parsed")
    if probe.duration_seconds is None:
        raise ValueError("Audio file cannot be parsed")
    return probe.duration_seconds


async def probe_existing_media_duration_seconds(path: Path) -> float | None:
    """Probe the container duration (seconds) of a media file already on disk.

    Unlike the bytes-input :func:`probe_audio_duration_seconds`, this probes an existing file directly:
    no temp file, no stream-type validation. Callers that need the video track's playback bounds should use
    :func:`probe_existing_video_duration_seconds`.
    Returns None when the bundled ffmpeg is unavailable or probing fails; the caller decides to allow or block
    according to its strictness.

    探测磁盘上已落盘媒体文件的容器时长（秒）。

    与 :func:`probe_audio_duration_seconds` 的字节输入版本不同：本函数直接对已存在文件探测，
    不写临时文件、不做流类型校验。需要视频轨播放边界的调用方应使用
    :func:`probe_existing_video_duration_seconds`。
    随包 ffmpeg 不可用或探测失败时返回 None，由调用方按业务严格度决定放行或阻断。
    """
    probe = await _probe_or_none(path)
    return probe.duration_seconds if probe is not None else None


async def probe_existing_media(path: Path) -> MediaProbe | None:
    """The whole probe of a file on disk, from the same cache; None when ffmpeg is missing or the probe fails (callers
    that need the reason probe again with :func:`lib.infra.media_probe.probe_media`)."""
    return await _probe_or_none(path)


async def probe_existing_video_duration_seconds(path: Path) -> float | None:
    """Probe the playable duration of the first video stream; return None when there is no video stream or probing fails.

    The container may be longer than the video track because of a longer audio tail; the video editor bounds the
    source range by the video track duration, so the container tail must not be treated as usable footage.

    探测首个视频流的可播放时长；没有视频流或探测失败时返回 None。

    容器可能因音轨尾部较长而比视频轨更长；视频编辑器按视频轨时长约束 source range，
    因此不能把容器尾部当成可用画面。
    """
    probe = await _probe_or_none(path)
    video = probe.first_stream("video") if probe is not None else None
    return video.duration_seconds if video is not None else None


async def probe_existing_audio_duration_seconds(path: Path) -> float | None:
    """Probe the final audio duration; keeps a semantic entry point for audio callers. / 探测正式音频时长；保留音频调用方的语义化入口。"""

    return await probe_existing_media_duration_seconds(path)


async def probe_reference_audio_total_seconds(paths: list[Path]) -> float | None:
    """Probe the total duration (seconds) of several reference audio files, for the request-time total-duration capability check.

    If probing any file fails (bundled ffmpeg unavailable, corrupt file), return None rather than a partial sum: a
    partial total would make the caller wrongly conclude "within limit" and let through a request that should be
    blocked, which is more dangerous than skipping the check.

    探测多段参考音频文件的总时长（秒），供请求期总时长能力校验使用。

    任一文件时长探测失败（随包 ffmpeg 不可用、文件损坏）都返回 None 而非部分求和：半截总时长会
    让调用方误判「未超限」而放行本该拦截的请求，比跳过校验更危险。
    """
    total = 0.0
    for path in paths:
        duration = await probe_existing_audio_duration_seconds(path)
        if duration is None:
            return None
        total += duration
    return total
