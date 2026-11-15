"""instructor_support module tests.

instructor_support 模块测试。"""

import json
from collections.abc import Generator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import instructor
import pytest
from instructor import Mode
from instructor.core import IncompleteOutputException, InstructorRetryException, ResponseParsingError
from openai import BadRequestError
from pydantic import BaseModel, ValidationError

from lib.backends.text_backends.base import (
    StructuredOutputExhaustedError,
    TextGenerationResult,
    TextOutputTruncatedError,
)
from lib.backends.text_backends.instructor_support import (
    _PARSE_FAILURE_TYPES,
    generate_structured_via_instructor,
    generate_structured_via_instructor_async,
    instructor_fallback_async,
    instructor_fallback_sync,
)
from tests.fakes import instructor_api_call_exhausted
from tests.http_capture import capture_http, only_request, request_json
from tests.unit.lib.backends.text_backends.response_schema_support import (
    leaked_class_docstrings,
    response_schema_params,
)


class SampleModel(BaseModel):
    name: str
    age: int


def _structured(json_text: str, input_tokens: int, output_tokens: int) -> TextGenerationResult:
    """Result when one Instructor tier call succeeds: the JSON text and that call's metering.

    一档 Instructor 调用成功时的结果：JSON 文本与这次调用的计量。"""
    return TextGenerationResult(
        text=json_text, provider="", model="", input_tokens=input_tokens, output_tokens=output_tokens
    )


def _completion(content: str, *, tool_calls=None, function_call=None) -> SimpleNamespace:
    """Build a completion, for the diagnostic-log assertions to take the raw output from and for the predicate to check for a tool call.

    构造一个 completion，供诊断日志断言取原始输出、供判据看有无 tool call。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls, function_call=function_call)
            )
        ]
    )


def _retry_exhausted(
    inner: Exception,
    *,
    content: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    earlier_attempts: list[Exception] | None = None,
) -> InstructorRetryException:
    """Build an Instructor in-tier retries-exhausted exception, with the terminating cause inner on __cause__ (as in the real shape).

    prompt_tokens / completion_tokens simulate the in-tier attempts that got HTTP 200 and were billed.
    earlier_attempts simulates the parse / validation failures accumulated before termination -- Instructor does not clear
    failed_attempts, so it being non-empty does not mean this tier failed on model output.

    构造 Instructor 档内重试耗尽异常，终止原因 inner 挂在 __cause__ 上（与真实形态一致）。

    prompt_tokens / completion_tokens 模拟档内那些拿到 HTTP 200、已被计费的尝试。
    earlier_attempts 模拟终止前已累积的解析 / 校验失败——Instructor 不清空 failed_attempts，
    所以它非空并不代表这一档是折在模型输出上。
    """
    from instructor.core.exceptions import FailedAttempt

    recorded = [*(earlier_attempts or []), inner]
    exc = InstructorRetryException(
        "retries exhausted",
        last_completion=_completion(content),
        n_attempts=3,
        total_usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        failed_attempts=[
            FailedAttempt(attempt_number=i, exception=e)
            for i, e in enumerate(recorded, 1)
            if isinstance(e, _PARSE_FAILURE_TYPES)
        ],
    )
    exc.__cause__ = inner
    return exc


def _validation_error() -> ValidationError:
    try:
        SampleModel.model_validate({"name": "Alice"})
    except ValidationError as exc:
        return exc
    raise AssertionError("SampleModel 缺 age 字段应当校验失败")


def _no_tool_call_error() -> ResponseParsingError:
    """Upstream returned no tool call: wire-level incompatibility.

    上游没回 tool call：wire 层不兼容。"""
    return ResponseParsingError(
        "No tool calls or function call found in response",
        mode="TOOLS",
        raw_response=_completion(""),
    )


def _md_json_parse_error() -> ResponseParsingError:
    """MD_JSON tier parse failure: that tier's responses never have a tool call, so the response shape is indistinguishable from the TOOLS tier missing a tool call.

    MD_JSON 档解析失败：该档响应本来就没有 tool call，响应结构与 TOOLS 档缺 tool call 无法区分。"""
    return ResponseParsingError(
        "Failed to extract JSON from response",
        mode="MD_JSON",
        raw_response=_completion("not json"),
    )


def _function_call_args_missing_error() -> ResponseParsingError:
    """Upstream returned a call via legacy function_call but arguments are missing: tool_calls is still None; this is the validation kind.

    上游走 legacy function_call 回了调用但 arguments 缺失：tool_calls 仍为 None，属校验类。"""
    function_call = SimpleNamespace(name="SampleModel", arguments=None)
    return ResponseParsingError(
        "Tool call arguments missing in response",
        mode="TOOLS",
        raw_response=_completion("", function_call=function_call),
    )


def _tool_call_args_invalid_error() -> ResponseParsingError:
    """Upstream returned a tool call but the arguments are unusable: the validation kind, not a wire-level incompatibility.

    上游回了 tool call 但 arguments 不可用：属校验类，不是 wire 层不兼容。"""
    tool_call = SimpleNamespace(function=SimpleNamespace(arguments=None))
    return ResponseParsingError(
        "Tool call arguments missing in response",
        mode="TOOLS",
        raw_response=_completion("", tool_calls=[tool_call]),
    )


def _bad_request(message: str) -> BadRequestError:
    return BadRequestError(
        message=message,
        response=httpx.Response(400, request=httpx.Request("POST", "https://proxy.example/v1/chat/completions")),
        body={"error": {"message": message}},
    )


def _tools_rejected_error() -> InstructorRetryException:
    """Upstream rejects the tools param: Instructor wraps this API call exception before it reaches the fallback chain.

    上游拒收 tools 参数：Instructor 把这次 API 调用异常包起来后才到达降级链。"""
    return instructor_api_call_exhausted(_bad_request("tools is not supported by this endpoint"))


@contextmanager
def _recorded_instructor(
    result: tuple[Any, Any], *, is_async: bool = False
) -> Generator[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """Recorder for the instructor entry points: records the args of from_openai and create_with_completion.

    Contracts such as mode selection and wire param names are about "what the call finally sent to the endpoint looks like", so
    assertions target the recorded args, not the stand-in's call objects.

    instructor 入口的记录器：记下 from_openai 与 create_with_completion 的参数。

    mode 选择、导线参数名这些契约都是「最终发往端点的调用长什么样」，断言落在记录的参数上，
    而不是替身的调用对象。
    """
    patched_with: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    class _Completions:
        def create_with_completion(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            if is_async:

                async def _await_result() -> tuple[Any, Any]:
                    return result

                return _await_result()
            return result

    patched = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()), on=lambda hook_name, handler: None)

    def _from_openai(client: Any, **kwargs: Any) -> Any:
        patched_with.append({"client": client, **kwargs})
        return patched

    with patch("lib.backends.text_backends.instructor_support.instructor.from_openai", _from_openai):
        yield patched_with, calls


class TestGenerateStructuredViaInstructor:
    def test_returns_json_and_tokens(self):
        """Returns the JSON text and token stats correctly.

        正确返回 JSON 文本和 token 统计。"""
        mock_client = MagicMock()
        sample = SampleModel(name="Alice", age=30)
        mock_completion = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=20),
        )

        with patch("lib.backends.text_backends.instructor_support.instructor") as mock_instructor:
            mock_patched = MagicMock()
            mock_instructor.from_openai.return_value = mock_patched
            mock_patched.chat.completions.create_with_completion.return_value = (
                sample,
                mock_completion,
            )

            result = generate_structured_via_instructor(
                client=mock_client,
                model="doubao-seed-2-0-lite-260215",
                messages=[{"role": "user", "content": "test"}],
                response_model=SampleModel,
            )

        assert result.text == sample.model_dump_json()
        assert result.input_tokens == 50
        assert result.output_tokens == 20

    def test_passes_mode_and_retries(self):
        """mode is handed to from_openai; the other params go out as is.

        mode 交给 from_openai，其余参数原样上线。"""
        mock_client = MagicMock()
        sample = SampleModel(name="Bob", age=25)
        mock_completion = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

        with _recorded_instructor((sample, mock_completion)) as (patched_with, calls):
            generate_structured_via_instructor(
                client=mock_client,
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_model=SampleModel,
                mode=Mode.MD_JSON,
                max_retries=3,
            )

        assert patched_with == [{"client": mock_client, "mode": Mode.MD_JSON}]
        # response_model is replaced by a subclass without the class docstring (see TestModelFacingSchemaOnWire); fields and validation unchanged.
        # response_model 换成不带类 docstring 的子类（见 TestModelFacingSchemaOnWire），字段与校验不变
        assert issubclass(calls[0].pop("response_model"), SampleModel)
        assert calls == [
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "test"}],
                "max_retries": 3,
            }
        ]

    def test_handles_none_usage(self):
        """When completion.usage is None, token stats are None.

        completion.usage 为 None 时返回 None token 统计。"""
        mock_client = MagicMock()
        sample = SampleModel(name="Charlie", age=35)
        mock_completion = SimpleNamespace(usage=None)

        with patch("lib.backends.text_backends.instructor_support.instructor") as mock_instructor:
            mock_patched = MagicMock()
            mock_instructor.from_openai.return_value = mock_patched
            mock_patched.chat.completions.create_with_completion.return_value = (
                sample,
                mock_completion,
            )

            result = generate_structured_via_instructor(
                client=mock_client,
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_model=SampleModel,
            )

        assert result.text == sample.model_dump_json()
        assert result.input_tokens is None
        assert result.output_tokens is None

    def test_max_tokens_uses_default_param_name(self):
        """With the default token_param, the max_tokens value goes out under the param name max_tokens.

        默认 token_param 下 max_tokens 值以 max_tokens 为参数名上线。"""
        sample = SampleModel(name="Dave", age=40)
        mock_completion = SimpleNamespace(usage=None)

        with patch("lib.backends.text_backends.instructor_support.instructor") as mock_instructor:
            mock_patched = MagicMock()
            mock_instructor.from_openai.return_value = mock_patched
            mock_patched.chat.completions.create_with_completion.return_value = (sample, mock_completion)

            generate_structured_via_instructor(
                client=MagicMock(),
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_model=SampleModel,
                max_tokens=1234,
            )

            call_kwargs = mock_patched.chat.completions.create_with_completion.call_args[1]
            assert call_kwargs["max_tokens"] == 1234
            assert "max_completion_tokens" not in call_kwargs

    def test_explicit_token_param_max_completion_tokens(self):
        """With an explicit token_param, it goes out under the param name max_completion_tokens.

        显式 token_param 时以 max_completion_tokens 为参数名上线。"""
        sample = SampleModel(name="Eve", age=45)
        mock_completion = SimpleNamespace(usage=None)

        with patch("lib.backends.text_backends.instructor_support.instructor") as mock_instructor:
            mock_patched = MagicMock()
            mock_instructor.from_openai.return_value = mock_patched
            mock_patched.chat.completions.create_with_completion.return_value = (sample, mock_completion)

            generate_structured_via_instructor(
                client=MagicMock(),
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_model=SampleModel,
                max_tokens=1234,
                token_param="max_completion_tokens",
            )

            call_kwargs = mock_patched.chat.completions.create_with_completion.call_args[1]
            assert call_kwargs["max_completion_tokens"] == 1234
            assert "max_tokens" not in call_kwargs

    def test_incomplete_output_maps_to_truncated_error(self):
        """Instructor's IncompleteOutputException (max_tokens truncation) is normalized to TextOutputTruncatedError.

        Instructor 的 IncompleteOutputException（max_tokens 截断）归一为 TextOutputTruncatedError。"""
        with patch("lib.backends.text_backends.instructor_support.instructor") as mock_instructor:
            mock_patched = MagicMock()
            mock_instructor.from_openai.return_value = mock_patched
            mock_patched.chat.completions.create_with_completion.side_effect = IncompleteOutputException()

            with pytest.raises(TextOutputTruncatedError) as exc_info:
                generate_structured_via_instructor(
                    client=MagicMock(),
                    model="test-model",
                    messages=[{"role": "user", "content": "test"}],
                    response_model=SampleModel,
                    provider="test-provider",
                )

        assert exc_info.value.provider == "test-provider"
        assert exc_info.value.model == "test-model"
        assert isinstance(exc_info.value.__cause__, IncompleteOutputException)


class TestGenerateStructuredViaInstructorAsync:
    async def test_explicit_token_param_max_completion_tokens(self):
        """Async version: with an explicit token_param, it goes out under the param name max_completion_tokens.

        异步版显式 token_param 时以 max_completion_tokens 为参数名上线。"""
        sample = SampleModel(name="Frank", age=50)
        mock_completion = SimpleNamespace(usage=None)

        with patch("lib.backends.text_backends.instructor_support.instructor") as mock_instructor:
            mock_patched = MagicMock()
            mock_instructor.from_openai.return_value = mock_patched
            mock_patched.chat.completions.create_with_completion = AsyncMock(return_value=(sample, mock_completion))

            result = await generate_structured_via_instructor_async(
                client=AsyncMock(),
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_model=SampleModel,
                max_tokens=2345,
                token_param="max_completion_tokens",
            )

            call_kwargs = mock_patched.chat.completions.create_with_completion.call_args[1]
            assert call_kwargs["max_completion_tokens"] == 2345
            assert "max_tokens" not in call_kwargs
        assert result.input_tokens is None
        assert result.output_tokens is None

    async def test_incomplete_output_maps_to_truncated_error(self):
        """Async version: IncompleteOutputException is likewise normalized to TextOutputTruncatedError.

        异步版 IncompleteOutputException 同样归一为 TextOutputTruncatedError。"""
        with patch("lib.backends.text_backends.instructor_support.instructor") as mock_instructor:
            mock_patched = MagicMock()
            mock_instructor.from_openai.return_value = mock_patched
            mock_patched.chat.completions.create_with_completion = AsyncMock(side_effect=IncompleteOutputException())

            with pytest.raises(TextOutputTruncatedError) as exc_info:
                await generate_structured_via_instructor_async(
                    client=AsyncMock(),
                    model="async-model",
                    messages=[{"role": "user", "content": "test"}],
                    response_model=SampleModel,
                    provider="async-provider",
                )

        assert exc_info.value.provider == "async-provider"
        assert exc_info.value.model == "async-model"


class TestInstructorFallbackSync:
    """Tests for the high-level function instructor_fallback_sync.

    instructor_fallback_sync 高层函数测试。"""

    def test_pydantic_schema_uses_instructor(self):
        """A Pydantic schema takes the instructor path and returns the correct TextGenerationResult.

        Pydantic schema 走 instructor 路径，返回正确的 TextGenerationResult。"""
        sample = SampleModel(name="Alice", age=30)

        with patch(
            "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
            return_value=_structured(sample.model_dump_json(), 50, 20),
        ):
            result = instructor_fallback_sync(
                client=MagicMock(),
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_schema=SampleModel,
                provider="test-provider",
            )

        assert result.text == sample.model_dump_json()
        assert result.provider == "test-provider"
        assert result.model == "test-model"
        assert result.input_tokens == 50
        assert result.output_tokens == 20

    def test_dict_schema_uses_json_object(self):
        """A dict schema takes the json_object path.

        dict schema 走 json_object 路径。"""
        mock_client = MagicMock()
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"key": "value"}'))],
            usage=SimpleNamespace(prompt_tokens=30, completion_tokens=15),
        )
        mock_client.chat.completions.create.return_value = mock_response

        result = instructor_fallback_sync(
            client=mock_client,
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema={"type": "object"},
            provider="test-provider",
        )

        assert result.text == '{"key": "value"}'
        assert result.provider == "test-provider"
        assert result.input_tokens == 30
        assert result.output_tokens == 15
        assert result.model == "test-model"
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["response_format"] == {"type": "json_object"}
        assert call_kwargs["model"] == "test-model"
        assert call_kwargs["messages"] == [
            {"role": "system", "content": "Respond in JSON format."},
            {"role": "user", "content": "test"},
        ]

    def test_pydantic_branch_forwards_token_param(self):
        """The Pydantic branch's token_param reaches the wire: the value goes out under the param name max_completion_tokens.

        Pydantic 分支的 token_param 一路传到导线：值以 max_completion_tokens 为参数名上线。"""
        sample = SampleModel(name="Alice", age=30)
        completion = SimpleNamespace(usage=None)

        client = MagicMock()
        with _recorded_instructor((sample, completion)) as (patched_with, calls):
            instructor_fallback_sync(
                client=client,
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_schema=SampleModel,
                provider="test-provider",
                max_tokens=500,
                token_param="max_completion_tokens",
            )

        assert issubclass(calls[0].pop("response_model"), SampleModel)
        assert calls == [
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "test"}],
                "max_retries": 2,
                "max_completion_tokens": 500,
            }
        ]
        assert patched_with == [{"client": client, "mode": Mode.TOOLS}]

    def test_dict_branch_default_token_param(self):
        """The dict branch goes out under the param name max_tokens by default.

        dict 分支默认以 max_tokens 为参数名上线。"""
        mock_client = MagicMock()
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"key": "value"}'))],
            usage=None,
        )
        mock_client.chat.completions.create.return_value = mock_response

        instructor_fallback_sync(
            client=mock_client,
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema={"type": "object"},
            provider="test-provider",
            max_tokens=500,
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["max_tokens"] == 500
        assert "max_completion_tokens" not in call_kwargs

    def test_dict_branch_explicit_token_param(self):
        """The dict branch with an explicit token_param goes out under the param name max_completion_tokens.

        dict 分支显式 token_param 时以 max_completion_tokens 为参数名上线。"""
        mock_client = MagicMock()
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"key": "value"}'))],
            usage=None,
        )
        mock_client.chat.completions.create.return_value = mock_response

        instructor_fallback_sync(
            client=mock_client,
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema={"type": "object"},
            provider="test-provider",
            max_tokens=500,
            token_param="max_completion_tokens",
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["max_completion_tokens"] == 500
        assert "max_tokens" not in call_kwargs

    def test_dict_schema_strips_leading_think_block(self):
        """The json_object path matches the native path: a thinking block at the start of content does not enter the result.

        json_object 路径与原生路径同口径：content 开头的思考块不进结果。"""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='<think>想一想。</think>\n{"key": "value"}'))],
            usage=None,
        )

        result = instructor_fallback_sync(
            client=mock_client,
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema={"type": "object"},
            provider="test-provider",
        )

        assert result.text == '{"key": "value"}'

    def test_dict_schema_truncation_raises(self):
        """Truncation with a dict schema (non-empty response_schema, no Pydantic model) is likewise escalated to a hard error.

        dict schema（response_schema 非空，无 Pydantic 模型）截断同样升级为硬错误。"""
        mock_client = MagicMock()
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="partial"), finish_reason="length"),
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=999),
        )
        mock_client.chat.completions.create.return_value = mock_response

        with pytest.raises(TextOutputTruncatedError) as exc_info:
            instructor_fallback_sync(
                client=mock_client,
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_schema={"type": "object"},
                provider="test-provider",
            )

        assert exc_info.value.provider == "test-provider"
        assert exc_info.value.model == "test-model"
        assert exc_info.value.output_tokens == 999


class TestInstructorExceptionShape:
    """Pins the Instructor exception shapes the fallback chain's predicate depends on.

    The predicate tells "API call rejected" apart from "model output non-compliant" by relying on Instructor putting the
    terminating cause on ``__cause__`` and separating parse / validation errors from other exceptions by type. These are
    Instructor implementation details that may change on a dependency upgrade -- this group checks them against the real
    Instructor and goes red as soon as the shape changes, so the fallback chain cannot silently degrade into "every failure is
    final" or "transient errors also drop a tier".

    钉住降级链判据所依赖的 Instructor 异常形态。

    判据要区分「API 调用被拒」与「模型输出不合规」，靠的是 Instructor 把终止原因挂在
    ``__cause__`` 上、并按类型区分解析 / 校验类与其余异常。这些都是 Instructor 的实现细节，
    升级依赖时可能变——本组用例对真实 Instructor 校验它们，形态一变即红，避免降级链在无人
    察觉的情况下退化成「任何失败都判终局」或「瞬态错误也降档」。
    """

    def test_api_call_failure_arrives_wrapped(self):
        from openai import OpenAI

        client = OpenAI(api_key="sk-test", base_url="https://proxy.invalid/v1")
        rejection = _bad_request("tools is not supported by this endpoint")
        client.chat.completions.create = MagicMock(side_effect=rejection)
        patched = instructor.from_openai(client, mode=Mode.TOOLS)

        with pytest.raises(InstructorRetryException) as exc_info:
            patched.chat.completions.create_with_completion(
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_model=SampleModel,
                max_retries=2,
            )

        assert exc_info.value.failed_attempts == []
        assert exc_info.value.__cause__ is rejection

    def test_parse_failure_types_match_instructors_retryable_set(self):
        """The predicate separates model problems from API problems by "is the terminating cause a parse / validation error", so the set must match Instructor's.

        `_RETRYABLE_PARSE_ERRORS` is a private symbol, not a public contract, and pyproject allows instructor>=1.16.0, so it may be
        renamed or moved after an upgrade. An import failure here means exactly that: find again, in instructor's retry loop, the
        exception types that "are recorded into failed_attempts and trigger reask", and update `_PARSE_FAILURE_TYPES` to match.

        判据靠「终止原因是否属解析 / 校验类」区分模型问题与 API 问题，集合须与 Instructor 一致。

        `_RETRYABLE_PARSE_ERRORS` 是私有符号、不属公开契约，而 pyproject 允许 instructor>=1.16.0，
        升级后它可能改名或搬家。此处导入失败即是该情况：到 instructor 的重试循环里重新找出这批
        「会被记进 failed_attempts 并触发 reask」的异常类型，同步更新 `_PARSE_FAILURE_TYPES`。
        """
        from instructor.v2.core.retry import _RETRYABLE_PARSE_ERRORS

        assert set(_PARSE_FAILURE_TYPES) == set(_RETRYABLE_PARSE_ERRORS)

    def test_failed_attempts_survive_a_later_api_failure(self):
        """A parse failure once, then an API-level error: failed_attempts keeps the old record, and the terminating cause is only in __cause__.

        If the predicate used "failed_attempts is non-empty" as a proxy for "failed on model output", this path would be misjudged.

        解析失败一次后再撞上 API 层错误：failed_attempts 保留旧记录，终止原因只在 __cause__。

        判据若拿 failed_attempts 是否为空当「折在模型输出上」的代理，这条路径会被误判。
        """
        from openai import OpenAI
        from openai.types.chat import ChatCompletionMessage

        bad_json = ChatCompletionMessage(role="assistant", content="不是 JSON")
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=bad_json, finish_reason="stop")],
            usage=None,
        )
        outage = ConnectionError("connection reset by peer")
        client = OpenAI(api_key="sk-test", base_url="https://proxy.invalid/v1")
        client.chat.completions.create = MagicMock(side_effect=[completion, outage])
        patched = instructor.from_openai(client, mode=Mode.MD_JSON)

        with pytest.raises(InstructorRetryException) as exc_info:
            patched.chat.completions.create_with_completion(
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_model=SampleModel,
                max_retries=2,
            )

        assert exc_info.value.failed_attempts != []
        assert exc_info.value.__cause__ is outage

    def test_parse_failure_is_recorded_in_failed_attempts(self):
        """Control group: when model output is non-compliant, the failure is recorded in failed_attempts rather than put on __cause__.

        对照组：模型输出不合规时失败记进 failed_attempts，而非挂在 __cause__ 上。"""
        from openai import OpenAI

        message = SimpleNamespace(content="不是 JSON", tool_calls=None, refusal=None)
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=None,
        )
        client = OpenAI(api_key="sk-test", base_url="https://proxy.invalid/v1")
        client.chat.completions.create = MagicMock(return_value=completion)
        patched = instructor.from_openai(client, mode=Mode.TOOLS)

        with pytest.raises(InstructorRetryException) as exc_info:
            patched.chat.completions.create_with_completion(
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_model=SampleModel,
                max_retries=1,
            )

        assert exc_info.value.failed_attempts != []

    def test_absent_tool_call_is_retried_then_terminates_in_parse_error(self):
        """Upstream returns no tool call in the TOOLS tier: Instructor treats it as a retryable parse failure and reasks with a corrective prompt.

        Pins the shape the predicate depends on: termination only after in-tier retries are exhausted, the terminating cause is
        directly the tool_calls=None parse exception, and each resend appends a corrective message (rather than resending the same request).

        TOOLS 档下上游不回 tool call：Instructor 当作可重试的解析失败，reask 带纠正提示重发。

        钉住判据依赖的形态：档内重试耗尽才终止、终止原因直接是 tool_calls=None 的解析异常，
        且每次重发都追加了纠正消息（而非原样重发同一份请求）。
        """
        from openai import OpenAI
        from openai.types.chat import ChatCompletionMessage

        content_only = ChatCompletionMessage(role="assistant", content='<think>…</think>{"name": "Bob", "age": 1}')
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=content_only, finish_reason="stop")],
            usage=None,
        )
        client = OpenAI(api_key="sk-test", base_url="https://proxy.invalid/v1")
        client.chat.completions.create = MagicMock(return_value=completion)
        patched = instructor.from_openai(client, mode=Mode.TOOLS)

        with pytest.raises(InstructorRetryException) as exc_info:
            patched.chat.completions.create_with_completion(
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                response_model=SampleModel,
                max_retries=2,
            )

        cause = exc_info.value.__cause__
        assert isinstance(cause, ResponseParsingError)
        assert cause.raw_response.choices[0].message.tool_calls is None
        calls = client.chat.completions.create.call_args_list
        assert len(calls) == 3
        # Instructor appends to the message list in place; every call records the same object, so only the final shape can be checked.
        # Instructor 原地追加消息列表，各次调用记录的是同一个对象，只能看最终形态
        assert [m["role"] for m in calls[-1].kwargs["messages"][:3]] == ["user", "assistant", "user"]


class TestStructuredModeChainSync:
    """TOOLS -> MD_JSON fallback chain (sync version).

    TOOLS → MD_JSON 降级链（同步版）。"""

    @staticmethod
    def _call():
        return instructor_fallback_sync(
            client=MagicMock(),
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema=SampleModel,
            provider="test-provider",
        )

    @staticmethod
    def _modes(mock_gen) -> list[Mode]:
        return [call.kwargs["mode"] for call in mock_gen.call_args_list]

    def test_chain_starts_with_tools_mode(self):
        """The first tier is TOOLS: return on success without touching the weaker-constrained MD_JSON.

        首档是 TOOLS：成功即返回，不触碰约束更弱的 MD_JSON。"""
        sample = SampleModel(name="Alice", age=30)
        with patch(
            "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
            return_value=_structured(sample.model_dump_json(), 50, 20),
        ) as mock_gen:
            result = self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS]
        assert [call.kwargs["provider"] for call in mock_gen.call_args_list] == ["test-provider"]
        assert result.text == sample.model_dump_json()

    def test_tools_param_rejected_falls_back_to_md_json(self):
        """Upstream rejects the tools param (wire level) -> drop to MD_JSON and produce a compliant result.

        上游拒收 tools 参数（wire 层）→ 降档到 MD_JSON 并产出合规结果。"""
        sample = SampleModel(name="Bob", age=25)
        with patch(
            "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
            side_effect=[_tools_rejected_error(), _structured(sample.model_dump_json(), 10, 5)],
        ) as mock_gen:
            result = self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS, Mode.MD_JSON]
        assert result.text == sample.model_dump_json()

    def test_no_tool_call_in_response_falls_back_to_md_json(self):
        """Upstream accepts tools but returns no tool call (wire level) -> drop to MD_JSON.

        上游收下 tools 却不回 tool call（wire 层）→ 降档到 MD_JSON。"""
        sample = SampleModel(name="Carol", age=28)
        with patch(
            "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
            side_effect=[_retry_exhausted(_no_tool_call_error()), _structured(sample.model_dump_json(), 10, 5)],
        ) as mock_gen:
            result = self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS, Mode.MD_JSON]
        assert result.text == sample.model_dump_json()

    def test_client_type_error_after_tools_parse_failure_propagates(self):
        """TOOLS tier misses a tool call once, then hits a client TypeError: the terminating cause is a client error, which bubbles up as is instead of dropping a tier.

        TOOLS 档缺 tool call 一次后撞上客户端 TypeError：终止原因是客户端错误，原样冒泡而非降档。"""
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[
                    _retry_exhausted(
                        TypeError("unexpected keyword argument"),
                        earlier_attempts=[_no_tool_call_error()],
                    )
                ],
            ) as mock_gen,
            pytest.raises(TypeError),
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS]

    def test_client_type_error_after_md_json_parse_failure_propagates(self):
        """MD_JSON tier fails to parse once, then hits a client TypeError: that tier's reask does not crash; TypeError bubbles up as is rather than being judged final.

        MD_JSON 档解析失败一次后撞上客户端 TypeError：该档 reask 不会崩，TypeError 原样冒泡而非判终局。"""
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[
                    _tools_rejected_error(),
                    _retry_exhausted(
                        TypeError("unexpected keyword argument"),
                        earlier_attempts=[_md_json_parse_error()],
                    ),
                ],
            ) as mock_gen,
            pytest.raises(TypeError),
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS, Mode.MD_JSON]

    def test_function_call_with_missing_args_is_terminal(self):
        """legacy function_call returned a call but arguments are missing: upstream did return a call, so it is final, no tier drop.

        legacy function_call 回了调用但 arguments 缺失：上游确实回了调用，判终局不降档。"""
        sample = SampleModel(name="Eve", age=29)
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[
                    _retry_exhausted(_function_call_args_missing_error()),
                    _structured(sample.model_dump_json(), 10, 5),
                ],
            ) as mock_gen,
            pytest.raises(StructuredOutputExhaustedError) as exc_info,
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS]
        assert "模型输出仍不合规" in str(exc_info.value)
        assert "ResponseParsingError" in str(exc_info.value)

    def test_tools_validation_exhaustion_is_terminal(self):
        """TOOLS tier exhausted on validation errors does not drop a tier: upstream did return a tool call, and a weaker tier would only be worse.

        TOOLS 档校验类耗尽不降档：上游确实回了 tool call，换更弱的档只会更差。"""
        exhausted = _retry_exhausted(_validation_error(), content='{"name": "Alice"}')
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[exhausted],
            ) as mock_gen,
            pytest.raises(StructuredOutputExhaustedError) as exc_info,
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS]
        assert exc_info.value.__cause__ is exhausted
        assert exc_info.value.provider == "test-provider"
        assert exc_info.value.model == "test-model"

    def test_md_json_exhaustion_raises_structured_output_exhausted(self):
        """Exhausting the last tier likewise converges to the final exception, without exposing the raw InstructorRetryException text.

        末档耗尽同样收敛为终局异常，不把 InstructorRetryException 原文透出去。"""
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[_tools_rejected_error(), _retry_exhausted(_validation_error())],
            ) as mock_gen,
            pytest.raises(StructuredOutputExhaustedError, match="结构化输出能力不足") as exc_info,
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS, Mode.MD_JSON]
        assert "最后一次 ValidationError" in exc_info.value.reason

    def test_transient_error_propagates_unchanged(self):
        """Transient errors neither drop a tier nor converge to the final exception; they bubble up as is for the caller's retry decorator to judge.

        瞬态错误既不降档也不收敛为终局异常，原样冒泡交调用方的重试装饰器判定。"""
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[instructor_api_call_exhausted(ConnectionError("503 service unavailable"))],
            ) as mock_gen,
            pytest.raises(ConnectionError),
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS]

    def test_truncation_does_not_fall_back(self):
        """Truncation is a hard error: resending the same request that will surely truncate again is pointless, so no tier drop.

        截断是硬错误：重发同一份必然再截断的请求没有意义，不降档。"""
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[TextOutputTruncatedError(provider="test-provider", model="test-model")],
            ) as mock_gen,
            pytest.raises(TextOutputTruncatedError),
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS]

    def test_downgrade_logs_raw_model_output(self, caplog):
        """The tier-drop trigger point logs the truncated raw model output as a warning.

        降档触发点以 warning 记录截断后的模型原始输出。"""
        sample = SampleModel(name="Dave", age=33)
        raw = "抱歉，我无法按要求输出 JSON。"
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[
                    _retry_exhausted(_no_tool_call_error(), content=raw),
                    _structured(sample.model_dump_json(), 10, 5),
                ],
            ),
            caplog.at_level("WARNING", logger="lib.backends.text_backends.instructor_support"),
        ):
            self._call()

        assert any(raw in record.getMessage() for record in caplog.records)

    def test_transient_error_keeps_its_type_after_unwrapping(self):
        """Transient errors bubble up with the Instructor wrapper removed, keeping the type for the caller's retry decorator to judge.

        瞬态错误剥掉 Instructor 包装后冒泡，保住类型供调用方的重试装饰器判定。"""
        from lib.infra.retry import BASE_RETRYABLE_ERRORS, should_retry

        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[instructor_api_call_exhausted(ConnectionError("connection reset by peer"))],
            ),
            pytest.raises(ConnectionError) as exc_info,
        ):
            self._call()

        # The message text contains no transient status-code pattern; only the exception type can prove it is retryable.
        # 消息文本不含任何瞬态状态码模式，只有异常类型能证明它可重试。
        assert should_retry(exc_info.value, BASE_RETRYABLE_ERRORS)

    def test_invalid_tool_call_arguments_are_terminal(self):
        """Upstream returned a tool call but the arguments are unusable: the validation kind, so no drop to the weaker-constrained MD_JSON.

        上游回了 tool call 但 arguments 不可用：属校验类，不降到约束更弱的 MD_JSON。"""
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[_retry_exhausted(_tool_call_args_invalid_error())],
            ) as mock_gen,
            pytest.raises(StructuredOutputExhaustedError),
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS]

    def test_api_failure_after_earlier_parse_failure_propagates(self):
        """A parse failure first, then a transient error: the terminating cause is the latter, which bubbles up as is rather than being treated as non-compliant model output.

        先解析失败一次、再撞上瞬态错误：终止原因是后者，原样冒泡而非当成模型输出不合规。"""
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[
                    _retry_exhausted(
                        ConnectionError("connection reset by peer"),
                        earlier_attempts=[_no_tool_call_error()],
                    )
                ],
            ) as mock_gen,
            pytest.raises(ConnectionError),
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS]

    def test_non_tools_bad_request_propagates(self):
        """A 400 unrelated to tools (e.g. context limit exceeded) bubbles up as is: models without the STRUCTURED_OUTPUT capability bit
        enter this chain directly without the native tier, and treating such a 400 as a tools incompatibility would replace the real cause.

        与 tools 无关的 400（如上下文超限）原样冒泡：无 STRUCTURED_OUTPUT 能力位的模型
        不经原生档直接进本链，把这类 400 当 tools 不兼容会替换掉真实的失败原因。"""
        rejection = _bad_request("This model's maximum context length is 8192 tokens")
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[instructor_api_call_exhausted(rejection)],
            ) as mock_gen,
            pytest.raises(BadRequestError),
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS]

    def test_tools_rejection_matched_case_insensitively(self):
        """Proxies' rejection texts vary in case; the predicate must normalize before matching, or the tier-drop path is useless.

        代理的拒收文案大小写不统一，判据须归一后再匹配，否则降档路径形同虚设。"""
        sample = SampleModel(name="Grace", age=31)
        rejection = _bad_request("Tools Are Not Supported By This Endpoint")
        with patch(
            "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
            side_effect=[instructor_api_call_exhausted(rejection), _structured(sample.model_dump_json(), 10, 5)],
        ) as mock_gen:
            result = self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS, Mode.MD_JSON]
        assert result.text == sample.model_dump_json()

    def test_downgrade_carries_billed_tokens_of_failed_mode(self):
        """The TOOLS tier got HTTP 200 (billed) before dropping a tier; those tokens are merged into the final result.

        TOOLS 档拿到过 HTTP 200（已计费）后才降档，这部分 token 并入最终结果。"""
        sample = SampleModel(name="Heidi", age=29)
        with patch(
            "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
            side_effect=[
                _retry_exhausted(_no_tool_call_error(), prompt_tokens=70, completion_tokens=30),
                _structured(sample.model_dump_json(), 10, 5),
            ],
        ):
            result = self._call()

        assert result.input_tokens == 80
        assert result.output_tokens == 35

    def test_downgrade_without_usage_keeps_tokens_untracked(self):
        """When neither side tracked usage, it stays None rather than collapsing to a literal 0 tokens by merging.

        两侧皆未追踪用量时保持 None，不因并账塌成字面 0 token。"""
        sample = SampleModel(name="Ivan", age=26)
        with patch(
            "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
            side_effect=[
                _retry_exhausted(_no_tool_call_error()),
                TextGenerationResult(text=sample.model_dump_json(), provider="", model=""),
            ],
        ):
            result = self._call()

        assert result.input_tokens is None
        assert result.output_tokens is None

    def test_last_mode_wire_rejection_is_terminal(self):
        """The last tier is still rejected upstream: no weaker tier to fall back to, so converge to the final exception rather than dropping tiers forever.

        末档仍被上游拒收：无更弱的档可退，收敛为终局异常而非无限降档。"""
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor",
                side_effect=[_tools_rejected_error(), _tools_rejected_error()],
            ) as mock_gen,
            pytest.raises(StructuredOutputExhaustedError, match="被上游拒收"),
        ):
            self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS, Mode.MD_JSON]


class TestStructuredModeChainAsync:
    """TOOLS -> MD_JSON fallback chain (async version), same rules as the sync version.

    TOOLS → MD_JSON 降级链（异步版），与同步版同口径。"""

    @staticmethod
    async def _call():
        return await instructor_fallback_async(
            client=AsyncMock(),
            model="async-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema=SampleModel,
            provider="async-provider",
        )

    @staticmethod
    def _modes(mock_gen) -> list[Mode]:
        return [call.kwargs["mode"] for call in mock_gen.call_args_list]

    async def test_wire_failure_falls_back_to_md_json(self):
        sample = SampleModel(name="Eve", age=45)
        with patch(
            "lib.backends.text_backends.instructor_support.generate_structured_via_instructor_async",
            side_effect=[_tools_rejected_error(), _structured(sample.model_dump_json(), 10, 5)],
        ) as mock_gen:
            result = await self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS, Mode.MD_JSON]
        assert [call.kwargs["provider"] for call in mock_gen.call_args_list] == ["async-provider", "async-provider"]
        assert result.text == sample.model_dump_json()

    async def test_validation_exhaustion_is_terminal(self):
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor_async",
                side_effect=[_retry_exhausted(_validation_error())],
            ) as mock_gen,
            pytest.raises(StructuredOutputExhaustedError) as exc_info,
        ):
            await self._call()

        assert self._modes(mock_gen) == [Mode.TOOLS]
        assert exc_info.value.provider == "async-provider"
        assert exc_info.value.model == "async-model"

    async def test_transient_error_propagates_unchanged(self):
        with (
            patch(
                "lib.backends.text_backends.instructor_support.generate_structured_via_instructor_async",
                side_effect=[instructor_api_call_exhausted(ConnectionError("503 service unavailable"))],
            ),
            pytest.raises(ConnectionError),
        ):
            await self._call()

    async def test_downgrade_carries_billed_tokens_of_failed_mode(self):
        sample = SampleModel(name="Judy", age=27)
        with patch(
            "lib.backends.text_backends.instructor_support.generate_structured_via_instructor_async",
            side_effect=[
                _retry_exhausted(_no_tool_call_error(), prompt_tokens=70, completion_tokens=30),
                _structured(sample.model_dump_json(), 10, 5),
            ],
        ):
            result = await self._call()

        assert result.input_tokens == 80
        assert result.output_tokens == 35


class TestStructuredModeChainThroughInstructor:
    """The fallback chain is driven by the real Instructor, stubbed only at the SDK boundary: the whole upstream channel returns only body text, never a tool call.

    The thinking block at the start of the body contains a draft JSON: the MD_JSON tier takes the first parseable JSON in the body,
    so only after stripping the thinking block is the parsed result the final answer after ``</think>``.

    降级链经真实 Instructor 驱动，只在 SDK 边界打桩：上游整条通道只回正文、从不回 tool call。

    正文开头的思考块里带一段草稿 JSON：MD_JSON 档取正文里第一段能解析的 JSON，只有先剥掉
    思考块，解析出的才是 ``</think>`` 之后的最终答案。
    """

    @staticmethod
    def _content_only_completion(*, prompt_tokens: int, completion_tokens: int) -> SimpleNamespace:
        from openai.types import CompletionUsage
        from openai.types.chat import ChatCompletionMessage

        message = ChatCompletionMessage(
            role="assistant",
            content='<think>先草拟 {"name": "Draft", "age": 99}，年龄不对，改掉。</think>\n{"name": "Bob", "age": 1}',
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=CompletionUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

    def test_sync_content_only_response_lands_on_md_json(self):
        """The TOOLS tier drops a tier after reask retries are exhausted; the MD_JSON tier parses the result from the same body; billing of all requests is merged.

        TOOLS 档 reask 重试耗尽后降档，MD_JSON 档从同样的正文解析出结果；各次请求的计费合并。"""
        from openai import OpenAI

        client = OpenAI(api_key="sk-test", base_url="https://proxy.invalid/v1")
        client.chat.completions.create = MagicMock(
            side_effect=[
                self._content_only_completion(prompt_tokens=11, completion_tokens=7),
                self._content_only_completion(prompt_tokens=12, completion_tokens=6),
                self._content_only_completion(prompt_tokens=13, completion_tokens=5),
                self._content_only_completion(prompt_tokens=14, completion_tokens=4),
            ]
        )

        result = instructor_fallback_sync(
            client=client,
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema=SampleModel,
            provider="test-provider",
        )

        assert result.text == SampleModel(name="Bob", age=1).model_dump_json()
        assert client.chat.completions.create.call_count == 4
        assert result.input_tokens == 50
        assert result.output_tokens == 22

    async def test_async_content_only_response_lands_on_md_json(self):
        """Same rules for the async entry: this is the production path of OpenAI-compatible backends through the structured fallback chain.

        异步入口同口径：这是 OpenAI 兼容后端走结构化降级链的生产路径。"""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key="sk-test", base_url="https://proxy.invalid/v1")
        client.chat.completions.create = AsyncMock(
            side_effect=[
                self._content_only_completion(prompt_tokens=11, completion_tokens=7),
                self._content_only_completion(prompt_tokens=12, completion_tokens=6),
                self._content_only_completion(prompt_tokens=13, completion_tokens=5),
                self._content_only_completion(prompt_tokens=14, completion_tokens=4),
            ]
        )

        result = await instructor_fallback_async(
            client=client,
            model="async-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema=SampleModel,
            provider="async-provider",
        )

        assert result.text == SampleModel(name="Bob", age=1).model_dump_json()
        assert client.chat.completions.create.await_count == 4
        assert result.input_tokens == 50
        assert result.output_tokens == 22


class TestModelFacingSchemaOnWire:
    """Request bodies sent through the real Instructor and OpenAI SDK carry no class docstring: the same for the TOOLS tier's tool
    description and params and for the schema injected by the MD_JSON tier.

    respx captures the first request at the transport layer and returns 400 to end the call; only the serialized request body is checked.

    经真实 Instructor 与 OpenAI SDK 发出的请求体里不带类 docstring：TOOLS 档的工具说明与参数、MD_JSON 档注入的
    schema 都一样。

    respx 在 transport 层截获首个请求后回 400 让调用结束，只看序列化后的请求体。
    """

    _ENDPOINT = "https://proxy.invalid/v1/chat/completions"
    _REJECT = httpx.Response(400, json={"error": {"message": "stop", "type": "invalid_request_error"}})

    @staticmethod
    def _wire_json(request: httpx.Request, mode: Mode) -> str:
        body = request_json(request)
        if mode is Mode.TOOLS:
            return json.dumps(body["tools"], ensure_ascii=False)
        system_content = body["messages"][0]["content"]
        # The schema really was injected, so the assertions below are meaningful.
        assert '"properties"' in system_content  # schema 确已注入，下面的断言才有意义
        return system_content

    @pytest.mark.parametrize("mode", [Mode.TOOLS, Mode.MD_JSON])
    @pytest.mark.parametrize("model", response_schema_params())
    def test_sync_request_has_no_class_docstrings(self, model: type[BaseModel], mode: Mode):
        from openai import OpenAI

        client = OpenAI(api_key="sk-test", base_url="https://proxy.invalid/v1", max_retries=0)
        with capture_http() as router:
            route = router.post(self._ENDPOINT).mock(return_value=self._REJECT)
            with pytest.raises(InstructorRetryException):
                generate_structured_via_instructor(
                    client=client,
                    model="test-model",
                    messages=[{"role": "user", "content": "test"}],
                    response_model=model,
                    mode=mode,
                )

        assert leaked_class_docstrings(model, self._wire_json(only_request(route), mode)) == []

    @pytest.mark.parametrize("mode", [Mode.TOOLS, Mode.MD_JSON])
    @pytest.mark.parametrize("model", response_schema_params())
    async def test_async_request_has_no_class_docstrings(self, model: type[BaseModel], mode: Mode):
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key="sk-test", base_url="https://proxy.invalid/v1", max_retries=0)
        with capture_http() as router:
            route = router.post(self._ENDPOINT).mock(return_value=self._REJECT)
            with pytest.raises(InstructorRetryException):
                await generate_structured_via_instructor_async(
                    client=client,
                    model="test-model",
                    messages=[{"role": "user", "content": "test"}],
                    response_model=model,
                    mode=mode,
                )

        assert leaked_class_docstrings(model, self._wire_json(only_request(route), mode)) == []


class TestInstructorFallbackAsync:
    """Tests for the high-level function instructor_fallback_async.

    instructor_fallback_async 高层函数测试。"""

    async def test_pydantic_schema_uses_instructor_async(self):
        """A Pydantic schema takes the async instructor path.

        Pydantic schema 走异步 instructor 路径。"""
        sample = SampleModel(name="Bob", age=25)

        with patch(
            "lib.backends.text_backends.instructor_support.generate_structured_via_instructor_async",
            return_value=_structured(sample.model_dump_json(), 40, 18),
        ):
            result = await instructor_fallback_async(
                client=AsyncMock(),
                model="async-model",
                messages=[{"role": "user", "content": "test"}],
                response_schema=SampleModel,
                provider="async-provider",
            )

        assert result.text == sample.model_dump_json()
        assert result.provider == "async-provider"
        assert result.model == "async-model"
        assert result.input_tokens == 40
        assert result.output_tokens == 18

    async def test_dict_schema_uses_json_object_async(self):
        """A dict schema takes the async json_object path.

        dict schema 走异步 json_object 路径。"""
        mock_client = AsyncMock()
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"k": "v"}'))],
            usage=SimpleNamespace(prompt_tokens=25, completion_tokens=12),
        )
        mock_client.chat.completions.create.return_value = mock_response

        result = await instructor_fallback_async(
            client=mock_client,
            model="async-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema={"type": "object"},
            provider="async-provider",
        )

        assert result.text == '{"k": "v"}'
        assert result.provider == "async-provider"
        assert result.input_tokens == 25
        assert result.output_tokens == 12
        assert result.model == "async-model"
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "async-model"
        assert call_kwargs["messages"] == [
            {"role": "system", "content": "Respond in JSON format."},
            {"role": "user", "content": "test"},
        ]

    async def test_pydantic_branch_forwards_token_param_async(self):
        """The async Pydantic branch's token_param reaches the wire: the value goes out under the param name max_completion_tokens.

        异步 Pydantic 分支的 token_param 一路传到导线：值以 max_completion_tokens 为参数名上线。"""
        sample = SampleModel(name="Bob", age=25)
        completion = SimpleNamespace(usage=None)

        client = AsyncMock()
        with _recorded_instructor((sample, completion), is_async=True) as (patched_with, calls):
            result = await instructor_fallback_async(
                client=client,
                model="async-model",
                messages=[{"role": "user", "content": "test"}],
                response_schema=SampleModel,
                provider="async-provider",
                max_tokens=600,
                token_param="max_completion_tokens",
            )

        assert issubclass(calls[0].pop("response_model"), SampleModel)
        assert calls == [
            {
                "model": "async-model",
                "messages": [{"role": "user", "content": "test"}],
                "max_retries": 2,
                "max_completion_tokens": 600,
            }
        ]
        assert patched_with == [{"client": client, "mode": Mode.TOOLS}]
        assert result.input_tokens is None
        assert result.output_tokens is None

    async def test_dict_branch_default_token_param_async(self):
        """The async dict branch goes out under the param name max_tokens by default.

        异步 dict 分支默认以 max_tokens 为参数名上线。"""
        mock_client = AsyncMock()
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"k": "v"}'))],
            usage=None,
        )
        mock_client.chat.completions.create.return_value = mock_response

        await instructor_fallback_async(
            client=mock_client,
            model="async-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema={"type": "object"},
            provider="async-provider",
            max_tokens=600,
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["max_tokens"] == 600
        assert "max_completion_tokens" not in call_kwargs

    async def test_dict_branch_explicit_token_param_async(self):
        """The async dict branch with an explicit token_param goes out under the param name max_completion_tokens.

        异步 dict 分支显式 token_param 时以 max_completion_tokens 为参数名上线。"""
        mock_client = AsyncMock()
        mock_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"k": "v"}'))],
            usage=None,
        )
        mock_client.chat.completions.create.return_value = mock_response

        await instructor_fallback_async(
            client=mock_client,
            model="async-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema={"type": "object"},
            provider="async-provider",
            max_tokens=600,
            token_param="max_completion_tokens",
        )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["max_completion_tokens"] == 600
        assert "max_tokens" not in call_kwargs

    async def test_dict_schema_strips_leading_think_block_async(self):
        """The async json_object path likewise strips the thinking block at the start of content.

        异步 json_object 路径同样剥掉 content 开头的思考块。"""
        mock_client = AsyncMock()
        mock_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='<think>想一想。</think>\n{"k": "v"}'))],
            usage=None,
        )

        result = await instructor_fallback_async(
            client=mock_client,
            model="async-model",
            messages=[{"role": "user", "content": "test"}],
            response_schema={"type": "object"},
            provider="async-provider",
        )

        assert result.text == '{"k": "v"}'

    async def test_dict_schema_truncation_raises_async(self):
        """Async dict schema truncation is likewise escalated to a hard error.

        异步 dict schema 截断同样升级为硬错误。"""
        mock_client = AsyncMock()
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="partial"), finish_reason="length"),
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=999),
        )
        mock_client.chat.completions.create.return_value = mock_response

        with pytest.raises(TextOutputTruncatedError) as exc_info:
            await instructor_fallback_async(
                client=mock_client,
                model="async-model",
                messages=[{"role": "user", "content": "test"}],
                response_schema={"type": "object"},
                provider="async-provider",
            )

        assert exc_info.value.provider == "async-provider"
        assert exc_info.value.model == "async-model"
        assert exc_info.value.output_tokens == 999
