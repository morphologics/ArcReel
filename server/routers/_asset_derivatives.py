"""Routes for derivative sub-resources under an asset entry (only registered for types with
``AssetSpec.supports_derivatives`` enabled).

Derivatives hang under the ``DERIVATIVES_FIELD`` table inside the base look entry, share the base look's name and
identity, are unique only within their entry, and are referenced as ``base look name/derivative name``. Add / edit
description / delete go through ``ProjectManager.update_asset_entry``, sharing the same project lock / final
artifact boundary as PATCH on base look fields; rename must also rewrite ``base look name/old derivative name``
references in scripts, so it goes through the cascading transaction of ``ProjectManager.rename_asset_derivative``.

This module only does registration (add, edit description, delete) and rename; generation, versions and staleness of
derivative asset sheets are not handled here. Rename and delete are the exception: that image's on-disk location
includes the derivative name, so rename must also move the image, version history and manifest keys
(``lib.project.asset_derivative_rename``), and delete must also move all three into the project trash
(``lib.project.asset_trash``); both belong to the same commit as the registration write.

资产条目下的衍生子资源路由（``AssetSpec.supports_derivatives`` 开启的类型才注册）。

衍生挂在本体条目内的 ``DERIVATIVES_FIELD`` 表下，共享本体的名字与身份，只在所属条目内
唯一，引用写作 ``本体名/衍生名``。新增 / 改描述 / 删除经 ``ProjectManager.update_asset_entry``，
与本体字段的 PATCH 共用同一条项目锁 / 正式产物边界；改名要连带改写剧本里的
``本体名/旧衍生名`` 引用，走 ``ProjectManager.rename_asset_derivative`` 的级联事务。

本模块只做登记（新增、改描述、删除）与改名；衍生资产图的生成、版本与过期判定不在此处。
改名与删除是例外：那张图的落盘坐标含衍生名，改名要连带搬图、版本历史与清单键
（``lib.project.asset_derivative_rename``），删除要连带把三者移进项目回收站（``lib.project.asset_trash``），
两者都与登记写入同属一次提交。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from lib.infra.api_errors import NotFoundError, UnprocessableError
from lib.infra.async_thread import run_sync_transaction
from lib.project import asset_trash
from lib.project.asset_derivatives import ensure_derivative_table, find_derivative_name
from lib.project.asset_rename import (
    AssetRenameConflictError,
    AssetRenameFileCollisionError,
    AssetRenameHistoryCollisionError,
    AssetRenameNotFoundError,
)
from lib.project.asset_types import (
    ASSET_SPECS,
    DERIVATIVES_FIELD,
    AssetSpec,
    resolve_asset_key,
    validate_asset_name,
)
from lib.project.project_change_hints import project_change_source
from lib.project.project_manager import ProjectManager
from server.i18n import Translator

logger = logging.getLogger(__name__)


class _DerivativeCreateRequest(BaseModel):
    """新增衍生：名字在所属本体内唯一，描述是相对本体的外观变化。"""

    name: str
    description: str = ""


class _DerivativeUpdateRequest(BaseModel):
    """更新衍生描述。改名走 ``/rename``，资产图路径由生成侧写入，均不在此。"""

    description: str


class _DerivativeRenameRequest(BaseModel):
    new_name: str


class _DerivativeExists(Exception):
    """The target derivative name is already taken by another derivative under the same base look (differing only in
    case counts too).

    目标衍生名已被同一本体下的另一个衍生占用（只差大小写也算）。"""

    def __init__(self, name: str):
        self.name = name
        super().__init__(name)


class _DerivativeMissing(Exception):
    """The derivative addressed by name does not exist in the base look's derivative table.

    按名寻址的衍生在本体的衍生表里不存在。"""

    def __init__(self, name: str):
        self.name = name
        super().__init__(name)


def _validated_name(raw: str) -> str:
    """Derivative names reuse the asset name validation: they are also spliced into single-segment file names and
    single-segment route parameters.

    衍生名沿用资产名校验：它同样被拼进单段文件名与单段路由参数。"""
    try:
        return validate_asset_name(raw)
    except ValueError as exc:
        raise UnprocessableError("asset_derivative_invalid_name", name=raw) from exc


def _resolve_owner_key(manager: ProjectManager, asset_type: str, project_name: str, entry_name: str) -> str | None:
    """Get the base look entry's actual on-disk name; return ``None`` when missing, and the following write reports 404
    uniformly.

    取本体条目的落盘真名；缺失时返回 ``None``，由随后的写入统一报 404。"""
    bucket = manager.load_project(project_name).get(ASSET_SPECS[asset_type].bucket_key)
    return resolve_asset_key(bucket, entry_name)


def register_derivative_routes(
    router: APIRouter,
    *,
    spec: AssetSpec,
    not_found_key: str,
    pm_getter: Callable[[], ProjectManager],
) -> None:
    """Register the four derivative sub-resource endpoints on that asset type's router.

    把衍生子资源的四个端点注册到该资产类型的路由上。"""

    base = f"/projects/{{project_name}}/{spec.subdir}/{{entry_name}}/{DERIVATIVES_FIELD}"
    asset_type = spec.asset_type

    async def _run(
        project_name: str,
        entry_name: str,
        write: Callable[[ProjectManager, Callable[[Path], None]], dict[str, Any]],
        _t: Translator,
        on_commit: Callable[[ProjectManager, str], None] | None = None,
    ) -> dict[str, Any]:
        """Run one derivative write and map domain exceptions to user-facing responses.

        Rename goes through the cascading transaction of ``rename_asset_derivative``, the other three endpoints
        through ``update_asset_entry``; both write paths share one exception mapping instead of each endpoint
        handling it separately. ``on_commit`` belongs to the same commit as the write and receives the base look's
        actual on-disk name -- the delete path uses it to clean up that image.

        跑一次衍生写入，把领域异常映射为面向用户的响应。

        改名走 ``rename_asset_derivative`` 的级联事务，其余三个端点走
        ``update_asset_entry``；两条写入路径的异常映射同一份，端点不各自兜一遍。
        ``on_commit`` 与该写入同属一次提交，收到本体的落盘真名——删除路径据此清理那张图。
        """
        try:

            def _sync():
                manager = pm_getter()
                with project_change_source("webui"):
                    # Resolve the base look's actual on-disk name once before writing: cascading cleanup uses it to
                    # build the derivative image directory, and the name in the request may be a different equivalent
                    # encoding.
                    # 本体的落盘真名在写入前解析一次：级联清理要用它拼衍生图的目录，而
                    # 请求里的名字可能是另一种等价编码形式。
                    owner_key = _resolve_owner_key(manager, asset_type, project_name, entry_name)

                    def _commit(_project_file: Path) -> None:
                        if on_commit is not None and owner_key is not None:
                            on_commit(manager, owner_key)

                    return write(manager, _commit)

            return await run_sync_transaction(_sync)
        except _DerivativeExists as exc:
            raise UnprocessableError("asset_derivative_already_exists", name=exc.name) from exc
        except _DerivativeMissing as exc:
            raise NotFoundError("asset_derivative_not_found", name=exc.name) from exc
        except AssetRenameFileCollisionError as exc:
            # Derivative images share these two conflict messages with base look asset sheets: the shape of the
            # refusal and what the user has to do are exactly the same.
            # 衍生图与本体资产图共用这两条冲突文案：拒绝的形状与用户要做的事完全一样。
            raise HTTPException(
                status_code=409, detail=_t("asset_rename_file_conflict", filename=exc.destination.name)
            ) from exc
        except AssetRenameHistoryCollisionError as exc:
            raise HTTPException(
                status_code=409, detail=_t("asset_rename_history_conflict", name=exc.resource_id)
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=_t(not_found_key, name=entry_name)) from exc
        except FileNotFoundError as exc:
            raise NotFoundError("project_not_found", name=project_name) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("请求处理失败")
            raise HTTPException(status_code=500, detail=_t("internal_server_error")) from exc

    @router.post(base)
    async def add_derivative(
        project_name: str,
        entry_name: str,
        req: _DerivativeCreateRequest,
        _t: Translator,
    ):
        name = _validated_name(req.name)

        def _mutate(entry: dict[str, Any]) -> None:
            table = ensure_derivative_table(entry)
            existing = find_derivative_name(table, name)
            if existing is not None:
                raise _DerivativeExists(existing)
            table[name] = {"description": req.description, spec.sheet_field: ""}

        result = await _run(
            project_name,
            entry_name,
            lambda manager, commit: manager.update_asset_entry(
                asset_type, project_name, entry_name, _mutate, on_commit=commit
            ),
            _t,
        )
        return {"success": True, asset_type: result}

    @router.patch(f"{base}/{{derivative_name}}")
    async def update_derivative(
        project_name: str,
        entry_name: str,
        derivative_name: str,
        req: _DerivativeUpdateRequest,
        _t: Translator,
    ):
        def _mutate(entry: dict[str, Any]) -> None:
            table = ensure_derivative_table(entry)
            key = resolve_asset_key(table, derivative_name)
            if key is None:
                raise _DerivativeMissing(derivative_name)
            derivative = table[key]
            if not isinstance(derivative, dict):
                raise ValueError(f"derivative {key!r} must be an object")
            derivative["description"] = req.description

        result = await _run(
            project_name,
            entry_name,
            lambda manager, commit: manager.update_asset_entry(
                asset_type, project_name, entry_name, _mutate, on_commit=commit
            ),
            _t,
        )
        return {"success": True, asset_type: result}

    @router.post(f"{base}/{{derivative_name}}/rename")
    async def rename_derivative(
        project_name: str,
        entry_name: str,
        derivative_name: str,
        req: _DerivativeRenameRequest,
        _t: Translator,
    ):
        """改名是一次级联事务：本体条目内的键，与全部剧本 / 草稿里的 ``本体名/旧衍生名`` 引用。"""
        new_name = _validated_name(req.new_name)

        def _write(manager: ProjectManager, _commit: Callable[[Path], None]) -> dict[str, Any]:
            try:
                return manager.rename_asset_derivative(asset_type, project_name, entry_name, derivative_name, new_name)
            except AssetRenameNotFoundError as exc:
                raise _DerivativeMissing(derivative_name) from exc
            except AssetRenameConflictError as exc:
                raise _DerivativeExists(exc.conflict_name) from exc

        result = await _run(project_name, entry_name, _write, _t)
        return {"success": True, asset_type: result}

    @router.delete(f"{base}/{{derivative_name}}")
    async def delete_derivative(
        project_name: str,
        entry_name: str,
        derivative_name: str,
        _t: Translator,
    ):
        deleted: list[str] = []
        before: dict[str, bytes] = {}

        def _write(manager: ProjectManager, commit: Callable[[Path], None]) -> dict[str, Any]:
            project_dir = manager.get_project_path(project_name)

            def _mutate(entry: dict[str, Any]) -> None:
                table = ensure_derivative_table(entry)
                key = resolve_asset_key(table, derivative_name)
                if key is None:
                    raise _DerivativeMissing(derivative_name)
                before.update(asset_trash.state_before(project_dir))
                del table[key]
                deleted.append(key)

            return manager.update_asset_entry(
                asset_type,
                project_name,
                entry_name,
                _mutate,
                on_commit=commit,
                formal_paths=asset_trash.state_paths(project_dir),
            )

        def _trash(manager: ProjectManager, owner_key: str) -> None:
            asset_trash.trash_asset_derivative(manager.get_project_path(project_name), owner_key, deleted[0], before)

        await _run(project_name, entry_name, _write, _t, _trash)
        return {"success": True, "message": _t("asset_derivative_deleted", name=derivative_name)}
