"""The malloc_trim loop: interval setting, one real trim, start / stop."""

from __future__ import annotations

import asyncio
import sys

import pytest

from server.services import memory_trim


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, memory_trim.DEFAULT_INTERVAL_SECONDS),
        ("", memory_trim.DEFAULT_INTERVAL_SECONDS),
        ("300", 300),
        ("0", 0),
        ("-5", 0),
        ("soon", memory_trim.DEFAULT_INTERVAL_SECONDS),
    ],
)
def test_interval_from_environment(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int) -> None:
    if raw is None:
        monkeypatch.delenv("ARCREEL_MALLOC_TRIM_INTERVAL", raising=False)
    else:
        monkeypatch.setenv("ARCREEL_MALLOC_TRIM_INTERVAL", raw)
    assert memory_trim.interval_seconds() == expected


def test_trim_once_reports_freed_memory_and_time() -> None:
    calls: list[int] = []
    freed, took = memory_trim.trim_once(lambda pad: calls.append(pad) or 1)
    assert calls == [0]
    assert took >= 0
    if sys.platform.startswith("linux"):
        assert isinstance(freed, int)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc malloc_trim is Linux only")
def test_real_malloc_trim_runs() -> None:
    trim = memory_trim._load_malloc_trim()
    if trim is None:
        pytest.skip("this C library has no malloc_trim (musl)")
    freed, _took = memory_trim.trim_once(trim)
    assert freed is not None


async def test_switched_off_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCREEL_MALLOC_TRIM_INTERVAL", "0")
    task = memory_trim.start_memory_trim()
    assert task is None
    await memory_trim.stop_memory_trim(task)


async def test_loop_trims_every_interval_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(memory_trim, "_load_malloc_trim", lambda: lambda pad: calls.append(pad) or 1)
    monkeypatch.setenv("ARCREEL_MALLOC_TRIM_INTERVAL", "1")
    real_sleep = asyncio.sleep
    monkeypatch.setattr(memory_trim.asyncio, "sleep", lambda _s: real_sleep(0.01))
    task = memory_trim.start_memory_trim()
    assert task is not None
    for _ in range(100):
        if len(calls) >= 2:
            break
        await real_sleep(0.01)
    await memory_trim.stop_memory_trim(task)
    assert len(calls) >= 2
    assert task.cancelled()
