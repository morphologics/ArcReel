"""Light read routes: the asset list, one asset, one episode's shot list (``GET /projects?covers=1``: ``projects.py``).

Cheap alternatives to ``GET /projects/{name}`` for clients that show one card or one board; see
:mod:`server.services.project.light_reads` for what each one reads.

轻量读取路由：单个资产、一集的分镜列表（``GET /projects?covers=1`` 在 ``projects.py``）。

给只显示一张卡片或一块画板的客户端，替代 ``GET /projects/{name}`` 的廉价读取；各自读什么见
:mod:`server.services.project.light_reads`。
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Query

from lib.infra.api_errors import NotFoundError
from lib.project.project_manager import get_project_manager
from server.services.project.light_reads import (
    ArtifactCurrency,
    AssetNotFound,
    EpisodeNotFound,
    EpisodeShotList,
    ProjectAssetList,
    ProjectAssetResponse,
    list_project_assets,
    read_episode_shots,
    read_project_asset,
)

router = APIRouter()

AssetType = Literal["character", "scene", "prop", "product"]

#: Not-found key per asset type (existing keys; scenes keep the historical ``project_scene_*`` prefix).
#: 各资产类型的不存在 key（沿用既有 key；场景保留历史前缀 ``project_scene_*``）。
_ASSET_NOT_FOUND_KEYS: dict[str, str] = {
    "character": "character_not_found",
    "scene": "project_scene_not_found",
    "prop": "prop_not_found",
    "product": "product_not_found",
}

_CURRENCY_DESCRIPTION = (
    "registered (default): registered in the artifact manifest and present, cheap, never reports stale; "
    "verified: compared with the canonical state, reports stale, hashes images (slower)"
)


@router.get("/projects/{project_name}/assets", response_model=ProjectAssetList)
async def list_assets(
    project_name: str,
    asset_type: list[AssetType] | None = Query(
        None, alias="type", description="repeat to choose several (type=character&type=scene); default: all four"
    ),
    currency: ArtifactCurrency = Query("registered", description=_CURRENCY_DESCRIPTION),
) -> ProjectAssetList:
    """The project's assets with their current sheet (path, URL, status, version), short description and derivative
    count. Reads no scripts: a cheap alternative to ``GET /projects/{name}`` for asset galleries."""

    def _sync() -> ProjectAssetList:
        return list_project_assets(get_project_manager(), project_name, asset_type, currency=currency)

    try:
        return await asyncio.to_thread(_sync)
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=project_name) from exc


@router.get("/projects/{project_name}/assets/{asset_type}/{name}", response_model=ProjectAssetResponse)
async def get_project_asset(
    project_name: str,
    asset_type: AssetType,
    name: str,
    currency: ArtifactCurrency = Query("registered", description=_CURRENCY_DESCRIPTION),
) -> ProjectAssetResponse:
    """One project asset: stored fields, current sheet (path, URL, status, version) and derivative summaries."""

    def _sync() -> ProjectAssetResponse:
        return read_project_asset(get_project_manager(), project_name, asset_type, name, currency=currency)

    try:
        return await asyncio.to_thread(_sync)
    except AssetNotFound as exc:
        raise NotFoundError(_ASSET_NOT_FOUND_KEYS[asset_type], name=name) from exc
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=project_name) from exc


@router.get("/projects/{project_name}/episodes/{episode}/shots", response_model=EpisodeShotList)
async def get_episode_shots(
    project_name: str,
    episode: int,
    include: Literal["prompts"] | None = Query(
        None, description="prompts: also return each item's prompts (image_prompt / video_prompt, or a unit's text)"
    ),
    currency: ArtifactCurrency = Query("registered", description=_CURRENCY_DESCRIPTION),
) -> EpisodeShotList:
    """One episode's items in script order: id, kind, duration, per-stage artifact status / file / version,
    thumbnail, prompt presence. Reads only this episode's script."""

    def _sync() -> EpisodeShotList:
        return read_episode_shots(
            get_project_manager(),
            project_name,
            episode,
            include_prompts=include == "prompts",
            currency=currency,
        )

    try:
        return await asyncio.to_thread(_sync)
    except EpisodeNotFound as exc:
        raise NotFoundError("episode_not_found", episode=episode) from exc
    except FileNotFoundError as exc:
        raise NotFoundError("project_not_found", name=project_name) from exc


__all__ = ["router"]
