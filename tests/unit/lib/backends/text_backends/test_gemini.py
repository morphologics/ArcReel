"""GeminiTextBackend tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from google.genai.types import GenerateContentResponseUsageMetadata
from pydantic import BaseModel

from lib.backends.text_backends.base import (
    StructuredOutputExhaustedError,
    TextCapability,
    TextGenerationRequest,
    TextGenerationResult,
)
from lib.backends.text_backends.gemini import GeminiTextBackend
from tests.fakes import bounded_poll_clock
from tests.http_capture import capture_http, request_json
from tests.unit.lib.backends.text_backends.response_schema_support import (
    response_schema_models,
    response_schema_params,
)


@pytest.fixture
def mock_genai():
    with patch("lib.backends.text_backends.gemini.genai") as m:
        yield m


class TestProperties:
    def test_name(self, mock_genai):
        b = GeminiTextBackend(api_key="k")
        assert b.name == "gemini"
        # AI Studio mode: construction hands api_key to genai.Client as is; without base_url, no http_options are passed.
        # AI Studio 模式：构造时把 api_key 原样交给 genai.Client，未给 base_url 则不带 http_options
        mock_genai.Client.assert_called_once_with(api_key="k", http_options=None)

    def test_default_model(self, mock_genai):
        b = GeminiTextBackend(api_key="k")
        assert b.model == "gemini-3-flash-preview"

    def test_custom_model(self, mock_genai):
        b = GeminiTextBackend(api_key="k", model="custom")
        assert b.model == "custom"

    def test_capabilities(self, mock_genai):
        b = GeminiTextBackend(api_key="k")
        assert b.capabilities == {
            TextCapability.TEXT_GENERATION,
            TextCapability.STRUCTURED_OUTPUT,
            TextCapability.VISION,
        }

    def test_no_api_key_raises(self, mock_genai):
        with pytest.raises(ValueError, match="API Key"):
            GeminiTextBackend()


class TestGenerate:
    @pytest.fixture
    def backend(self, mock_genai):
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        b = GeminiTextBackend(api_key="k")
        b._test_client = mock_client
        return b

    async def test_plain_text(self, backend):
        mock_resp = SimpleNamespace(
            text="  generated text  ",
            usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=10, candidates_token_count=5),
        )
        backend._test_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        result = await backend.generate(TextGenerationRequest(prompt="hello"))

        assert isinstance(result, TextGenerationResult)
        assert result.text == "generated text"
        assert result.provider == "gemini"
        assert result.input_tokens == 10
        assert result.output_tokens == 5

    async def test_cache_hits_and_thinking_tokens_are_reported(self, backend):
        """prompt_token_count includes cache hits, and hits are reported separately; thinking tokens count as output.

        prompt_token_count 含缓存命中，命中量另报；思考 token 按输出计入。"""
        mock_resp = SimpleNamespace(
            text="ok",
            usage_metadata=GenerateContentResponseUsageMetadata(
                prompt_token_count=1000,
                cached_content_token_count=700,
                candidates_token_count=50,
                thoughts_token_count=30,
            ),
        )
        backend._test_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        result = await backend.generate(TextGenerationRequest(prompt="hello"))

        assert (result.input_tokens, result.cache_read_tokens, result.output_tokens) == (1000, 700, 80)

    async def test_structured_output_passes_schema(self, backend):
        mock_resp = SimpleNamespace(
            text='{"key": "value"}',
            usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=20, candidates_token_count=10),
        )
        backend._test_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        schema = {"type": "object", "properties": {"key": {"type": "string"}}}
        result = await backend.generate(TextGenerationRequest(prompt="gen json", response_schema=schema))

        assert result.text == '{"key": "value"}'
        call_kwargs = backend._test_client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config")
        assert config["response_mime_type"] == "application/json"
        assert config["response_schema"] == schema
        assert "response_json_schema" not in config

    async def test_structured_truncation_raises(self, backend):
        """Structured output truncated by MAX_TOKENS raises TextOutputTruncatedError (see docs/adr/0044).

        结构化输出被 MAX_TOKENS 截断时抛 TextOutputTruncatedError（见 docs/adr/0044）。"""
        from lib.backends.text_backends.base import TextOutputTruncatedError

        mock_resp = SimpleNamespace(
            text='{"key": "value"}',
            usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=20, candidates_token_count=10),
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
        )
        backend._test_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        schema = {"type": "object", "properties": {"key": {"type": "string"}}}
        with pytest.raises(TextOutputTruncatedError) as exc_info:
            await backend.generate(TextGenerationRequest(prompt="gen", response_schema=schema))

        assert exc_info.value.provider == "gemini"

    async def test_free_text_truncation_only_warns(self, backend, caplog):
        """Truncated free text (no response_schema) warns and marks the result as truncated, without raising.

        自由文本（无 response_schema）被截断时告警并在结果上标记截断，不抛错。"""
        import logging

        mock_resp = SimpleNamespace(
            text="partial",
            usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=20, candidates_token_count=10),
            candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
        )
        backend._test_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        with caplog.at_level(logging.WARNING, logger="lib.backends.text_backends.base"):
            result = await backend.generate(TextGenerationRequest(prompt="gen"))

        assert result.text == "partial"
        assert result.truncated is True
        assert any("被截断" in r.message for r in caplog.records)

    async def test_structured_output_pydantic_class_resolved_to_response_schema(self, backend):
        """A Pydantic class is resolved to a dict and sent via response_schema (wire field responseSchema).

        responseSchema has been around long enough that proxy gateways generally pass it to the real upstream for constrained
        decoding; the newer responseJsonSchema is unknown to most proxy gateways' request structs and is silently dropped, making
        upstream generate freely in schema-less JSON mode. So responseSchema is always used, the same as for dict input.

        传入 Pydantic 类时解析为 dict 走 response_schema（wire 字段 responseSchema）。

        responseSchema 上线已久、代理网关普遍能透传给真实上游执行约束解码；较新的
        responseJsonSchema 多数代理网关的请求解析结构体不认识、会静默丢弃，导致上游
        按无 schema 的纯 JSON 模式自由生成。故统一走 responseSchema，与 dict 入参同口径。
        """
        from pydantic import BaseModel

        class MyModel(BaseModel):
            name: str

        mock_resp = SimpleNamespace(
            text='{"name": "test"}',
            usage_metadata=GenerateContentResponseUsageMetadata(prompt_token_count=20, candidates_token_count=10),
        )
        backend._test_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        await backend.generate(TextGenerationRequest(prompt="gen", response_schema=MyModel))

        call_kwargs = backend._test_client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config")
        assert config["response_mime_type"] == "application/json"
        assert "response_json_schema" not in config
        js = config["response_schema"]
        assert js["type"] == "object"
        assert js["properties"]["name"]["type"] == "string"
        assert js["required"] == ["name"]

    def test_episode_script_integer_enum_stringified(self, backend):
        """Regression: integer duration_seconds enums are converted to string enums on the responseSchema channel.

        types.Schema's enum only supports strings; sending an integer enum as is raises "Input should be a valid string" during
        the real SDK conversion. After converting to a string enum, constrained decoding over the exact set still holds, and the
        parse side's mechanical coercion in _duration_literal restores int.

        回归：duration_seconds 整数 enum 在 responseSchema 通道转为字符串枚举。

        types.Schema 的 enum 仅支持字符串，整数枚举原样发出会在真实 SDK 转换时抛
        "Input should be a valid string"。转为字符串枚举后精确集合的约束解码依然成立，
        解析侧 _duration_literal 的机械强转恢复 int。
        """
        from lib.script.script_models import build_episode_script_model

        config = backend._build_config(build_episode_script_model("narration", [4, 6, 8]), None)

        assert "response_json_schema" not in config
        seg_props = config["response_schema"]["properties"]["segments"]["items"]["properties"]
        assert seg_props["duration_seconds"]["enum"] == ["4", "6", "8"]
        assert seg_props["duration_seconds"]["type"] == "string"

    def test_single_value_duration_const_normalized_to_enum(self, backend):
        """A single-value supported_durations renders as const (types.Schema has no such field), normalized to a single-element
        string enum to keep the hard constraint at the generation layer.

        单值 supported_durations 渲染为 const（types.Schema 无此字段），
        归一为单元素字符串 enum 以保留生成层硬约束。"""
        from lib.script.script_models import build_episode_script_model

        config = backend._build_config(build_episode_script_model("narration", [8]), None)
        ds = config["response_schema"]["properties"]["segments"]["items"]["properties"]["duration_seconds"]
        assert "const" not in ds
        assert ds["enum"] == ["8"]
        assert ds["type"] == "string"

    def test_const_to_enum_distinguishes_keyword_field_name_and_data(self, backend):
        """Distinguishes the three places const appears: schema keyword (normalized), field name (value is still a sub-schema), instance data (untouched).

        In this repo const only comes from single-value duration Literals (scalars). Position awareness ensures: keys of mappings
        like properties are field names whose values are still sub-schemas (real consts inside are normalized as usual); the values
        of keywords like const/default are data and are not recursed into.

        区分 const 出现的三种位置：schema 关键字（归一）、字段名（值仍是子 schema）、实例数据（不动）。

        本仓库 const 只来自单值时长 Literal（标量）。位置感知确保：properties 等映射的 key 是字段名，
        其值仍是子 schema（里面真正的 const 照常归一）；const/default 等关键字的值是数据，不递归。
        """
        schema = {
            "type": "object",
            "properties": {
                "duration_seconds": {"const": 8, "type": "integer"},  # const as a keyword -> normalized / const 作关键字 → 归一
                "const": {"type": "string"},  # Field named const -> untouched (value has no const) / 字段名为 const → 不动（值无 const）
                # Field named default -> its value is a sub-schema, const normalized as usual
                "default": {"const": 6, "type": "integer"},  # 字段名为 default → 其值是子 schema，const 照常归一
                # default as a keyword (data) -> untouched
                "with_default": {"type": "object", "default": {"const": 42}},  # default 作关键字（数据）→ 不动
                "obj_const": {"const": {"const": 5}},  # Non-scalar const -> untouched / 非标量 const → 不动
            },
        }
        props = backend._build_config(schema, None)["response_schema"]["properties"]
        assert props["duration_seconds"] == {"type": "string", "enum": ["8"]}
        assert props["const"] == {"type": "string"}
        assert props["default"] == {"type": "string", "enum": ["6"]}
        assert props["with_default"]["default"] == {"const": 42}
        assert props["obj_const"] == {"const": {"const": 5}}

    def test_all_script_schemas_accepted_by_google_genai_schema(self, backend):
        """Integration regression: every script schema factory's output via _build_config must be accepted by the real types.Schema.

        Mocking out generate_content means the SDK's schema conversion never runs, hiding the incompatibility of integer enum / const
        with types.Schema ("Input should be a valid string" is raised before the request is sent, and whole-episode generation fails
        outright). Here the real SDK type validation is run directly to close that blind spot.

        集成回归：全部剧本 schema 工厂经 _build_config 产出后必须被真实 types.Schema 接受。

        mock 掉 generate_content 会让 SDK 的 schema 转换不执行，掩盖整数 enum / const 与
        types.Schema 的不兼容（"Input should be a valid string" 在请求发出前即抛，整集生成
        直接失败）。这里直接过真实 SDK 类型校验，堵住该盲区。
        """
        from google.genai import types as gtypes

        from lib.script.script_models import (
            AdReferenceFlatScript,
            ReferencePromptAuthoringFlatScript,
            build_drama_normalized_script_model,
            build_episode_script_model,
            build_reference_units_script_plan_model,
        )

        schemas = [
            build_episode_script_model(mode, durations)
            for mode in ("narration", "drama", "ad")
            for durations in ([4, 6, 8], [8])
        ]
        schemas += [
            AdReferenceFlatScript,
            build_drama_normalized_script_model([4, 6, 8]),
            build_drama_normalized_script_model([8]),
            build_reference_units_script_plan_model([4, 6, 8]),
            # Single-value tiers render as const and take a different normalization path from multi-value enums.
            # 单值档位渲染为 const，与多值 enum 走不同归一路径
            build_reference_units_script_plan_model([8]),
            # prompt_authoring's actual response_schema (static, no enum narrowing) goes through this channel too.
            # prompt_authoring 的实际 response_schema（静态，无枚举收窄）同样过这条通道
            ReferencePromptAuthoringFlatScript,
        ]
        assert schemas, "schema 清单为空时下面的循环形同虚设"
        for schema in schemas:
            config = backend._build_config(schema, None)
            # Not raising = the converted dict (string enums, no const) is accepted by google-genai types.Schema.
            # 不抛 = 转换后的 dict（字符串枚举、无 const）被 google-genai types.Schema 接受
            validated = gtypes.Schema.model_validate(config["response_schema"])
            assert validated.type == gtypes.Type.OBJECT

    async def test_system_prompt(self, backend):
        mock_resp = SimpleNamespace(
            text="output",
            usage_metadata=None,
        )
        backend._test_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        result = await backend.generate(TextGenerationRequest(prompt="hello", system_prompt="You are X."))

        assert result.text == "output"
        assert result.input_tokens is None
        call_kwargs = backend._test_client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config")
        assert config["system_instruction"] == "You are X."

    async def test_no_usage_metadata(self, backend):
        mock_resp = SimpleNamespace(text="output", usage_metadata=None)
        backend._test_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        result = await backend.generate(TextGenerationRequest(prompt="hi"))
        assert result.input_tokens is None
        assert result.output_tokens is None

    async def test_max_output_tokens_in_config(self, backend):
        """max_output_tokens is injected into the Gemini config dict.

        max_output_tokens 注入到 Gemini config 字典。"""
        mock_resp = SimpleNamespace(text="x", usage_metadata=None)
        backend._test_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        await backend.generate(TextGenerationRequest(prompt="hi", max_output_tokens=32000))

        call_kwargs = backend._test_client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config")
        assert config["max_output_tokens"] == 32000

    async def test_no_max_output_tokens_means_no_config_key(self, backend):
        """When max_output_tokens is not given, the key should not appear in the config.

        未指定 max_output_tokens 时 config 中不应出现该键。"""
        mock_resp = SimpleNamespace(text="x", usage_metadata=None)
        backend._test_client.aio.models.generate_content = AsyncMock(return_value=mock_resp)

        await backend.generate(TextGenerationRequest(prompt="hi"))

        call_kwargs = backend._test_client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs.get("config")
        assert config is None or "max_output_tokens" not in config


class _OverviewModel(BaseModel):
    genre: str
    theme: str


def _resp(text: str, input_tokens: int | None = None, output_tokens: int | None = None) -> SimpleNamespace:
    usage = (
        GenerateContentResponseUsageMetadata(prompt_token_count=input_tokens, candidates_token_count=output_tokens)
        if input_tokens is not None or output_tokens is not None
        else None
    )
    return SimpleNamespace(text=text, usage_metadata=usage)


_PROSE = "经仔细阅读，该剧本讲述了一个都市逆袭故事。"
_VALID_JSON = '{"genre": "都市", "theme": "逆袭"}'


class TestStructuredFallback:
    """Fallback path when a proxy gateway returns HTTP 200 but does not enforce response_schema (returns prose / violating JSON).

    代理网关 HTTP 200 但不强制 response_schema（返回散文/违例 JSON）时的降级路径。"""

    @pytest.fixture
    def backend(self, mock_genai):
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client
        b = GeminiTextBackend(api_key="k")
        b._test_client = mock_client
        return b

    async def test_prose_triggers_prompt_fallback_and_succeeds(self, backend):
        """A prose response triggers fallback: the schema is injected into the prompt, resent without wire structured params, tokens merged.

        散文响应触发降级：schema 注入 prompt、不带 wire 结构化参数重发，token 并账。"""
        gc = AsyncMock(side_effect=[_resp(_PROSE, 20, 10), _resp(_VALID_JSON, 30, 15)])
        backend._test_client.aio.models.generate_content = gc

        result = await backend.generate(TextGenerationRequest(prompt="分析剧本", response_schema=_OverviewModel))

        assert gc.call_count == 2
        fb_config = gc.call_args_list[1].kwargs.get("config")
        assert fb_config is None or (
            "response_schema" not in fb_config
            and "response_json_schema" not in fb_config
            and "response_mime_type" not in fb_config
        )
        fb_prompt = gc.call_args_list[1].kwargs["contents"][-1]
        assert "分析剧本" in fb_prompt
        assert "JSON Schema" in fb_prompt
        assert '"genre"' in fb_prompt
        # After validation passes, normalized JSON is returned, so the downstream model_validate_json always succeeds.
        # 校验通过后返回规范化 JSON，下游 model_validate_json 必定成功
        assert _OverviewModel.model_validate_json(result.text) == _OverviewModel(genre="都市", theme="逆袭")
        assert result.provider == "gemini"
        assert result.model == backend.model
        assert result.input_tokens == 50
        assert result.output_tokens == 25

    async def test_fallback_strips_code_fences(self, backend):
        """Fallback output wrapped in markdown fences is stripped before validation.

        降级输出带 markdown 栅栏时剥离后校验。"""
        fenced = f"```json\n{_VALID_JSON}\n```"
        gc = AsyncMock(side_effect=[_resp(_PROSE), _resp(fenced)])
        backend._test_client.aio.models.generate_content = gc

        result = await backend.generate(TextGenerationRequest(prompt="p", response_schema=_OverviewModel))

        assert _OverviewModel.model_validate_json(result.text).genre == "都市"
        # When neither the native nor the fallback call has usage, tokens stay None rather than collapsing to 0.
        # 原生与降级两次调用都没有用量时，token 保持 None，不塌成 0
        assert result.input_tokens is None
        assert result.output_tokens is None

    async def test_fallback_retries_with_error_feedback(self, backend):
        """When the first fallback output violates the schema, retry once with error feedback.

        降级首次输出违反 schema 时带错误反馈重试一次。"""
        gc = AsyncMock(
            side_effect=[
                _resp(_PROSE, 10, 5),
                _resp('{"genre": "都市"}', 10, 5),  # Missing required theme. / 缺必填 theme
                _resp(_VALID_JSON, 10, 5),
            ]
        )
        backend._test_client.aio.models.generate_content = gc

        result = await backend.generate(TextGenerationRequest(prompt="p", response_schema=_OverviewModel))

        assert gc.call_count == 3
        retry_prompt = gc.call_args_list[2].kwargs["contents"][-1]
        assert "不符合" in retry_prompt
        assert _OverviewModel.model_validate_json(result.text).theme == "逆袭"
        assert result.input_tokens == 30
        assert result.output_tokens == 15

    async def test_fallback_exhausted_raises_structured_output_exhausted(self, backend):
        """Exhausted fallback retries converge to StructuredOutputExhaustedError with a user-readable message (raw pydantic strings not passed through).

        降级重试穷尽后收敛为 StructuredOutputExhaustedError，消息面向用户可读（不透传 pydantic 原始串）。"""
        gc = AsyncMock(side_effect=[_resp(_PROSE), _resp(_PROSE), _resp(_PROSE)])
        backend._test_client.aio.models.generate_content = gc

        with pytest.raises(StructuredOutputExhaustedError, match="结构化输出能力不足") as exc_info:
            await backend.generate(TextGenerationRequest(prompt="p", response_schema=_OverviewModel))

        assert gc.call_count == 3
        assert exc_info.value.provider == "gemini"
        assert exc_info.value.model == "gemini-3-flash-preview"
        # reason states the number of fallback attempts and the cause of the last validation failure. / reason 写明降级尝试次数与最后一次校验失败的原因
        assert exc_info.value.reason == "prompt 注入降级 2 次尝试后输出仍不合规（1 处字段不符（<root>））"

    async def test_fallback_logs_raw_model_output(self, backend, caplog):
        """The fallback trigger point logs the model's raw output as a warning, for later diagnosis of what the model actually produced.

        降级触发点以 warning 记录模型原始输出，供事后诊断模型实际吐了什么。"""
        gc = AsyncMock(side_effect=[_resp(_PROSE), _resp(_VALID_JSON)])
        backend._test_client.aio.models.generate_content = gc

        with caplog.at_level("WARNING", logger="lib.backends.text_backends.gemini"):
            await backend.generate(TextGenerationRequest(prompt="p", response_schema=_OverviewModel))

        assert any(_PROSE in record.getMessage() for record in caplog.records)

    async def test_valid_schema_json_no_fallback(self, backend):
        """Valid JSON satisfying the schema does not trigger fallback (a single call).

        满足 schema 的合法 JSON 不触发降级（单次调用）。"""
        gc = AsyncMock(return_value=_resp(_VALID_JSON, 20, 10))
        backend._test_client.aio.models.generate_content = gc

        result = await backend.generate(TextGenerationRequest(prompt="p", response_schema=_OverviewModel))

        assert gc.call_count == 1
        assert result.text == _VALID_JSON

    async def test_dict_schema_prose_fallback_returns_stripped_json(self, backend):
        """A dict schema has no Pydantic model to validate against; after fallback it passes on the "valid JSON" standard.

        dict schema 无 Pydantic 模型可校验，降级后按「合法 JSON」标准放行。"""
        schema = {"type": "object", "properties": {"k": {"type": "string"}}}
        gc = AsyncMock(side_effect=[_resp(_PROSE), _resp('```json\n{"k": "v"}\n```')])
        backend._test_client.aio.models.generate_content = gc

        result = await backend.generate(TextGenerationRequest(prompt="p", response_schema=schema))

        assert result.text == '{"k": "v"}'

    async def test_fallback_transient_error_does_not_replay_native_call(self, backend):
        """Transient errors on the fallback path are retried only at the fallback layer; the successful (billed) native call is not replayed.

        降级路径瞬态错误只在降级层重试，不重放已成功（已计费）的原生调用。"""
        transient = ConnectionError("503 service unavailable")
        gc = AsyncMock(side_effect=[_resp(_PROSE), transient, transient, transient])
        backend._test_client.aio.models.generate_content = gc

        with bounded_poll_clock(), pytest.raises(ConnectionError):
            await backend.generate(TextGenerationRequest(prompt="p", response_schema=_OverviewModel))

        # 1 native success + the fallback layer's own 3 retries exhausted; the native call was not replayed.
        # 1 次原生成功 + 降级层自身 3 次重试穷尽；原生调用未被重放
        assert gc.call_count == 4


def _keys_anywhere(node: object, names: set[str]) -> list[str]:
    """Recursively collect the given key wherever it appears in the dict (including property-name positions, for asserting "contains none at all").

    递归收集 dict 中任何层级出现的指定 key（含属性名位置，用于断言「完全不含」）。"""
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in names:
                found.append(k)
            found.extend(_keys_anywhere(v, names))
    elif isinstance(node, list):
        for item in node:
            found.extend(_keys_anywhere(item, names))
    return found


class TestAdditionalPropertiesStripped:
    """The Gemini Developer API's responseSchema does not know additionalProperties; sending it gives 400 INVALID_ARGUMENT.

    Gemini Developer API 的 responseSchema 不认识 additionalProperties，下发即 400 INVALID_ARGUMENT。"""

    @pytest.fixture
    def backend(self, mock_genai):
        mock_genai.Client.return_value = MagicMock()
        return GeminiTextBackend(api_key="k")

    def test_response_schema_models_carry_additional_properties_before_conversion(self):
        """Precondition self-check: the raw schema really carries additionalProperties, otherwise the parametrized assertions below are meaningless.

        前提自检：原始 schema 确实带 additionalProperties，否则下面的参数化断言形同虚设。"""
        assert any(_keys_anywhere(m.model_json_schema(), {"additionalProperties"}) for m in response_schema_models())

    @pytest.mark.parametrize("model", response_schema_params())
    def test_every_response_schema_model_has_no_additional_properties(self, backend, model):
        config = backend._build_config(model, None)
        assert _keys_anywhere(config["response_schema"], {"additionalProperties", "additional_properties"}) == []

    def test_strips_at_every_nesting_level_including_array_items_and_union_members(self, backend):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "string"}}},
                },
                "either": {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]},
            },
        }
        out = backend._build_config(schema, None)["response_schema"]
        assert _keys_anywhere(out, {"additionalProperties"}) == []
        assert out["properties"]["items"]["items"]["properties"] == {"a": {"type": "string"}}
        assert out["properties"]["either"]["anyOf"][0] == {"type": "object"}

    def test_keeps_property_named_additional_properties_and_instance_data(self, backend):
        """Only strip schema keywords: same-named fields and same-named content inside enum/default/const instance data stay as is.

        只剥 schema 关键字：同名字段、enum/default/const 实例数据里的同名内容保持原样。"""
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "additionalProperties": {"type": "string", "description": "字段名恰好叫 additionalProperties"},
                "nested": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"additionalProperties": {"type": "integer"}},
                },
                "with_default": {"type": "object", "default": {"additionalProperties": False}},
                "with_enum": {"enum": ["additionalProperties", "other"]},
                "with_const": {"const": {"additionalProperties": 1}},
                "with_examples": {"type": "object", "examples": [{"additionalProperties": False}]},
            },
            "required": ["additionalProperties"],
        }
        out = backend._build_config(schema, None)["response_schema"]
        props = out["properties"]
        assert "additionalProperties" not in {k for k in out if k != "properties"}
        assert props["additionalProperties"] == {"type": "string", "description": "字段名恰好叫 additionalProperties"}
        assert props["nested"] == {"type": "object", "properties": {"additionalProperties": {"type": "integer"}}}
        assert props["with_default"]["default"] == {"additionalProperties": False}
        assert props["with_enum"]["enum"] == ["additionalProperties", "other"]
        assert props["with_const"]["const"] == {"additionalProperties": 1}
        assert props["with_examples"]["examples"] == [{"additionalProperties": False}]
        assert out["required"] == ["additionalProperties"]

    async def test_request_body_response_schema_has_no_additional_properties(self):
        """At the request-body level: generationConfig.responseSchema as serialized by the real google-genai does not contain the field.

        With aiohttp installed google-genai uses aiohttp by default, which respx cannot intercept; after injecting
        ``httpx_async_client`` the SDK uses that httpx client instead (including later requests in the fallback chain), so respx
        captures it at the transport layer and undeclared requests raise directly.

        请求体层面：真实 google-genai 序列化后的 generationConfig.responseSchema 不含该字段。

        google-genai 装有 aiohttp 时默认走 aiohttp，respx 拦不到；注入 ``httpx_async_client`` 后 SDK
        改走该 httpx 客户端（含降级链的后续请求），由 respx 在 transport 层截获，未声明的请求直接抛错。
        """
        from google import genai as real_genai

        reply = {
            "candidates": [{"content": {"parts": [{"text": "{}"}], "role": "model"}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }
        backend = GeminiTextBackend(api_key="k")
        with capture_http() as router:
            route = router.post(url__regex=r":generateContent$").mock(return_value=httpx.Response(200, json=reply))
            async with httpx.AsyncClient() as http_client:
                backend._client = real_genai.Client(api_key="k", http_options={"httpx_async_client": http_client})
                for model in response_schema_models():
                    route.calls.reset()
                    # The response is just an empty object that fails each model's validation and goes down the fallback chain; this case only looks at the first native structured request.
                    # 回包只是空对象，过不了各模型的校验，会走降级链；本用例只看首个原生结构化请求
                    with pytest.raises(StructuredOutputExhaustedError):
                        await backend.generate(TextGenerationRequest(prompt="x", response_schema=model))
                    assert route.called, model.__name__
                    response_schema = request_json(route.calls[0].request)["generationConfig"]["responseSchema"]
                    assert response_schema["type"] == "OBJECT"
                    assert _keys_anywhere(response_schema, {"additional_properties", "additionalProperties"}) == [], (
                        model.__name__
                    )
