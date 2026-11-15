"""Retired model mapping: models referenced in settings and projects that the provider has renamed or shut down are rewritten to their successors.

Only exact ``provider/model`` references confirmed renamed or shut down and having a successor are included. Models
delisted without a successor are not here: such references are kept as is, and when the backend is built at runtime it
reports "this model is not in the provider's model list", letting the user reselect
(``lib.catalog.errors.ModelNotInCatalogError``).

退役模型映射：设置与项目里引用的、已被供应商改名或停服的模型改写为后继模型。

只收已确认改名或停服、且有后继模型的精确 ``provider/model`` 引用。已下架而没有后继的模型不在这里：
这类引用原样保留，运行时构造 backend 时报「该模型不在供应商的模型清单中」，由用户重新选择
（``lib.catalog.errors.ModelNotInCatalogError``）。
"""

from __future__ import annotations

#: Retired model reference → successor model reference, covering all media types. Other preview models or similar strings are not put in the mapping.
#: 退役模型引用 → 后继模型引用，覆盖所有媒体类型。不把其他 preview 模型或相似字符串纳入映射。
RETIRED_MODEL_REFERENCES: dict[str, str] = {
    # Text / 文本
    "gemini-aistudio/gemini-3.1-flash-lite-preview": "gemini-aistudio/gemini-3.1-flash-lite",
    "gemini-vertex/gemini-3.1-flash-lite-preview": "gemini-vertex/gemini-3.1-flash-lite",
    # Ark Seed 2.0 Lite/Mini version 260215 and xAI Grok 4.1 Fast are shut down; switch to in-service models of the same tier
    # 方舟 Seed 2.0 Lite/Mini 的 260215 版本与 xAI Grok 4.1 Fast 停服，改用同档在役型号
    "ark/doubao-seed-2-0-lite-260215": "ark/doubao-seed-2-1-lite-260915",
    "ark/doubao-seed-2-0-mini-260215": "ark/doubao-seed-2-0-mini-260428",
    "grok/grok-4-1-fast-reasoning": "grok/grok-4.3",
    "grok/grok-4-1-fast-non-reasoning": "grok/grok-4.3",
    # Images: Gemini image models are generally available; AI Studio and Vertex both switch to names without -preview
    # 图片：Gemini 图片模型正式发布，AI Studio 与 Vertex 都改用不带 -preview 的名字
    "gemini-aistudio/gemini-3-pro-image-preview": "gemini-aistudio/gemini-3-pro-image",
    "gemini-aistudio/gemini-3.1-flash-image-preview": "gemini-aistudio/gemini-3.1-flash-image",
    "gemini-vertex/gemini-3-pro-image-preview": "gemini-vertex/gemini-3-pro-image",
    "gemini-vertex/gemini-3.1-flash-image-preview": "gemini-vertex/gemini-3.1-flash-image",
}

#: Text backend fields with the same name in the global system_settings and the project's project.json.
#: 全局 system_settings 与项目 project.json 同名的文本 backend 字段。
TEXT_BACKEND_SETTING_KEYS: tuple[str, ...] = (
    "default_text_backend",
    "text_backend_simple",
    "text_backend_complex",
)

#: All settings in the global system_settings that reference models. / 全局 system_settings 里所有引用模型的设置项。
GLOBAL_MODEL_SETTING_KEYS: tuple[str, ...] = (
    *TEXT_BACKEND_SETTING_KEYS,
    "default_image_backend",
    "default_image_backend_t2i",
    "default_image_backend_i2i",
    "default_video_backend",
    "default_video_backend_i2v",
    "default_video_backend_r2v",
    "default_audio_backend",
)

#: All fields in the project's project.json that reference models. / 项目 project.json 里所有引用模型的字段。
PROJECT_MODEL_SETTING_KEYS: tuple[str, ...] = (
    *TEXT_BACKEND_SETTING_KEYS,
    "default_image_backend",
    "image_provider_t2i",
    "image_provider_i2i",
    "video_backend",
    "video_provider_i2v",
    "video_provider_r2v",
    "audio_backend",
)


def migrate_retired_model_reference(value: str) -> str:
    """Rewrite an exact retired model reference to the successor model reference; other values are returned as is.

    把精确的退役模型引用改写为后继模型引用；其余值原样返回。"""

    return RETIRED_MODEL_REFERENCES.get(value, value)
