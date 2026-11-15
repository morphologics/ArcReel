"""All structured-output models sent to text models as ``response_schema``, for parametrizing the schema content cases.

When adding a structured-output call site, add its model to :func:`response_schema_models` so the schema content constraints cover it.

作为 ``response_schema`` 发给文本模型的全部结构化输出模型，供 schema 内容用例参数化。

新增结构化输出调用点时把它的模型补进 :func:`response_schema_models`，让 schema 内容约束覆盖到它。
"""

import inspect
import json
from typing import get_args

import pytest
from pydantic import BaseModel


def response_schema_models() -> list[type[BaseModel]]:
    """The response_schema actually passed at each call site; dynamic models are built once each for a multi-value and a single-value duration enum.

    各调用点实际传入的 response_schema；动态模型按多值与单值时长枚举各取一份。"""
    from lib.episode.episode_planner import DramaPlanDraft, NarrationPlanDraft
    from lib.project.project_manager import ProjectOverview
    from lib.script.script_models import (
        AdReferenceFlatScript,
        AdVisualScript,
        DramaVisualScript,
        NarrationScriptPlanDraft,
        NarrationVisualEpisodeScript,
        ReferencePromptAuthoringFlatScript,
        build_drama_normalized_script_model,
        build_episode_script_model,
        build_reference_units_script_plan_model,
        with_new_assets_field,
    )

    models: list[type[BaseModel]] = [
        # Episode planning / 分集规划
        NarrationPlanDraft,
        DramaPlanDraft,
        # Shot breakdown (narration) and prompt authoring (reference-to-video) / 分镜拆分（旁白）与提示词编写（参考生视频）
        NarrationScriptPlanDraft,
        ReferencePromptAuthoringFlatScript,
        # Prompt authoring visual layer / 提示词编写视觉层
        NarrationVisualEpisodeScript,
        DramaVisualScript,
        AdVisualScript,
        # Ad script (reference-to-video) / 广告剧本（参考生视频）
        AdReferenceFlatScript,
        # Project overview / 项目概述
        ProjectOverview,
    ]
    for durations in ([4, 6, 8], [8]):
        models += [
            build_drama_normalized_script_model(durations),
            build_reference_units_script_plan_model(durations),
            with_new_assets_field(build_episode_script_model("ad", durations)),
        ]
    return models


def response_schema_params() -> list:
    """pytest param form of :func:`response_schema_models`; dynamic models share a name, so ids carry an index to tell them apart.

    :func:`response_schema_models` 的 pytest 参数形态；动态模型同名，id 带序号区分。"""
    return [pytest.param(model, id=f"{i}-{model.__name__}") for i, model in enumerate(response_schema_models())]


def _model_field_descriptions(model: type[BaseModel]) -> set[str]:
    """All field ``description`` values on the model and its nested models.

    模型及其嵌套模型上全部字段的 ``description``。"""
    seen: set[type[BaseModel]] = set()
    found: set[str] = set()

    def visit(tp: object) -> None:
        if isinstance(tp, type) and issubclass(tp, BaseModel) and tp not in seen:
            seen.add(tp)
            for field in tp.model_fields.values():
                if field.description:
                    found.add(field.description)
                visit(field.annotation)
        for arg in get_args(tp):
            visit(arg)

    visit(model)
    return found


def _class_docstring_texts(model: type[BaseModel]) -> set[str]:
    """Descriptions generated from class docstrings in ``model``'s default schema, i.e. text that must not be in what is sent to the model.

    Takes ``description`` from the top level and each ``$defs`` definition. Excludes ones identical to a field description (e.g.
    ``Composition``'s "构图信息"): in that case the default schema keeps it only on the definition, indistinguishable from the
    field description, so the field descriptions taken from the model class are subtracted.

    ``model`` 的默认 schema 里由类 docstring 生成的说明，即发给模型的内容不应带的文本。

    取顶层与各 ``$defs`` 定义上的 ``description``。与字段说明同文的除外（如 ``Composition`` 的「构图信息」）：
    默认 schema 在这种情况下只把它留在定义上，无法与字段说明区分，故从模型类上取字段说明排除掉。
    """
    raw = model.model_json_schema()
    nodes = [raw, *raw.get("$defs", {}).values()]
    return {node["description"] for node in nodes if "description" in node} - _model_field_descriptions(model)


def leaked_class_docstrings(model: type[BaseModel], wire_json: str) -> list[str]:
    """``model`` class docstrings appearing in ``wire_json`` (the JSON text sent to the endpoint), matched by first line.

    Instructor's tool description takes only the first paragraph of the docstring; matching by first line covers both the whole
    description and the extracted first paragraph.

    ``wire_json``（发往端点的 JSON 文本）里出现的 ``model`` 类 docstring，按首行匹配。

    Instructor 的工具说明只取 docstring 的首段，按首行匹配才能同时覆盖整段说明与摘取的首段。
    """
    first_lines = (inspect.cleandoc(text).splitlines()[0] for text in _class_docstring_texts(model))
    return [line for line in first_lines if json.dumps(line, ensure_ascii=False)[1:-1] in wire_json]
