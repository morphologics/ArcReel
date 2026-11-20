"""Measure the EBU R128 integrated loudness of audio with the bundled ffmpeg's ``ebur128`` filter.

用随包 ffmpeg 的 ``ebur128`` 滤镜实测音频的 EBU R128 积分响度。"""

from __future__ import annotations

import re
from pathlib import Path

from lib.infra.ffmpeg import local_file_input
from lib.infra.subprocess_deadline import Spawner, SubprocessDeadlineExceeded, run_with_deadline

MEASURE_DEADLINE_FLOOR_SECONDS = 60.0
MEASURE_DEADLINE_PER_AUDIO_SECOND = 0.5

_INTEGRATED_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+(?:\.\d+)?|-inf)\s*LUFS")


class LoudnessMeasurementError(Exception):
    """ffmpeg could not measure the integrated loudness: decode failure, timeout, or no ``ebur128`` summary in the output.

    ffmpeg 测不出积分响度：解码失败、超时，或输出里没有 ``ebur128`` 的汇总。"""


def measure_args(ffmpeg: str, path: Path) -> list[str]:
    """``ebur128`` prints the summary only at the end; per-frame logging is off and the summary is written to stderr at info level.

    ``ebur128`` 只在结束时打印汇总；逐帧日志关闭，汇总按 info 级别写到 stderr。"""
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        *local_file_input(path),
        "-vn",
        "-af",
        "ebur128=framelog=quiet",
        "-f",
        "null",
        "-",
    ]


def parse_integrated_loudness(stderr: str) -> float:
    """Get the integrated loudness (LUFS) from the ``ebur128`` summary; for silence ffmpeg reports −70 or ``-inf``, both recorded as below −70.

    从 ``ebur128`` 的汇总里取积分响度（LUFS）；无声时 ffmpeg 报 −70 或 ``-inf``，都记为 −70 以下。"""
    matches = _INTEGRATED_RE.findall(stderr)
    if not matches:
        raise LoudnessMeasurementError("ffmpeg output has no integrated loudness summary")
    value = matches[-1]
    return float("-inf") if value == "-inf" else float(value)


async def measure_integrated_loudness(
    ffmpeg: str, path: Path, *, duration_seconds: float, spawn: Spawner | None = None
) -> float:
    """Measure the integrated loudness (LUFS) of ``path``. / 实测 ``path`` 的积分响度（LUFS）。"""
    try:
        result = await run_with_deadline(
            measure_args(ffmpeg, path),
            deadline_seconds=MEASURE_DEADLINE_FLOOR_SECONDS + MEASURE_DEADLINE_PER_AUDIO_SECOND * duration_seconds,
            capture_stderr=True,
            spawn=spawn,
        )
    except SubprocessDeadlineExceeded:
        raise LoudnessMeasurementError("Loudness measurement timed out") from None
    stderr = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        raise LoudnessMeasurementError(f"ffmpeg exit code {result.returncode}: {stderr.strip()[-500:]}")
    return parse_integrated_loudness(stderr)


__all__ = [
    "LoudnessMeasurementError",
    "measure_args",
    "measure_integrated_loudness",
    "parse_integrated_loudness",
]
