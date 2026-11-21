"""App-level exception handler tests: status code mapping, Accept-Language translation, redaction.

app 级异常处理器测试：状态码映射、Accept-Language 翻译、脱敏。"""

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lib.catalog.errors import ModelNotInCatalogError
from lib.generation.generation_queue import ActiveTaskRequestConflict
from lib.generation.generation_queue_client import TaskSpecValidationError
from lib.i18n import _
from lib.infra.api_errors import ApiError, BadRequestError, NotFoundError, UnprocessableError
from lib.script.script_editor import ScriptEditError
from server.error_handlers import register_error_handlers

# Built at runtime from the system tmp directory; no machine-specific absolute paths committed.
# 运行时基于系统 tmp 目录构造，不提交机器特定的绝对路径。
_SERVER_PATH = str(Path(tempfile.gettempdir()) / "projects" / "demo" / "episode_1.json")


def _make_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/api-error-404")
    async def _api_error_404():
        raise NotFoundError("segment_not_found", id="E1S01")

    @app.get("/api-error-400")
    async def _api_error_400():
        raise BadRequestError("audio_provider_not_configured")

    @app.get("/api-error-422-with-diagnostic")
    async def _api_error_422_with_diagnostic():
        raise UnprocessableError("script_validation_failed").with_diagnostic("scenes[0].shots must be a list")

    @app.get("/api-error-custom-status")
    async def _api_error_custom():
        raise ApiError("internal_server_error", status_code=503)

    @app.get("/task-spec-error")
    async def _task_spec_error():
        raise TaskSpecValidationError("prompt_text_empty")

    @app.get("/active-video-request-conflict")
    async def _active_video_request_conflict():
        raise ActiveTaskRequestConflict(resource_id="E1S01", existing_task_id="task-existing")

    @app.get("/model-not-in-catalog")
    async def _model_not_in_catalog():
        raise ModelNotInCatalogError(provider_id="ark", model_id="doubao-seed-1-8-251228", media_type="text")

    @app.get("/provider-without-catalog-model")
    async def _provider_without_catalog_model():
        raise ModelNotInCatalogError(provider_id="openai", model_id="", media_type="video")

    @app.get("/script-edit-error")
    async def _script_edit_error():
        raise ScriptEditError("segments 必须是列表，当前为 NoneType")

    @app.get("/script-edit-error-keyed")
    async def _script_edit_error_keyed():
        raise ScriptEditError(
            "segments 必须是列表，当前为 NoneType",
            key="script_edit_items_not_list",
            kind="segments",
            type_name="NoneType",
        )

    @app.get("/file-not-found")
    async def _file_not_found():
        raise FileNotFoundError(f"剧本文件不存在: {_SERVER_PATH}")

    @app.get("/unexpected")
    async def _unexpected():
        raise RuntimeError(f"boom at {_SERVER_PATH}")

    return TestClient(app, raise_server_exceptions=False)


class TestApiErrorHandler:
    @pytest.mark.parametrize(
        ("path", "status", "key"),
        [
            ("/api-error-422-with-diagnostic", 422, "script_validation_failed"),
            ("/task-spec-error", 400, "prompt_text_empty"),
        ],
    )
    async def test_catalog_database_unavailable_preserves_original_error(
        self, tmp_path, set_error_handler_sessions, path, status, key
    ):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/missing/db.sqlite")
        set_error_handler_sessions(async_sessionmaker(engine))
        try:
            response = _make_client().get(path)
            assert response.status_code == status
            assert response.json()["detail"] == _(key)
            if status == 422:
                assert response.json()["diagnostic"] == "scenes[0].shots must be a list"
        finally:
            await engine.dispose()

    def test_not_found_translated_zh_default(self):
        client = _make_client()
        resp = client.get("/api-error-404")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "分镜 'E1S01' 不存在"

    def test_bad_request_400(self):
        client = _make_client()
        resp = client.get("/api-error-400")
        assert resp.status_code == 400
        assert "音频" in resp.json()["detail"]

    def test_accept_language_en(self):
        client = _make_client()
        resp = client.get("/api-error-404", headers={"Accept-Language": "en-US,en;q=0.9"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Segment 'E1S01' does not exist"

    def test_accept_language_vi(self):
        client = _make_client()
        resp = client.get("/api-error-404", headers={"Accept-Language": "vi"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Đoạn 'E1S01' không tồn tại"

    def test_diagnostic_absent_when_not_supplied(self):
        """Without diagnostic info the response body has only the summary; the shape existing consumers read is
        unchanged.

        未带诊断信息时响应体只有摘要，既有消费方读到的形状不变。"""
        client = _make_client()
        resp = client.get("/api-error-404")
        assert "diagnostic" not in resp.json()

    def test_diagnostic_carries_technical_detail_beside_summary(self):
        """Technical info such as field names / schema goes only into diagnostic; detail stays a product-language
        summary.

        字段名 / schema 这类技术信息只进 diagnostic，detail 保持产品语言摘要。"""
        client = _make_client()
        resp = client.get("/api-error-422-with-diagnostic")
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"] == "脚本结构校验失败，请检查后重试"
        assert body["diagnostic"] == "scenes[0].shots must be a list"

    def test_custom_status_code(self):
        client = _make_client()
        resp = client.get("/api-error-custom-status")
        assert resp.status_code == 503


class TestLibExceptionHandlers:
    def test_active_video_request_conflict_409(self):
        client = _make_client()
        resp = client.get("/active-video-request-conflict", headers={"Accept-Language": "en"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == (
            "Unit 'E1S01' already has a video task with a different confirmed request duration; "
            "wait for it to finish before retrying (a task that is still queued can be cancelled first)."
        )

    def test_task_spec_validation_error_400(self):
        client = _make_client()
        resp = client.get("/task-spec-error")
        assert resp.status_code == 400
        # detail is the translated finished text, not a bare code
        # detail 是翻译后的成品文案，不是裸 code
        assert resp.json()["detail"] != "prompt_text_empty"

    def test_model_not_in_catalog_400_asks_to_reselect(self):
        client = _make_client()
        resp = client.get("/model-not-in-catalog", headers={"Accept-Language": "en"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "doubao-seed-1-8-251228" in detail
        assert "re-select a model" in detail

    def test_provider_without_catalog_model_names_the_media_type(self):
        client = _make_client()
        resp = client.get("/provider-without-catalog-model", headers={"Accept-Language": "en"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "does not support video generation" in detail
        assert "/:" not in detail

    def test_script_edit_error_400(self):
        client = _make_client()
        resp = client.get("/script-edit-error")
        assert resp.status_code == 400
        assert "损坏" in resp.json()["detail"]

    def test_script_edit_error_keyed_reason_translated_en(self):
        """For a ScriptEditError with key/params (like those raised by resolve_items) in an en request,
        reason must be translated to English, without mixing in the original Chinese.

        带 key/params 的 ScriptEditError（如 resolve_items 抛出的那种）en 请求下，
        reason 须按英文翻译，不得混入中文原文。"""
        client = _make_client()
        resp = client.get("/script-edit-error-keyed", headers={"Accept-Language": "en"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail == "Script data is corrupted: segments must be a list, but got NoneType"
        assert not any("一" <= ch <= "鿿" for ch in detail)

    def test_script_edit_error_keyed_reason_translated_vi(self):
        client = _make_client()
        resp = client.get("/script-edit-error-keyed", headers={"Accept-Language": "vi"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail == "Dữ liệu kịch bản bị hỏng: segments phải là một danh sách, nhưng nhận được NoneType"
        assert not any("一" <= ch <= "鿿" for ch in detail)

    def test_file_not_found_404_hides_server_path(self):
        client = _make_client()
        resp = client.get("/file-not-found")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "请求的资源不存在"
        assert _SERVER_PATH not in resp.text

    def test_unexpected_exception_500_hides_details(self):
        client = _make_client()
        resp = client.get("/unexpected")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "服务器内部错误，请稍后重试"
        assert "boom" not in resp.text
        assert _SERVER_PATH not in resp.text


class TestRealAppRegistration:
    def test_server_app_registers_all_handlers(self):
        from server.app import app as real_app

        for exc_type in (
            ApiError,
            TaskSpecValidationError,
            ModelNotInCatalogError,
            ScriptEditError,
            FileNotFoundError,
            Exception,
            StarletteHTTPException,
            RequestValidationError,
        ):
            assert exc_type in real_app.exception_handlers, f"{exc_type} 未注册 app 级 handler"

    def test_openapi_documents_the_error_body(self):
        """YouReel (G4): every operation lists the shared ErrorResponse for 4XX / 5XX.

        YouReel（G4）：每个操作都为 4XX / 5XX 列出共用的 ErrorResponse。"""
        from server.app import app as real_app

        spec = real_app.openapi()
        schema = spec["components"]["schemas"]["ErrorResponse"]
        assert {"detail", "code", "params"} <= set(schema["properties"])
        operation = spec["paths"]["/api/v1/projects"]["get"]
        assert operation["responses"]["4XX"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }


def _make_cors_client(allow_origins, allow_credentials) -> TestClient:
    app = FastAPI()
    register_error_handlers(app, cors_allow_origins=allow_origins, cors_allow_credentials=allow_credentials)

    @app.get("/unexpected")
    async def _unexpected():
        raise RuntimeError("boom")

    return TestClient(app, raise_server_exceptions=False)


class TestUnexpectedErrorCorsHeaders:
    """When ServerErrorMiddleware sends the fallback 500 it bypasses CORSMiddleware (using the outermost raw send),
    so the handler must add CORS headers by hand, otherwise a cross-origin frontend treats the 500 as a network
    error.

    ServerErrorMiddleware 兜底发 500 时绕过 CORSMiddleware（走最外层原始 send），
    handler 需手工补齐 CORS 头，否则跨域前端把 500 当成 network error。"""

    def test_wildcard_origin_gets_wildcard_header(self):
        client = _make_cors_client(["*"], False)
        resp = client.get("/unexpected", headers={"Origin": "https://example.com"})
        assert resp.status_code == 500
        assert resp.headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in resp.headers

    def test_allowlisted_origin_with_credentials_gets_explicit_origin(self):
        client = _make_cors_client(["https://example.com"], True)
        resp = client.get("/unexpected", headers={"Origin": "https://example.com"})
        assert resp.status_code == 500
        assert resp.headers.get("access-control-allow-origin") == "https://example.com"
        assert resp.headers.get("access-control-allow-credentials") == "true"
        assert resp.headers.get("vary") == "Origin"

    def test_disallowed_origin_gets_no_allow_origin_header(self):
        client = _make_cors_client(["https://allowed.example.com"], True)
        resp = client.get("/unexpected", headers={"Origin": "https://evil.example.com"})
        assert resp.status_code == 500
        assert "access-control-allow-origin" not in resp.headers

    def test_no_origin_header_means_no_cors_headers(self):
        client = _make_cors_client(["*"], False)
        resp = client.get("/unexpected")
        assert resp.status_code == 500
        assert "access-control-allow-origin" not in resp.headers

    def test_default_kwargs_fall_back_to_wildcard(self):
        # Conservative default when register_error_handlers gets no cors argument: wildcard origins, no credentials.
        # register_error_handlers 不传 cors 参数时的保守默认值：通配 origins，不开 credentials。
        app = FastAPI()
        register_error_handlers(app)

        @app.get("/unexpected")
        async def _unexpected():
            raise RuntimeError("boom")

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/unexpected", headers={"Origin": "https://example.com"})
        assert resp.status_code == 500
        assert resp.headers.get("access-control-allow-origin") == "*"


def _make_g4_client() -> TestClient:
    """YouReel (G4): routes exercising the machine-readable ``code`` / ``params`` of every error kind.

    YouReel（G4）：覆盖各类错误的机器可读 ``code`` / ``params`` 的路由。"""
    from fastapi import HTTPException
    from pydantic import BaseModel

    from server.i18n import Translator

    app = FastAPI()
    register_error_handlers(app)

    class _Body(BaseModel):
        name: str
        count: int

    @app.get("/http-keyed")
    async def _http_keyed(_t: Translator):
        raise HTTPException(status_code=404, detail=_t("segment_not_found", id="E1S01"))

    @app.get("/http-keyed-secret")
    async def _http_keyed_secret(_t: Translator):
        raise HTTPException(
            status_code=400,
            detail=_t("file_not_found", path=_SERVER_PATH, api_key="sk-secret-value"),
        )

    @app.get("/http-nested")
    async def _http_nested(_t: Translator):
        raise HTTPException(
            status_code=400, detail=_t("script_data_corrupted", reason=_t("script_edit_error"))
        )

    @app.get("/http-literal")
    async def _http_literal():
        raise HTTPException(status_code=409, detail="The studio agent has no model configured.")

    @app.get("/http-bare-code")
    async def _http_bare_code():
        raise HTTPException(status_code=404, detail="no_editor_save")

    @app.get("/http-structured")
    async def _http_structured():
        raise HTTPException(status_code=413, detail={"code": "media_too_large", "asset_id": "a1"})

    @app.get("/http-unauthorized")
    async def _http_unauthorized():
        raise HTTPException(status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/api-error-params")
    async def _api_error_params():
        raise NotFoundError("segment_not_found", id="E1S01")

    @app.get("/api-error-diagnostic")
    async def _api_error_diagnostic():
        raise UnprocessableError("script_validation_failed").with_diagnostic("scenes[0].shots must be a list")

    @app.get("/active-video-request-conflict")
    async def _active_video_request_conflict():
        raise ActiveTaskRequestConflict(resource_id="E1S01", existing_task_id="task-existing")

    @app.get("/model-not-in-catalog")
    async def _model_not_in_catalog():
        raise ModelNotInCatalogError(provider_id="ark", model_id="doubao-seed-1-8-251228", media_type="text")

    @app.get("/task-spec-error")
    async def _task_spec_error():
        raise TaskSpecValidationError("prompt_text_empty")

    @app.get("/script-edit-error-keyed")
    async def _script_edit_error_keyed():
        raise ScriptEditError("x", key="script_edit_items_not_list", kind="segments", type_name="NoneType")

    @app.post("/validated")
    async def _validated(body: _Body):
        return body

    @app.get("/file-not-found")
    async def _file_not_found():
        raise FileNotFoundError(f"剧本文件不存在: {_SERVER_PATH}")

    @app.get("/unexpected")
    async def _unexpected():
        raise RuntimeError(f"boom at {_SERVER_PATH}")

    return TestClient(app, raise_server_exceptions=False)


class TestMachineReadableErrors:
    """YouReel (G4): every error body is ``{"detail", "code", "params", "diagnostic"?}``; ``detail`` is unchanged.

    YouReel（G4）：每个错误体都是 ``{"detail", "code", "params", "diagnostic"?}``；``detail`` 不变。"""

    def test_http_exception_with_translated_detail_gets_key_and_params(self):
        resp = _make_g4_client().get("/http-keyed", headers={"Accept-Language": "en"})
        assert resp.status_code == 404
        assert resp.json() == {
            "detail": "Segment 'E1S01' does not exist",
            "code": "segment_not_found",
            "params": {"id": "E1S01"},
        }

    def test_params_hide_secrets_and_server_paths(self):
        resp = _make_g4_client().get("/http-keyed-secret")
        body = resp.json()
        assert body["code"] == "file_not_found"
        assert body["params"] == {"path": "<path>"}
        assert "sk-secret-value" not in resp.text

    def test_nested_rendered_param_carries_its_code(self):
        body = _make_g4_client().get("/http-nested").json()
        assert body["code"] == "script_data_corrupted"
        assert body["params"]["reason"] == _("script_edit_error")
        assert body["params"]["reason_code"] == "script_edit_error"

    @pytest.mark.parametrize(
        ("path", "status", "code"),
        [
            ("/http-literal", 409, "conflict"),
            ("/http-bare-code", 404, "no_editor_save"),
            ("/http-structured", 413, "media_too_large"),
            ("/http-unauthorized", 401, "unauthorized"),
        ],
    )
    def test_http_exception_without_key_gets_detail_code_or_generic_code(self, path, status, code):
        resp = _make_g4_client().get(path)
        assert resp.status_code == status
        body = resp.json()
        assert body["code"] == code
        assert body["params"] == {}

    def test_structured_detail_is_kept_as_is(self):
        body = _make_g4_client().get("/http-structured").json()
        assert body["detail"] == {"code": "media_too_large", "asset_id": "a1"}

    def test_http_exception_keeps_headers(self):
        resp = _make_g4_client().get("/http-unauthorized")
        assert resp.headers.get("www-authenticate") == "Bearer"
        assert resp.json()["detail"] == "Not authenticated"

    def test_unknown_route_and_method_get_generic_codes(self):
        client = _make_g4_client()
        missing = client.get("/no-such-route")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Not Found", "code": "not_found", "params": {}}
        wrong_method = client.delete("/http-keyed")
        assert wrong_method.status_code == 405
        assert wrong_method.json()["code"] == "method_not_allowed"

    def test_domain_error_with_params(self):
        resp = _make_g4_client().get("/api-error-params")
        assert resp.json() == {"detail": "分镜 'E1S01' 不存在", "code": "segment_not_found", "params": {"id": "E1S01"}}

    def test_domain_error_with_diagnostic(self):
        body = _make_g4_client().get("/api-error-diagnostic").json()
        assert body["code"] == "script_validation_failed"
        assert body["params"] == {}
        assert body["diagnostic"] == "scenes[0].shots must be a list"

    @pytest.mark.parametrize(
        ("path", "code", "params"),
        [
            (
                "/active-video-request-conflict",
                "video_request_conflicts_with_active_task",
                {"resource_id": "E1S01", "existing_task_id": "task-existing"},
            ),
            (
                "/model-not-in-catalog",
                "model_not_in_provider_catalog",
                {"provider": "ark", "model": "doubao-seed-1-8-251228"},
            ),
            ("/task-spec-error", "prompt_text_empty", {}),
            (
                "/script-edit-error-keyed",
                "script_data_corrupted",
                {
                    "reason_code": "script_edit_items_not_list",
                    "reason_params": {"kind": "segments", "type_name": "NoneType"},
                },
            ),
        ],
    )
    def test_lib_exceptions_carry_their_codes(self, path, code, params):
        body = _make_g4_client().get(path).json()
        assert body["code"] == code
        assert body["params"] == params

    def test_request_validation_keeps_detail_list_and_adds_compact_errors(self):
        resp = _make_g4_client().post("/validated", json={"name": "x", "count": "not-a-number"})
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "validation_failed"
        # detail stays FastAPI's list, so existing consumers keep reading ``msg`` / ``loc``
        # detail 保持 FastAPI 的列表，既有消费方照旧读 ``msg`` / ``loc``
        assert isinstance(body["detail"], list)
        assert body["detail"][0]["loc"] == ["body", "count"]
        assert "input" in body["detail"][0]
        assert body["params"]["errors"] == [
            {"type": body["detail"][0]["type"], "loc": ["body", "count"], "msg": body["detail"][0]["msg"]}
        ]

    def test_file_not_found_has_code_without_path(self):
        resp = _make_g4_client().get("/file-not-found")
        assert resp.json() == {"detail": "请求的资源不存在", "code": "resource_not_found", "params": {}}
        assert _SERVER_PATH not in resp.text

    def test_unexpected_500_has_code_without_details(self):
        resp = _make_g4_client().get("/unexpected")
        assert resp.status_code == 500
        assert resp.json() == {"detail": "服务器内部错误，请稍后重试", "code": "internal_server_error", "params": {}}
        assert "boom" not in resp.text
        assert _SERVER_PATH not in resp.text


class TestMachineParams:
    """YouReel (G4): the param encoder alone. / YouReel（G4）：参数编码器本身。"""

    def test_relative_paths_and_urls_stay(self):
        from server.error_handlers import machine_params

        params = machine_params(None, {"path": "scripts/episode_1.json", "url": "https://example.com/a/b"})
        assert params == {"path": "scripts/episode_1.json", "url": "https://example.com/a/b"}

    def test_absolute_paths_masked_inside_text_and_nested_values(self):
        from server.error_handlers import machine_params

        params = machine_params(None, {"err_msg": "cannot open /srv/data/x.json now", "gaps": [{"p": "C:\\data\\x"}]})
        assert params == {"err_msg": "cannot open <path> now", "gaps": [{"p": "<path>"}]}

    def test_credentials_dropped_by_name(self):
        from server.error_handlers import machine_params

        params = machine_params(None, {"api_key": "k", "token": "t", "max_tokens": 5, "key_id": "abc"})
        assert params == {"max_tokens": 5, "key_id": "abc"}

    def test_unserializable_values_become_text(self):
        from server.error_handlers import machine_params

        class _Opaque:
            def __str__(self) -> str:
                return "opaque"

        assert machine_params(None, {"value": _Opaque()}) == {"value": "opaque"}
