""""Fetch models" for built-in providers: call the provider's model list endpoint, take only the model IDs, and match exactly against the API model names in the list (docs/adr/0105).

7 built-in providers have a usable list endpoint: gemini-aistudio and gemini-vertex use the google shape; openai, grok,
dashscope, minimax and agnes use the OpenAI-compatible shape. Other metadata returned by the list endpoint is not read;
the result is also not used to judge whether a model is usable, since that varies by account and region.

内置供应商的「获取模型」：调用供应商的模型列表接口，只取模型 ID，按清单中的 API 模型名精确匹配（docs/adr/0105）。

7 家内置供应商有可用的列表接口：gemini-aistudio、gemini-vertex 用 google 形态，openai、grok、dashscope、minimax、
agnes 用 OpenAI 兼容形态。列表接口返回的其他元信息不读取；获取结果也不用来判断模型是否可用，它会因账号、地域而不同。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from arcreel_catalog_core import CatalogEntry
from lib.catalog import query as catalog
from lib.catalog.model_id import has_dot_segment
from lib.custom_provider.endpoints import infer_media_type

DiscoveryForm = Literal["google", "openai"]

#: Built-in providers that have a model list endpoint, and the endpoint shape. / 有模型列表接口的内置供应商与接口形态。
BUILTIN_DISCOVERY_FORMS: Mapping[str, DiscoveryForm] = {
    "gemini-aistudio": "google",
    "gemini-vertex": "google",
    "openai": "openai",
    "grok": "openai",
    "dashscope": "openai",
    "minimax": "openai",
    "agnes": "openai",
}

#: xAI's OpenAI-compatible endpoint address; grok's backend uses xai_sdk and doesn't store an address in the config.
#: xAI 的 OpenAI 兼容接口地址；grok 的调用通道走 xai_sdk，不在配置里存地址。
XAI_OPENAI_BASE_URL = "https://api.x.ai/v1"


class BuiltinDiscoveryConfigError(ValueError):
    """The credential lacks a field needed to fetch models (e.g. the Vertex credential file doesn't exist or has no project_id). ``code`` is the i18n key of the error message.

    凭证缺少获取模型需要的字段（如 Vertex 凭证文件不存在或没有 project_id）。``code`` 是错误文案的 i18n key。"""

    def __init__(self, code: str, **params: object) -> None:
        super().__init__(code)
        self.code = code
        self.params = params


@dataclass(frozen=True)
class DiscoveredBuiltinModel:
    """One fetched model.

    When ``recognized`` is true it matched an officially listed model in the list, and ``model_key`` is its model key
    (an amphibious model's API model name matches two model keys, one row each); when not matched ``model_key`` is
    ``None``, and ``media_type`` comes first from the entry matched by name, else inferred from the name, and is used as
    the user-added model's media type when added; ``entry`` is the entry matched by name (``None`` if none), which calls
    are priced by after adding.

    获取到的一个模型。

    ``recognized`` 为真时对上了清单里的官方收录模型，``model_key`` 是它的模型键（两栖模型的 API 模型名对上两个
    模型键，各列一行）；对不上时 ``model_key`` 为 ``None``，``media_type`` 先看名字对应上的条目，再按名字推断，
    添加时作为用户添加的模型的媒体类型；``entry`` 是名字对应上的条目（对应不上为 ``None``），添加后调用按它计价。
    """

    model_id: str
    media_type: str
    recognized: bool
    model_key: str | None
    entry: CatalogEntry | None


def is_discoverable(provider_id: str) -> bool:
    return provider_id in BUILTIN_DISCOVERY_FORMS


def match_by_name(provider_id: str, model_id: str) -> tuple[str, CatalogEntry | None]:
    """When no officially listed model matches, the entry and media type matched by name (docs/adr/0105).

    When the name directly maps to an entry, the entry's media type is used; an amphibious model's name doesn't map to a
    single entry, so the media type is first inferred from the name, then looked up once within that media type. This is
    consistent with how a user-added model maps to an entry via ``catalog.find_entry`` after adding, so the entry matched
    here is exactly the one pricing will be based on after adding.

    对不上官方收录模型时，按名字对应的条目与媒体类型（docs/adr/0105）。

    名字直接对应上条目时取条目的媒体类型；两栖模型的名字对应不上单个条目，先按名字推断媒体类型，再在该媒体类型内
    查一次。与添加后用户添加的模型经 ``catalog.find_entry`` 对应条目的口径一致，所以这里对应到的条目就是添加后
    计价所依据的条目。
    """
    entry = catalog.find_entry(model_id)
    if entry is not None:
        return entry.media_type, entry
    media_type = infer_media_type(model_id, BUILTIN_DISCOVERY_FORMS.get(provider_id, "openai"))
    return media_type, catalog.find_entry(model_id, media_type=media_type)


def name_media_type(provider_id: str, model_id: str) -> str:
    """Media type when no officially listed model matches: the entry's if the name maps to one, otherwise inferred from the name (same rules as custom provider model discovery).

    对不上官方收录模型时的媒体类型：名字对应上条目取条目的，否则按名字推断（规则同自定义供应商的模型发现）。"""
    return match_by_name(provider_id, model_id)[0]


def classify_model_ids(provider_id: str, model_ids: list[str]) -> list[DiscoveredBuiltinModel]:
    """Match exactly against the API model names in the list; results sorted by model ID and deduplicated.

    按清单中的 API 模型名精确匹配；结果按模型 ID 排序、去重。"""
    official: dict[str, list[catalog.ProviderModelView]] = {}
    for view in catalog.provider_models(provider_id):
        if view.official:
            official.setdefault(view.api_model_name, []).append(view)
    result: list[DiscoveredBuiltinModel] = []
    # IDs containing dot segments are rejected when added (see model_id.py), so they are not put in the addable list
    # 含点段的 ID 添加会被拒（见 model_id.py），不放进可添加列表
    for model_id in sorted({m for m in model_ids if not has_dot_segment(m)}):
        views = official.get(model_id)
        if views:
            result.extend(
                DiscoveredBuiltinModel(model_id, view.model.media_type, True, view.model_id, view.entry)
                for view in views
            )
        else:
            media_type, entry = match_by_name(provider_id, model_id)
            result.append(DiscoveredBuiltinModel(model_id, media_type, False, None, entry))
    return result


def list_model_ids(provider_id: str, config: Mapping[str, str]) -> list[str]:
    """Call the built-in provider's model list endpoint and return the model IDs; performs network requests synchronously, the caller runs it in a thread.

    ``config`` is the provider config with the credential overlaid (``api_key``, ``base_url``, ``credentials_path``).

    Raises:
        BuiltinDiscoveryConfigError: the credential lacks a field needed to fetch models.
        ValueError: ``provider_id`` has no model list endpoint.

    调用内置供应商的模型列表接口，返回模型 ID；同步执行网络请求，调用方放进线程。

    ``config`` 是叠加了凭证的供应商配置（``api_key``、``base_url``、``credentials_path``）。

    Raises:
        BuiltinDiscoveryConfigError: 凭证缺少获取模型需要的字段。
        ValueError: ``provider_id`` 没有模型列表接口。
    """
    match provider_id:
        case "gemini-aistudio":
            return _list_google_aistudio(config)
        case "gemini-vertex":
            return _list_google_vertex(config)
        case "openai" | "grok" | "dashscope" | "minimax" | "agnes":
            return _list_openai(config["api_key"], _openai_base_url(provider_id, config.get("base_url")))
        case _:
            raise ValueError(f"provider {provider_id!r} has no model list API")


def _openai_base_url(provider_id: str, configured: str | None) -> str:
    """Address of the list endpoint; an explicit address is returned for every provider. With ``None`` the OpenAI SDK reads the ``OPENAI_BASE_URL`` env var instead and would send the key there.

    列表接口的地址；每家都返回显式地址。传 ``None`` 时 OpenAI SDK 改读 ``OPENAI_BASE_URL`` 环境变量，会把密钥发到那里。"""
    match provider_id:
        case "grok":
            return XAI_OPENAI_BASE_URL
        case "dashscope":
            from lib.backends.dashscope_shared import dashscope_text_base_url

            return dashscope_text_base_url(configured)
        case "minimax":
            from lib.backends.minimax_shared import minimax_text_base_url

            return minimax_text_base_url(configured)
        case "agnes":
            from lib.backends.agnes_shared import agnes_base_url

            return agnes_base_url(configured)
        case _:
            from lib.config.url_utils import OFFICIAL_OPENAI_BASE_URL

            return (configured or "").strip() or OFFICIAL_OPENAI_BASE_URL


#: Timeout of a single model list request. The caller has its own overall time limit; here the thread ends with it instead of hanging on the SDK's default long timeout and retries.
#: 模型列表接口单次请求的超时。调用方另有总时限，这里让线程随之结束，不按 SDK 默认的长超时与重试挂着。
_LIST_TIMEOUT_SECONDS = 30
#: The same timeout in the unit of the genai SDK's ``HttpOptions.timeout`` (milliseconds); without ``retry_options`` it doesn't retry.
#: 同一超时按 genai SDK ``HttpOptions.timeout`` 的单位（毫秒）；不设 ``retry_options`` 时它不重试。
_GENAI_LIST_TIMEOUT_MS = _LIST_TIMEOUT_SECONDS * 1000


def _list_openai(api_key: str, base_url: str) -> list[str]:
    from openai import OpenAI

    with OpenAI(api_key=api_key, base_url=base_url, timeout=_LIST_TIMEOUT_SECONDS, max_retries=0) as client:
        return [m.id for m in client.models.list() if m.id]


def _short_name(name: str | None) -> str:
    """Strip path prefixes like ``models/``, ``publishers/google/models/``. / 去掉 ``models/``、``publishers/google/models/`` 这类路径前缀。"""
    return (name or "").rsplit("/", 1)[-1]


def _list_google_aistudio(config: Mapping[str, str]) -> list[str]:
    from google import genai

    from lib.config.url_utils import ensure_google_base_url

    http_options: genai.types.HttpOptionsDict = {"timeout": _GENAI_LIST_TIMEOUT_MS}
    if base_url := config.get("base_url"):
        http_options["base_url"] = ensure_google_base_url(base_url)
    client = genai.Client(api_key=config["api_key"], http_options=http_options)
    return [name for m in client.models.list() if (name := _short_name(m.name))]


def _list_google_vertex(config: Mapping[str, str]) -> list[str]:
    from google import genai
    from google.oauth2 import service_account

    from lib.backends.gemini_shared import VERTEX_SCOPES

    credentials_path = config.get("credentials_path", "")
    if not credentials_path or not Path(credentials_path).is_file():
        raise BuiltinDiscoveryConfigError("file_not_found", path=credentials_path)
    with open(credentials_path, encoding="utf-8") as f:
        project_id = json.load(f).get("project_id")
    if not project_id:
        raise BuiltinDiscoveryConfigError("vertex_json_missing_project_id")
    credentials = service_account.Credentials.from_service_account_file(credentials_path, scopes=VERTEX_SCOPES)
    client = genai.Client(
        vertexai=True,
        project=project_id,
        location="global",
        credentials=credentials,
        http_options={"timeout": _GENAI_LIST_TIMEOUT_MS},
    )
    return [name for m in client.models.list() if (name := _short_name(m.name))]
