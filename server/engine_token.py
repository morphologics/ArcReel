"""Engine token: an optional shared secret for deployments that put the server behind a reverse proxy with its own
login and run it with ``AUTH_ENABLED=false``.

With login off, anything that can open a connection to the server's port controls it. Setting ``ENGINE_PROXY_TOKEN``
narrows that to callers that know the token: every HTTP request must then carry the header ``X-Engine-Token`` with the
same value (compared in constant time), else the answer is 401 with the usual error body (``detail`` / ``code`` /
``params``): code ``engine_token_required`` when the header is missing, ``engine_token_invalid`` when it is wrong.
The reverse proxy adds the header on the browser path; server-side clients send it themselves.

Unset or empty = no check at all (the behaviour without this module).

Always let through, without a token:

- ``GET /health`` (container health checks run inside the container and know no token);
- requests from the server's own loopback (``127.0.0.1`` / ``::1``) that carry no forwarding header
  (``X-Forwarded-For``, ``X-Real-IP``, ``Forwarded``): callers inside the same network namespace, such as the
  embedded agent's skill scripts calling ``ARCREEL_API_BASE`` (``http://127.0.0.1:1241/api/v1``). In a container,
  connections from the host never arrive from loopback (a published port is relayed from the bridge gateway
  address), so this does not open the door to host processes. A proxy that runs in the same network namespace
  (for example a dev server proxying ``/api`` to the backend) passes the forwarding headers of the request it
  relays, so its requests are not exempt and need the token like any other. Do not set the token on a server
  started with host networking: there, every local process is "loopback".
- CORS preflight requests (``OPTIONS`` with ``Access-Control-Request-Method``) are answered by the CORS middleware,
  which sits outside this one, before they get here; browsers never add custom headers to a preflight.

The header is removed from the request before it reaches the routes, so no route can echo or log it.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
from collections.abc import Awaitable, Mapping

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

ENGINE_TOKEN_ENV = "ENGINE_PROXY_TOKEN"
ENGINE_TOKEN_HEADER = b"x-engine-token"

#: Paths that never need the token (exact match).
EXEMPT_PATHS: frozenset[str] = frozenset({"/health"})

#: Headers a relaying proxy adds; a loopback request carrying one of them was relayed, so it is not exempt.
_FORWARDING_HEADERS: frozenset[bytes] = frozenset({b"x-forwarded-for", b"x-real-ip", b"forwarded"})


def configured_engine_token(environ: Mapping[str, str] | None = None) -> str:
    """The configured token, or ``""`` when the check is off (unset, empty or only whitespace)."""
    return ((environ if environ is not None else os.environ).get(ENGINE_TOKEN_ENV) or "").strip()


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool(ip.is_loopback or (mapped is not None and mapped.is_loopback))


def is_local_unforwarded(scope: Scope) -> bool:
    """True for a request from the server's own loopback that no proxy relayed (see the module docstring)."""
    client = scope.get("client")
    if not client or not _is_loopback(client[0]):
        return False
    return not any(name in _FORWARDING_HEADERS for name, _ in scope.get("headers") or [])


class EngineTokenMiddleware:
    """Pure ASGI middleware enforcing ``ENGINE_PROXY_TOKEN`` (see the module docstring).

    The token is read from the environment on every request (a dict lookup), so it follows the environment the
    process runs with and tests can switch it per case."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def __call__(self, scope: Scope, receive: Receive, send: Send) -> Awaitable[None]:
        # A plain function returning the awaitable of the next app (or of the refusal), not a coroutine: the check
        # needs no awaiting, and this way the middleware adds no frame to the request's stack (the event-loop
        # blocking guard of the test suite charges a blocking call to the innermost server coroutine on the stack).
        if scope["type"] not in ("http", "websocket"):
            return self.app(scope, receive, send)
        token = configured_engine_token()
        if not token or scope.get("path") in EXEMPT_PATHS or is_local_unforwarded(scope):
            return self.app(scope, receive, send)

        headers = scope.get("headers") or []
        sent = [value for name, value in headers if name == ENGINE_TOKEN_HEADER]
        if not sent:
            return self._deny(scope, send, "engine_token_required")
        # One header, equal to the token. compare_digest takes the same time wherever the first difference is.
        if len(sent) != 1 or not hmac.compare_digest(sent[0], token.encode("utf-8")):
            return self._deny(scope, send, "engine_token_invalid")

        # Passed: the routes never see the token.
        scope = dict(scope)
        scope["headers"] = [(name, value) for name, value in headers if name != ENGINE_TOKEN_HEADER]
        return self.app(scope, receive, send)

    async def _deny(self, scope: Scope, send: Send, code: str) -> None:
        client = scope.get("client")
        logger.warning(
            "engine token: %s %s refused (%s) from %s",
            scope.get("method", "WS"),
            scope.get("path"),
            code,
            client[0] if client else "?",
        )
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the handshake with 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        # Imported here: server.i18n pulls in the database layer, which this module does not need otherwise.
        from lib.i18n import _
        from server.i18n import get_locale

        locale = get_locale(Request(scope))
        body = json.dumps(
            {"detail": _(code, locale=locale), "code": code, "params": {}}, ensure_ascii=False
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = [
    "ENGINE_TOKEN_ENV",
    "EXEMPT_PATHS",
    "EngineTokenMiddleware",
    "configured_engine_token",
    "is_local_unforwarded",
]
