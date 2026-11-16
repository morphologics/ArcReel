"""Give freed memory back to the system every few minutes (glibc ``malloc_trim``).

Python frees objects, but glibc keeps the freed heap space for reuse and returns little of it to the system, so a
studio's memory only grows with use: on DEV, loading every project's edit timelines and OpenReel packs took the
worker from 274 MB to 520 MB with only ~82 MB actually in use, and three rounds to 617 MB. ``malloc_trim(0)`` gave
back 89-168 MB each time.

Every :data:`DEFAULT_INTERVAL_SECONDS` (environment ``ARCREEL_MALLOC_TRIM_INTERVAL``, seconds; 0 = off) this calls
``malloc_trim(0)`` in a worker thread, so the event loop isn't held while glibc walks its heaps. Only where glibc
provides it (Linux); elsewhere (macOS, Windows, musl) nothing starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import logging
import os
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 120
#: Freeing at least this much is logged at info level (smaller trims at debug).
REPORT_FREED_BYTES = 32 * 2**20


def _load_malloc_trim() -> Callable[[int], int] | None:
    """glibc's ``malloc_trim``, or None where the C library doesn't have it."""
    name = ctypes.util.find_library("c")
    if not name:
        return None
    try:
        libc = ctypes.CDLL(name)
        trim = libc.malloc_trim
    except (OSError, AttributeError):
        return None
    trim.argtypes = [ctypes.c_size_t]
    trim.restype = ctypes.c_int
    return trim


def _rss_bytes() -> int | None:
    """This process's resident memory, from /proc (Linux); None elsewhere."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def interval_seconds() -> int:
    """The trim interval from ``ARCREEL_MALLOC_TRIM_INTERVAL`` (seconds, 0 = off); the default when unset or invalid."""
    raw = os.environ.get("ARCREEL_MALLOC_TRIM_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("ARCREEL_MALLOC_TRIM_INTERVAL=%r is not a number of seconds; using %d", raw, DEFAULT_INTERVAL_SECONDS)
        return DEFAULT_INTERVAL_SECONDS


def trim_once(trim: Callable[[int], int]) -> tuple[int | None, float]:
    """Run one trim; returns (bytes freed by resident memory, or None if unknown; seconds taken)."""
    before = _rss_bytes()
    started = time.monotonic()
    trim(0)
    took = time.monotonic() - started
    after = _rss_bytes()
    freed = before - after if before is not None and after is not None else None
    return freed, took


async def _trim_loop(trim: Callable[[int], int], interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            freed, took = await asyncio.to_thread(trim_once, trim)
        except Exception:
            logger.warning("malloc_trim failed; trying again next round", exc_info=True)
            continue
        if freed is not None and freed >= REPORT_FREED_BYTES:
            logger.info("malloc_trim gave back %d MB in %.0f ms", freed // 2**20, took * 1000)
        else:
            logger.debug("malloc_trim: %s bytes in %.0f ms", freed, took * 1000)


def start_memory_trim() -> asyncio.Task[None] | None:
    """Start the trim loop (first trim after one interval); None when switched off or unavailable."""
    interval = interval_seconds()
    if interval == 0:
        logger.info("malloc_trim loop off (ARCREEL_MALLOC_TRIM_INTERVAL=0)")
        return None
    trim = _load_malloc_trim()
    if trim is None:
        return None
    return asyncio.get_running_loop().create_task(_trim_loop(trim, interval), name="malloc-trim")


async def stop_memory_trim(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
