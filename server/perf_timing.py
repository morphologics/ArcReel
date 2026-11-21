"""YouReel: server-side timing for Settings → Options → Diagnostics. A pure ASGI middleware measures every HTTP request
(wall time, response bytes, the process's CPU time over the request) and records the slow or very large answers as
``server_slow`` reports in the client error table, next to what the browser reported about the same moment (``perf`` /
``jank``), so a machine that clogs shows which requests were heavy on the server.

What is recorded, and when:

- ``api`` answers (JSON and the like): wall time over ``DIAGNOSTICS_SERVER_SLOW_MS`` (default 2000) or a body over
  ``DIAGNOSTICS_SERVER_SLOW_BYTES`` (default 20 MiB);
- ``transfer`` answers (media, files, packs, downloads: a media / zip / octet-stream content type, a 206 range answer,
  an attachment, or a path under ``/files/``, ``/oreelpack``, ``…/download``; anything outside ``/api/``): they are
  long and large by nature, so only over ``DIAGNOSTICS_SERVER_SLOW_TRANSFER_MS`` (default 30000) or
  ``DIAGNOSTICS_SERVER_SLOW_TRANSFER_BYTES`` (default 500 MiB);
- ``stream`` answers (``text/event-stream``: they stay open for as long as the page does): never by their length,
  only when the first byte took over ``DIAGNOSTICS_SERVER_SLOW_MS`` (``ttfb_ms``);
- never: ``/health``, the diagnostics routes themselves, websockets. ``DIAGNOSTICS_SERVER_SLOW=0`` turns it all off.

A record holds the method, the path without query string, status, ``tag`` (api / transfer / stream), the client
(``X-Client``, default ``webui``) and ``metrics``: ``wall_ms``, ``ttfb_ms``, ``cpu_ms``, ``bytes``. The same method +
path is recorded at most once per 30 s, and at most 30 records per minute in all, so a machine that is slow for
everything doesn't add a database write to every request. The write runs in its own task after the answer has gone;
it never delays or fails a request.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lib.billing.call_client import is_valid_client_name

logger = logging.getLogger(__name__)

SWITCH_ENV = "DIAGNOSTICS_SERVER_SLOW"
SLOW_MS_ENV = "DIAGNOSTICS_SERVER_SLOW_MS"
SLOW_BYTES_ENV = "DIAGNOSTICS_SERVER_SLOW_BYTES"
TRANSFER_MS_ENV = "DIAGNOSTICS_SERVER_SLOW_TRANSFER_MS"
TRANSFER_BYTES_ENV = "DIAGNOSTICS_SERVER_SLOW_TRANSFER_BYTES"

EXCLUDED_PATHS: frozenset[str] = frozenset({"/health"})
EXCLUDED_PREFIXES: tuple[str, ...] = ("/api/v1/diagnostics/",)
_TRANSFER_PATH = re.compile(r"/files/|/oreelpack|/download\b|/export\b|/media/")
_TRANSFER_TYPES = ("video/", "audio/", "image/", "font/", "application/zip", "application/octet-stream")

SAME_PATH_GAP_S = 30.0
RECORDS_PER_MINUTE = 30


@dataclass(frozen=True)
class SlowThresholds:
    """When an answer counts as slow; 0 turns that dimension off."""

    wall_ms: float = 2000
    bytes: int = 20 * 1024 * 1024
    transfer_wall_ms: float = 30_000
    transfer_bytes: int = 500 * 1024 * 1024
    enabled: bool = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SlowThresholds:
        env = environ if environ is not None else os.environ
        default = cls()

        def number(name: str, fallback: float) -> float:
            try:
                return float(env.get(name) or fallback)
            except ValueError:
                return fallback

        return cls(
            wall_ms=number(SLOW_MS_ENV, default.wall_ms),
            bytes=int(number(SLOW_BYTES_ENV, default.bytes)),
            transfer_wall_ms=number(TRANSFER_MS_ENV, default.transfer_wall_ms),
            transfer_bytes=int(number(TRANSFER_BYTES_ENV, default.transfer_bytes)),
            enabled=(env.get(SWITCH_ENV) or "1").strip().lower() not in ("0", "false", "off", "no"),
        )


@dataclass(frozen=True)
class RequestTiming:
    """One finished HTTP request as measured."""

    method: str
    path: str
    status: int
    tag: str  # api | transfer | stream
    client: str
    wall_ms: float
    ttfb_ms: float | None
    cpu_ms: float
    bytes: int


def is_excluded(path: str) -> bool:
    return path in EXCLUDED_PATHS or path.startswith(EXCLUDED_PREFIXES)


def classify(path: str, status: int, headers: Mapping[str, str]) -> str:
    """``stream`` (event stream), ``transfer`` (media / files / downloads) or ``api``."""
    content_type = headers.get("content-type", "").lower()
    if content_type.startswith("text/event-stream"):
        return "stream"
    if (
        status == 206
        or content_type.startswith(_TRANSFER_TYPES)
        or "attachment" in headers.get("content-disposition", "").lower()
        or not path.startswith("/api/")
        or _TRANSFER_PATH.search(path)
    ):
        return "transfer"
    return "api"


def is_slow(timing: RequestTiming, limits: SlowThresholds) -> bool:
    def over(value: float | None, limit: float) -> bool:
        return limit > 0 and value is not None and value > limit

    if timing.tag == "stream":
        return over(timing.ttfb_ms, limits.wall_ms)
    if timing.tag == "transfer":
        return over(timing.wall_ms, limits.transfer_wall_ms) or over(timing.bytes, limits.transfer_bytes)
    return over(timing.wall_ms, limits.wall_ms) or over(timing.bytes, limits.bytes)


def _client(scope: Scope) -> str:
    values = [value for name, value in scope.get("headers") or [] if name == b"x-client"]
    if not values:
        return "webui"
    text = values[0].decode("latin-1")
    return text if len(values) == 1 and is_valid_client_name(text) else "invalid"


def _megabytes(size: int) -> str:
    return f"{size / 1_048_576:.1f} MB"


def timing_report(timing: RequestTiming) -> tuple[str, dict[str, Any]]:
    """The message and ``detail`` of a ``server_slow`` report."""
    shown = timing.ttfb_ms if timing.tag == "stream" and timing.ttfb_ms is not None else timing.wall_ms
    parts = [f"{timing.method} {timing.path}", f"{shown / 1000:.1f} s"]
    if timing.tag == "stream":
        parts[-1] += " to first byte"
    if timing.bytes >= 1_048_576:
        parts.append(_megabytes(timing.bytes))
    metrics: dict[str, Any] = {"wall_ms": round(timing.wall_ms, 1), "cpu_ms": round(timing.cpu_ms, 1)}
    metrics["bytes"] = timing.bytes
    if timing.ttfb_ms is not None:
        metrics["ttfb_ms"] = round(timing.ttfb_ms, 1)
    detail = {
        "method": timing.method,
        "api_path": timing.path,
        "status": timing.status,
        "tag": timing.tag,
        "client": timing.client,
        "metrics": metrics,
    }
    return " · ".join(parts), detail


async def write_timing(timing: RequestTiming) -> None:
    """Store one ``server_slow`` report (own session, committed); failures are logged, never raised."""
    # Imported here: the database layer is not needed to build the app's middleware stack
    from lib.db import safe_session_factory
    from lib.db.repositories.client_error_repo import ClientErrorReportRepository, NewClientErrorReport

    message, detail = timing_report(timing)
    try:
        async with safe_session_factory() as session:
            report = NewClientErrorReport(
                kind="server_slow", message=message, detail=detail, user_agent=None, app_build=None
            )
            await ClientErrorReportRepository(session).add_many([report])
            await session.commit()
    except Exception:  # a diagnostics write must never surface; it is only logged
        logger.debug("server_slow: could not store %s %s", timing.method, timing.path, exc_info=True)


class _RateLimit:
    """At most one record per method + path per SAME_PATH_GAP_S, and RECORDS_PER_MINUTE in all."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: dict[tuple[str, str], float] = {}
        self._window_start = 0.0
        self._in_window = 0

    def allow(self, method: str, path: str) -> bool:
        now = self._clock()
        if now - self._window_start >= 60:
            self._window_start = now
            self._in_window = 0
            # forget old paths too, so the map stays small
            self._last = {key: at for key, at in self._last.items() if now - at < SAME_PATH_GAP_S}
        last = self._last.get((method, path))
        if last is not None and now - last < SAME_PATH_GAP_S:
            return False
        if self._in_window >= RECORDS_PER_MINUTE:
            return False
        self._last[(method, path)] = now
        self._in_window += 1
        return True


class ServerTimingMiddleware:
    """Pure ASGI middleware recording slow / large answers (see the module docstring).

    ``writer`` stores one record (default :func:`write_timing`); ``thresholds`` default to the environment, read once.
    The per-request cost is two clock reads at the start, a header scan at the response start and an integer add per
    body chunk.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        writer: Callable[[RequestTiming], Awaitable[None]] | None = None,
        thresholds: SlowThresholds | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self._writer = writer or write_timing
        self._limits = thresholds or SlowThresholds.from_env()
        self._rate = _RateLimit(clock)
        self._tasks: set[asyncio.Task[None]] = set()

    def __call__(self, scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        # A plain function like EngineTokenMiddleware, not a coroutine: while the route runs, no frame of this module
        # is on the stack (the test suite's event-loop blocking guard charges a blocking call to the innermost server
        # coroutine on the stack; an async wrapper here would be blamed for every route). Only the wrapped ``send``
        # below is a coroutine, and it runs just while a message is handed on. The measurement ends at the last body
        # chunk (a stream: at its first byte); a request that fails before answering is not recorded.
        if not self._limits.enabled or scope["type"] != "http" or is_excluded(scope.get("path", "")):
            return self.app(scope, receive, send)
        start = time.perf_counter()
        # process_time() is the CPU time of the whole process (every thread), not of this request alone: with other
        # requests or background work running at the same time it over-counts. Good enough to tell "the server was
        # busy computing" from "it was waiting" (disk, network, a lock) for a slow request.
        cpu_start = time.process_time()
        path = scope.get("path", "")
        state: dict[str, Any] = {"status": 500, "headers": {}, "ttfb": None, "bytes": 0, "done": False}

        async def counting_send(message: Message) -> None:
            await send(message)
            if state["done"]:
                return
            if message["type"] == "http.response.start":
                state["status"] = message.get("status", 500)
                state["headers"] = {
                    name.decode("latin-1").lower(): value.decode("latin-1")
                    for name, value in message.get("headers") or []
                    if name.lower() in (b"content-type", b"content-disposition")
                }
                state["ttfb"] = (time.perf_counter() - start) * 1000
                if state["headers"].get("content-type", "").lower().startswith("text/event-stream"):
                    state["done"] = True
                    self._finish(scope, path, start, cpu_start, state)
            elif message["type"] == "http.response.body":
                state["bytes"] += len(message.get("body", b""))
                if not message.get("more_body", False):
                    state["done"] = True
                    self._finish(scope, path, start, cpu_start, state)

        return self.app(scope, receive, counting_send)

    def _finish(self, scope: Scope, path: str, start: float, cpu_start: float, state: dict[str, Any]) -> None:
        try:
            timing = RequestTiming(
                method=scope.get("method", "GET"),
                path=path,
                status=state["status"],
                tag=classify(path, state["status"], state["headers"]),
                client=_client(scope),
                wall_ms=(time.perf_counter() - start) * 1000,
                ttfb_ms=state["ttfb"],
                cpu_ms=(time.process_time() - cpu_start) * 1000,
                bytes=state["bytes"],
            )
            if not is_slow(timing, self._limits) or not self._rate.allow(timing.method, timing.path):
                return
            task = asyncio.get_running_loop().create_task(self._writer(timing))
            # keep a reference until done (a task only weakly referenced can be collected mid-way)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:  # measuring must never break a request
            logger.debug("server_slow: measuring %s failed", path, exc_info=True)


__all__ = [
    "RequestTiming",
    "ServerTimingMiddleware",
    "SlowThresholds",
    "classify",
    "is_excluded",
    "is_slow",
    "timing_report",
    "write_timing",
]
