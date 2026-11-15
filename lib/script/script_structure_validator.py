"""Script structure validator (pure function).

Converges the question "is a script dict structurally well-formed" into one place: feed in a dict, get back a
`ValidationResult`, no disk reads, no dependency on project state. The unified write entry, tests and future write surfaces all reuse it.

It validates against the structural Pydantic models (`lib.script.script_models`), not the FS-aware `DataValidator`
-- the latter reads disk and rejects legitimate half-finished drafts (storyboard images not yet generated). The models already
encode all structural constraints (required fields, `duration_seconds` range, id format, prompt shape, reference unit
shots<->duration consistency); this validator only "picks the right model by mode" and converts Pydantic's `ValidationError`
into `ValidationResult`, without duplicating any constraint -- a model change is a validation change (single source of truth).

剧本结构校验器（纯函数）。

把「一个剧本 dict 是否结构良构」这个判断收敛到唯一一处：喂入 dict、返回
`ValidationResult`，不读磁盘、不依赖项目状态。写盘统一入口、测试、未来其它写入面都复用它。

校验对象是结构层 Pydantic 模型（`lib.script.script_models`），而非 FS 感知的 `DataValidator`
——后者会读磁盘并拒绝合法的半成品草稿（分镜图尚未生成）。模型已编码所有结构约束
（必填、`duration_seconds` 范围、id 格式、prompt 形状、参考单元 shots↔duration 一致性），
本校验器只负责「按模式选对模型」并把 Pydantic 的 `ValidationError` 转成 `ValidationResult`，
不复制任何约束——模型变更即校验变更（单一真相源）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

# pydantic does not re-export ErrorDetails at top level, so we have to depend on pydantic-core directly.
# pydantic 顶层不 re-export ErrorDetails，只能直接依赖 pydantic-core。
from pydantic_core import ErrorDetails

from arcreel_market_core.validation_messages import ValidationMessage
from lib.infra.validation_messages import ValidationResult
from lib.script.script_models import (
    AdEpisodeScript,
    DramaEpisodeScript,
    NarrationEpisodeScript,
    ReferenceVideoScript,
)
from lib.script.script_skeleton import resolve_script_kind


class ScriptStructureValidationError(ValueError):
    """Script structure validation failed. Carries a `ValidationResult` for the router to turn into an i18n 4xx response.

    剧本结构校验失败。携带 `ValidationResult`，供 router 转 i18n 4xx 响应。"""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__("; ".join(result.errors) or "script structure invalid")


_KIND_MODEL: dict[str, type[BaseModel]] = {
    "video_units": ReferenceVideoScript,
    "scenes": DramaEpisodeScript,
    "segments": NarrationEpisodeScript,
    "shots": AdEpisodeScript,
}


def _select_model(script: dict[str, Any]) -> type[BaseModel]:
    """Decide which script model to use by mode; the decision logic is centralized in `script_skeleton.resolve_script_kind` (evidence-based
    resolution, single source of truth, shared with the editing core and the unified write entry's metadata recomputation, no drift);
    the kind -> model mapping (`_KIND_MODEL`) stays local (models are not in the registry).

    The decision is **data shape first** (see the `resolve_script_kind` docstring): it routes to reference only when video_units
    alone exists, otherwise by content_mode -> segments/scenes (a drama with empty scenes still goes to DramaEpisodeScript).
    ``generation_mode`` does not take part -- the caller-side generation paths (server/media_tools/videos.py etc.) branch on
    generation_mode themselves; this function only decides which Pydantic model **structure validation** uses.

    按模式判别该用哪个剧本模型，判别逻辑收归 `script_skeleton.resolve_script_kind`（取证解析，
    单一真相源，与编辑核心、写盘统一入口的 metadata 重算共用，不漂移）；kind → 模型的映射
    (`_KIND_MODEL`) 留本地（模型不进注册表）。

    判别**数据形状优先**(详见 `resolve_script_kind` docstring):video_units 唯一存在时才路由
    reference,否则按 content_mode → segments/scenes(空场景 drama 仍走 DramaEpisodeScript)。
    ``generation_mode`` 不参与判别——caller 端的生成路径(server/media_tools/videos.py 等)自己按
    generation_mode 分流;此函数只决定**结构校验**用哪个 Pydantic 模型。
    """
    return _KIND_MODEL[resolve_script_kind(script)]


def _format_error(err: ErrorDetails) -> str:
    loc = ".".join(str(part) for part in err.get("loc", ()))
    msg = err.get("msg", "")
    return f"{loc}: {msg}" if loc else str(msg)


def validate_script_structure(script: dict[str, Any]) -> ValidationResult:
    """Validate that a script dict is structurally well-formed; return `ValidationResult`.

    Pure function: no disk reads, no file reference checks, no checks of character/scene name consistency across project.json
    (those are the archive-layer job of `DataValidator.validate_project_tree`).

    校验剧本 dict 的结构是否良构，返回 `ValidationResult`。

    纯函数：不读磁盘、不查文件引用、不查跨 project.json 的角色/场景名一致性
    （那些是 `DataValidator.validate_project_tree` 的归档层职责）。
    """
    model = _select_model(script)
    try:
        model.model_validate(script)
    except ValidationError as exc:
        # Pydantic errors are already finished text (no translatable structure); they are merged into the same message carrier via the literal channel.
        # Pydantic 报错已是成品文本（无可翻译结构），经 literal 通道并入同一消息载体。
        return ValidationResult(
            valid=False,
            error_messages=[ValidationMessage.literal(_format_error(e)) for e in exc.errors()],
        )
    return ValidationResult(valid=True)
