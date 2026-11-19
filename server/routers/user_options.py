"""User options saved for the studio.

Options the Settings "Options" panel saves on the server, so that every device the member opens the studio on
shares them. Options tied to one device (language, theme, touch behaviour) stay in that browser and never reach
this route.

Stored as one JSON object in the instance-level ``SystemSetting`` table under ``user_options``. Only keys the
member has set are stored and returned; the frontend's option registry holds the defaults. PUT merges the given
keys into the stored object; ``null`` removes a key (back to its default). Unknown keys and values outside a key's
allowed set are rejected with 422.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from lib.config.service import ConfigService
from lib.db import get_async_session
from server.dependencies import get_config_service

router = APIRouter()

USER_OPTIONS_KEY = "user_options"


class UserOptions(BaseModel):
    """Known studio options and their allowed values; a missing (or null) key means the default."""

    model_config = ConfigDict(extra="forbid")

    default_editor: Literal["arcreel", "openreel"] | None = None
    mode: Literal["easy", "advanced"] | None = None
    #: YouReel (owner 2026-11-19): the YouReel Editor's "Include motion transitions" (set in the Editor's Settings; the
    #: studio only keeps it for the member). Missing = off.
    motion_transitions: Literal["on", "off"] | None = None


async def _read(svc: ConfigService) -> dict[str, str]:
    raw = await svc.get_setting(USER_OPTIONS_KEY)
    if not raw:
        return {}
    stored = json.loads(raw)
    # Keys a later version no longer knows are dropped instead of failing the whole read
    known = {key: value for key, value in stored.items() if key in UserOptions.model_fields}
    return UserOptions.model_validate(known).model_dump(exclude_none=True)


@router.get("/user-options", response_model=UserOptions, response_model_exclude_none=True)
async def get_user_options(
    svc: Annotated[ConfigService, Depends(get_config_service)],
) -> dict[str, str]:
    """The studio options the member has set."""
    return await _read(svc)


@router.put("/user-options", response_model=UserOptions, response_model_exclude_none=True)
async def update_user_options(
    patch: UserOptions,
    svc: Annotated[ConfigService, Depends(get_config_service)],
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, str]:
    """Merge the given keys into the stored options and return the result."""
    merged = await _read(svc)
    for key in patch.model_fields_set:
        value = getattr(patch, key)
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    await svc.set_setting(USER_OPTIONS_KEY, json.dumps(merged, ensure_ascii=False, sort_keys=True))
    await session.commit()
    return merged
