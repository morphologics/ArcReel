"""The studio agent's model for the editor's AI chat (OpenReel served next to the studio).

Route prefix: /api/v1/agent-llm

OpenReel's "Edit by chatting" sends Anthropic Messages requests from the browser. Next to the studio it sends them
here instead of to a provider with a key typed into the browser: the request is forwarded to the active Agent
credential's provider with that credential's key and main model (the same values the studio agent runs with), so the
key never reaches the browser. Usage is recorded like an agent turn (purpose: assistant session), under the project
the editor opened, priced by the same credential.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from lib.backends.providers import PROVIDER_ANTHROPIC, CallStatus
from lib.billing.ledger import Ledger
from lib.billing.metering import Usage, token_usage
from lib.config.anthropic_probe import anthropic_auth_headers
from lib.config.service import build_anthropic_env
from lib.config.url_utils import ANTHROPIC_MESSAGES_PATH, anthropic_endpoint_url
from lib.db import get_async_session
from lib.db.repositories.agent_credential_repo import AgentCredentialRepository
from lib.db.repositories.usage_repo import AgentModelCall
from lib.i18n.display_names import agent_provider_id
from lib.infra.httpx_shared import get_http_client
from lib.project.project_manager import ProjectManager, get_project_manager
from server.auth import CurrentUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent-llm", tags=["Agent model for the editor"])

#: Request body limit (base64 preview images from the editor's inspection tools make bodies large).
MAX_BODY_BYTES = 12 * 1024 * 1024
#: Output limit per request.
MAX_OUTPUT_TOKENS = 8192
#: Fields passed on to the provider; anything else (stream, metadata, a model of the browser's choosing) is dropped.
FORWARDED_FIELDS = ("system", "messages", "tools", "tool_choice", "max_tokens", "temperature", "top_p", "stop_sequences")
UPSTREAM_TIMEOUT_S = 180.0


class AgentLlmInfo(BaseModel):
    available: bool
    model: str | None = None
    provider: str | None = None


async def _agent_target(session: AsyncSession) -> tuple[str, str, str, Any, int | None, str | None]:
    """Base URL, key, model, pricing, credential id and provider name of the studio agent (409 when none is set)."""
    agent = await build_anthropic_env(session)
    base_url = agent.env.get("ANTHROPIC_BASE_URL", "").strip()
    api_key = agent.env.get("ANTHROPIC_API_KEY", "").strip()
    model = (agent.env.get("ANTHROPIC_MODEL") or agent.env.get("ANTHROPIC_DEFAULT_SONNET_MODEL") or "").strip()
    if not api_key or not model:
        raise HTTPException(status_code=409, detail="The studio agent has no model configured (Settings → Agent).")
    credential = await AgentCredentialRepository(session).get_active()
    return (
        base_url,
        api_key,
        model,
        agent.pricing,
        agent.pricing.credential_id,
        credential.display_name if credential is not None else None,
    )


@router.get("", response_model=AgentLlmInfo)
async def agent_llm_info(_user: CurrentUser, session: AsyncSession = Depends(get_async_session)) -> AgentLlmInfo:
    try:
        _, _, model, _, _, provider = await _agent_target(session)
    except HTTPException:
        return AgentLlmInfo(available=False)
    return AgentLlmInfo(available=True, model=model, provider=provider)


async def _known_project(name: str) -> bool:
    """Usage is recorded only under a real project of this studio (the header comes from the browser)."""
    try:
        ProjectManager.normalize_project_name(name)
    except ValueError:
        return False
    return await asyncio.to_thread(get_project_manager().project_exists, name)


def _mask(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _usage_call(model: str, usage: dict[str, Any]) -> AgentModelCall:
    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) and value > 0 else 0

    uncached = count("input_tokens")
    cache_read = count("cache_read_input_tokens")
    cache_write = count("cache_creation_input_tokens")
    output = count("output_tokens")
    total_input = uncached + cache_read + cache_write
    return AgentModelCall(
        model=model,
        requests=(
            Usage(
                quantities=token_usage(
                    input_tokens=total_input,
                    output_tokens=output,
                    cache_read_tokens=cache_read,
                    cache_write_5m_tokens=cache_write,
                )
            ),
        ),
        input_tokens=total_input,
        output_tokens=output,
        cache_read_tokens=cache_read,
        cache_write_5m_tokens=cache_write,
        cache_write_1h_tokens=0,
    )


@router.post("/messages")
async def agent_llm_messages(
    request: Request,
    user: CurrentUser,
    session: AsyncSession = Depends(get_async_session),
    x_youreel_project: str | None = Header(default=None),
    x_youreel_session: str | None = Header(default=None),
) -> JSONResponse:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="The request is too large.")
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="The request body is not JSON.") from exc
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body["messages"]:
        raise HTTPException(status_code=400, detail="An Anthropic Messages request with messages is expected.")

    base_url, api_key, model, pricing, credential_id, _ = await _agent_target(session)
    payload: dict[str, Any] = {key: body[key] for key in FORWARDED_FIELDS if key in body}
    payload["model"] = model
    requested = payload.get("max_tokens")
    payload["max_tokens"] = min(requested, MAX_OUTPUT_TOKENS) if isinstance(requested, int) and requested > 0 else 4096

    url = anthropic_endpoint_url(base_url, ANTHROPIC_MESSAGES_PATH)
    headers = {**anthropic_auth_headers(api_key), "content-type": "application/json"}
    try:
        upstream = await get_http_client().post(url, headers=headers, json=payload, timeout=UPSTREAM_TIMEOUT_S)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="The agent's model did not answer in time.") from exc
    except httpx.HTTPError as exc:
        logger.warning("agent-llm upstream unreachable url=%s error=%s", url, type(exc).__name__)
        raise HTTPException(status_code=502, detail="The agent's model could not be reached.") from exc
    logger.info("agent-llm url=%s status=%s", url, upstream.status_code)

    try:
        answer = upstream.json()
    except ValueError:
        answer = None
    if upstream.status_code >= 400 or not isinstance(answer, dict):
        message = "The agent's model returned an error."
        if isinstance(answer, dict) and isinstance(answer.get("error"), dict):
            detail = answer["error"].get("message")
            if isinstance(detail, str):
                message = _mask(detail, api_key)[:300]
        status = upstream.status_code if upstream.status_code >= 400 else 502
        return JSONResponse(status_code=status, content={"type": "error", "error": {"type": "upstream_error", "message": message}})

    usage = answer.get("usage")
    if isinstance(usage, dict) and x_youreel_project and await _known_project(x_youreel_project):
        try:
            await Ledger().record_agent_turn(
                project_name=x_youreel_project,
                session_id=f"openreel-{x_youreel_session or uuid.uuid4()}",
                prompt="OpenReel: edit by chatting",
                user_id=user.id,
                provider=agent_provider_id(credential_id) if credential_id is not None else PROVIDER_ANTHROPIC,
                pricing=pricing,
                status=CallStatus.SUCCESS,
                calls=[_usage_call(model, usage)],
            )
        except Exception:
            logger.exception("agent-llm usage could not be recorded project=%s", x_youreel_project)
    return JSONResponse(status_code=200, content=answer)
