"""The member's own text templates for the YouReel Editor (owner 2026-11-18).

A template is a text's whole look, kept to be reused in any project: font, weight, colours, outline, shadow, background,
spacing, alignment (``style``, the Editor's text style as it is), its size relative to the frame (``size``: a share of the
frame's width or height) and its place in the frame (``position``: 0..1 across and down). The Editor ships the built-in
templates itself; this route keeps the ones the member saved, so they follow the member to every project and device.

Stored as one JSON list in the instance-level ``SystemSetting`` table under ``editor_text_templates`` (as the user
options). GET returns the list; PUT replaces it (the Editor sends the whole list after adding, renaming or deleting).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from lib.config.service import ConfigService
from lib.db import get_async_session
from server.dependencies import get_config_service

router = APIRouter()

TEXT_TEMPLATES_KEY = "editor_text_templates"
MAX_TEMPLATES = 200
#: one template's style, as JSON (the Editor's text style is small; this only stops abuse)
MAX_STYLE_BYTES = 8 * 1024


class TemplateSize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: "width": the text's widest line spans this share of the frame's width; "height": the font is this share of its height
    mode: Literal["width", "height"]
    fraction: float = Field(gt=0, le=1)


class TemplatePosition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class TextTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    text: str = Field(max_length=200)
    style: dict[str, Any]
    size: TemplateSize
    position: TemplatePosition

    @field_validator("style")
    @classmethod
    def _small_style(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value)) > MAX_STYLE_BYTES:
            raise ValueError("style too large")
        return value


class TextTemplates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    templates: list[TextTemplate] = Field(max_length=MAX_TEMPLATES)


async def _read(svc: ConfigService) -> list[dict[str, Any]]:
    raw = await svc.get_setting(TEXT_TEMPLATES_KEY)
    if not raw:
        return []
    kept = []
    # a template a later version can't read is dropped instead of failing the whole list
    for item in json.loads(raw):
        try:
            kept.append(TextTemplate.model_validate(item).model_dump())
        except ValueError:
            continue
    return kept


@router.get("/editor/text-templates", response_model=TextTemplates)
async def get_text_templates(svc: Annotated[ConfigService, Depends(get_config_service)]) -> dict[str, Any]:
    """The member's own text templates."""
    return {"templates": await _read(svc)}


@router.put("/editor/text-templates", response_model=TextTemplates)
async def put_text_templates(
    body: TextTemplates,
    svc: Annotated[ConfigService, Depends(get_config_service)],
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """Replace the member's own text templates with the given list."""
    templates = [template.model_dump() for template in body.templates]
    await svc.set_setting(TEXT_TEMPLATES_KEY, json.dumps(templates, ensure_ascii=False))
    await session.commit()
    return {"templates": templates}
