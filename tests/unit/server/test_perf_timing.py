"""YouReel: server timing middleware (server/perf_timing.py): which answers become server_slow records."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse

from server.perf_timing import (
    RequestTiming,
    ServerTimingMiddleware,
    SlowThresholds,
    _RateLimit,
    classify,
    timing_report,
)

FAST_ONLY = 10_000_000.0  # a wall threshold no test request reaches


def _app(thresholds: SlowThresholds, recorded: list[RequestTiming]) -> FastAPI:
    app = FastAPI()

    async def writer(timing: RequestTiming) -> None:
        recorded.append(timing)

    app.add_middleware(ServerTimingMiddleware, writer=writer, thresholds=thresholds)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/echo")
    async def echo():
        return {"ok": True}

    @app.get("/api/v1/big")
    async def big():
        return Response(b"x" * 100, media_type="application/json")

    @app.post("/api/v1/diagnostics/client-errors")
    async def diagnostics():
        return {"accepted": 0}

    @app.get("/api/v1/projects/p/edit-timelines/t/oreelpack")
    async def pack():
        return Response(b"x" * 100, media_type="application/zip")

    @app.get("/api/v1/events")
    async def events():
        async def stream():
            for i in range(3):
                yield f"data: {i}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


async def _get(thresholds: SlowThresholds, path: str, **kwargs) -> list[RequestTiming]:
    recorded: list[RequestTiming] = []
    transport = httpx.ASGITransport(app=_app(thresholds, recorded))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        method = kwargs.pop("method", "GET")
        await client.request(method, path, **kwargs)
    await asyncio.sleep(0)  # the write runs in its own task
    return recorded


async def test_fast_small_answers_are_not_recorded():
    assert await _get(SlowThresholds(wall_ms=FAST_ONLY, bytes=10_000), "/api/v1/big") == []


async def test_slow_answer_is_recorded_with_path_without_query_and_client():
    recorded = await _get(
        SlowThresholds(wall_ms=0.000001), "/api/v1/echo?token=secret", headers={"X-Client": "flow"}
    )
    assert len(recorded) == 1
    timing = recorded[0]
    assert (timing.method, timing.path, timing.status, timing.tag, timing.client) == (
        "GET",
        "/api/v1/echo",
        200,
        "api",
        "flow",
    )
    assert timing.bytes == len(b'{"ok":true}')
    assert timing.wall_ms >= 0
    assert timing.cpu_ms >= 0


async def test_large_answer_is_recorded_by_bytes():
    recorded = await _get(SlowThresholds(wall_ms=FAST_ONLY, bytes=50), "/api/v1/big")
    assert [(t.path, t.bytes) for t in recorded] == [("/api/v1/big", 100)]


async def test_transfers_use_the_higher_thresholds():
    limits = SlowThresholds(wall_ms=0.000001, bytes=50, transfer_wall_ms=FAST_ONLY, transfer_bytes=1000)
    assert await _get(limits, "/api/v1/projects/p/edit-timelines/t/oreelpack") == []
    limits = SlowThresholds(wall_ms=FAST_ONLY, transfer_wall_ms=FAST_ONLY, transfer_bytes=50)
    recorded = await _get(limits, "/api/v1/projects/p/edit-timelines/t/oreelpack")
    assert [t.tag for t in recorded] == ["transfer"]


async def test_streams_count_only_time_to_first_byte():
    # The stream's full length never counts: only a first byte later than the api threshold would
    assert await _get(SlowThresholds(wall_ms=FAST_ONLY, bytes=1), "/api/v1/events") == []
    recorded = await _get(SlowThresholds(wall_ms=0.000001), "/api/v1/events")
    assert [t.tag for t in recorded] == ["stream"]
    assert recorded[0].ttfb_ms is not None


@pytest.mark.parametrize(
    ("path", "method"),
    [("/health", "GET"), ("/api/v1/diagnostics/client-errors", "POST")],
)
async def test_health_and_diagnostics_routes_are_never_recorded(path: str, method: str):
    assert await _get(SlowThresholds(wall_ms=0.000001, bytes=1), path, method=method) == []


async def test_switched_off():
    assert await _get(SlowThresholds(wall_ms=0.000001, enabled=False), "/api/v1/echo") == []


async def test_invalid_client_header_is_labelled_not_trusted():
    recorded = await _get(SlowThresholds(wall_ms=0.000001), "/api/v1/echo", headers={"X-Client": "Bad Name!"})
    assert recorded[0].client == "invalid"


@pytest.mark.parametrize(
    ("path", "status", "headers", "tag"),
    [
        ("/api/v1/projects", 200, {"content-type": "application/json"}, "api"),
        ("/api/v1/projects/p/events", 200, {"content-type": "text/event-stream; charset=utf-8"}, "stream"),
        ("/api/v1/files/p/videos/a.mp4", 206, {"content-type": "video/mp4"}, "transfer"),
        ("/api/v1/system/logs/download", 200, {"content-disposition": "attachment; x"}, "transfer"),
        ("/api/v1/x", 200, {"content-type": "application/octet-stream"}, "transfer"),
        ("/reel/assets/index.js", 200, {"content-type": "text/javascript"}, "transfer"),
    ],
)
def test_classify(path: str, status: int, headers: dict[str, str], tag: str):
    assert classify(path, status, headers) == tag


def test_thresholds_from_env():
    limits = SlowThresholds.from_env(
        {"DIAGNOSTICS_SERVER_SLOW_MS": "500", "DIAGNOSTICS_SERVER_SLOW_BYTES": "nonsense", "DIAGNOSTICS_SERVER_SLOW": "0"}
    )
    assert limits.wall_ms == 500
    assert limits.bytes == SlowThresholds().bytes
    assert limits.enabled is False
    assert SlowThresholds.from_env({}).enabled is True


def test_rate_limit_per_path_and_per_minute():
    now = [0.0]
    rate = _RateLimit(lambda: now[0])
    assert rate.allow("GET", "/a")
    assert not rate.allow("GET", "/a")
    assert rate.allow("POST", "/a")
    now[0] = 31
    assert rate.allow("GET", "/a")
    allowed = sum(rate.allow("GET", f"/p{i}") for i in range(100))
    assert allowed == 30 - 3  # three records already in this minute


def test_report_message_and_detail():
    timing = RequestTiming("GET", "/api/v1/x", 200, "transfer", "webui", 4210.04, 12.0, 30.0, 5 * 1_048_576)
    message, detail = timing_report(timing)
    assert message == "GET /api/v1/x · 4.2 s · 5.0 MB"
    assert detail["tag"] == "transfer"
    assert detail["metrics"] == {"wall_ms": 4210.0, "cpu_ms": 30.0, "bytes": 5 * 1_048_576, "ttfb_ms": 12.0}

