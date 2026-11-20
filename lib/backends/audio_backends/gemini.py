"""GeminiAudioBackend — Gemini TTS speech synthesis backend (``generateContent`` + ``response_modalities=["AUDIO"]``).

Built-in Gemini (AI Studio / Vertex) and custom providers' ``gemini-tts`` endpoint share this backend. Audio is returned
in ``candidates[0].content.parts[].inline_data``: non-streaming requests to Gemini 3.8 TTS give a complete WAV by
default, earlier TTS models give headerless 16-bit PCM (``audio/L16;codec=pcm;rate=24000``). The latter is wrapped as
WAV using the sample rate and channels declared in the mime, so artifacts on disk are always directly playable and
editable. The schema follows the official docs listed in docs/api-docs/endpoints/gemini-tts.md.

GeminiAudioBackend — Gemini TTS 语音合成后端（``generateContent`` + ``response_modalities=["AUDIO"]``）。

内置 Gemini（AI Studio / Vertex）与自定义供应商的 ``gemini-tts`` 端点共用本后端。音频在
``candidates[0].content.parts[].inline_data`` 里返回：Gemini 3.8 TTS 的非流式请求默认给完整 WAV，
更早的 TTS 模型给不带头的 16-bit PCM（``audio/L16;codec=pcm;rate=24000``）。后者按 mime 声明的
采样率与声道封装为 WAV，落盘产物一律可直接播放和剪辑。schema 依据
docs/api-docs/endpoints/gemini-tts.md 所列官方文档。
"""

from __future__ import annotations

import json as json_module
import logging
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from lib.backends.audio_backends.base import (
    AudioCapability,
    AudioSynthesisRequest,
    AudioSynthesisResult,
    VoiceOption,
)
from lib.backends.gemini_shared import VERTEX_SCOPES, gemini_http_options, resolve_gemini_api_key, with_retry_async
from lib.backends.transport_layer import TransportLayer
from lib.config.system_config import resolve_vertex_credentials_path
from lib.config.url_utils import normalize_base_url
from lib.infra.async_thread import run_noninterruptible_sync

if TYPE_CHECKING:
    from google.genai.types import GenerateContentResponseUsageMetadata

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.8-flash-tts"

# The 30 official preset voices; source: docs/api-docs/endpoints/gemini-tts.md. label is the id itself, same rule as openai-tts.
# 官方 30 个预置音色，出处见 docs/api-docs/endpoints/gemini-tts.md。label 取 id 本身，与 openai-tts 同口径。
_VOICE_CATALOG: tuple[VoiceOption, ...] = tuple(
    VoiceOption(id=voice_id, label=voice_id)
    for voice_id in (
        "Zephyr",
        "Puck",
        "Charon",
        "Kore",
        "Fenrir",
        "Leda",
        "Orus",
        "Aoede",
        "Callirrhoe",
        "Autonoe",
        "Enceladus",
        "Iapetus",
        "Umbriel",
        "Algieba",
        "Despina",
        "Erinome",
        "Algenib",
        "Rasalgethi",
        "Laomedeia",
        "Achernar",
        "Alnilam",
        "Schedar",
        "Gacrux",
        "Pulcherrima",
        "Achird",
        "Zubenelgenubi",
        "Vindemiatrix",
        "Sadachbia",
        "Sadaltager",
        "Sulafat",
    )
)

# Official default output when no rate parameter is given: 24 kHz, mono, 16-bit / 未带 rate 参数时的官方缺省输出：24 kHz、单声道、16-bit
_DEFAULT_SAMPLE_RATE = 24000
_PCM_SAMPLE_WIDTH = 2


def _pcm_params(mime_type: str | None) -> tuple[int, int]:
    """Read (sample rate, channel count) from a mime of the form ``audio/L16;codec=pcm;rate=24000``.

    从 ``audio/L16;codec=pcm;rate=24000`` 形态的 mime 读出 (采样率, 声道数)。"""
    params: dict[str, str] = {}
    for item in (mime_type or "").split(";")[1:]:
        key, _, value = item.partition("=")
        params[key.strip().lower()] = value.strip()
    rate = int(params.get("rate") or _DEFAULT_SAMPLE_RATE)
    channels = int(params.get("channels") or 1)
    return rate, channels


def _write_audio(data: bytes, mime_type: str | None, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if data[:4] == b"RIFF":
        output_path.write_bytes(data)
        return
    # Only raw PCM can be wrapped as WAV; when a relay returns a compressed format like mp3, forcing a wrapper would only produce noise
    # 只有裸 PCM 才能封装成 WAV；中转站回了 mp3 等压缩格式时，硬封装只会得到一段噪声
    if mime_type and not mime_type.lower().startswith(("audio/l16", "audio/pcm")):
        raise RuntimeError(f"Gemini speech synthesis returned an unsupported audio format: {mime_type}")
    rate, channels = _pcm_params(mime_type)
    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(_PCM_SAMPLE_WIDTH)
        wav.setframerate(rate)
        wav.writeframes(data)


class GeminiAudioBackend:
    """Gemini TTS speech synthesis backend, supporting AI Studio and Vertex AI. / Gemini TTS 语音合成后端，支持 AI Studio 与 Vertex AI。"""

    def __init__(
        self,
        *,
        backend_type: str = "aistudio",
        api_key: str | None = None,
        base_url: str | None = None,
        credentials_path: str | None = None,
        model: str | None = None,
        provider_name: str | None = None,
        transport_layer: TransportLayer | None = None,
    ) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self._backend_type = backend_type.strip().lower()
        self._model = model or DEFAULT_MODEL
        # When a custom provider connects via gemini-tts, bill with the real provider, consistent with the wrapper's .name
        # 自定义供应商经 gemini-tts 接入时用真实 provider 记账，与包装层 .name 一致
        self._provider_name = provider_name or f"gemini-{self._backend_type}"

        if self._backend_type == "vertex":
            from google.oauth2 import service_account

            credentials_file = Path(credentials_path) if credentials_path else resolve_vertex_credentials_path()
            if credentials_file is None:
                raise ValueError("Vertex AI credentials file not found")
            with open(credentials_file, encoding="utf-8") as f:
                project_id = json_module.load(f).get("project_id")
            credentials = service_account.Credentials.from_service_account_file(
                str(credentials_file), scopes=VERTEX_SCOPES
            )
            # Gemini 3.8 TTS is only offered in global / Gemini 3.8 TTS 只在 global 提供
            self._client = genai.Client(vertexai=True, project=project_id, location="global", credentials=credentials)
        else:
            effective_base_url = normalize_base_url(base_url)
            self._client = genai.Client(
                api_key=resolve_gemini_api_key(api_key),
                http_options=gemini_http_options(effective_base_url, transport_layer),
            )

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[AudioCapability]:
        # Don't declare SPEECH_SPEED: Gemini TTS has no speed parameter; speed can only be written into the style instruction, which can't give a reproducible multiplier.
        # 不声明 SPEECH_SPEED：Gemini TTS 没有语速参数，语速只能写进风格指令，给不出可复现的倍率。
        return {AudioCapability.TEXT_TO_SPEECH}

    def list_voices(self) -> list[VoiceOption]:
        return list(_VOICE_CATALOG)

    async def synthesize(self, request: AudioSynthesisRequest) -> AudioSynthesisResult:
        if request.speed is not None:
            logger.debug("Gemini TTS 不支持语速参数 speed=%s，已忽略", request.speed)
        # language_type is not sent: Gemini TTS detects the language from the input text automatically.
        # The billed call is separate from writing to disk: retries only wrap the API call, and a disk write failure never reruns the synthesis request that would bill again.
        # language_type 不发送：Gemini TTS 按输入文本自动识别语种。
        # 计费调用与写盘分离：重试只包 API 调用，写盘失败不回头重跑会再次计费的合成请求。
        data, mime_type, usage = await self._request_speech(request)
        await run_noninterruptible_sync(_write_audio, data, mime_type, request.output_path)

        logger.info("Gemini 语音合成完成: %s", request.output_path)
        return AudioSynthesisResult(
            provider=self._provider_name,
            model=self._model,
            text=request.text,
            output_path=request.output_path,
            # Priced by token: input is text tokens, output is all audio tokens. / 按 token 计价：输入是文本 token，输出全是音频 token。
            input_tokens=usage.prompt_token_count if usage is not None else None,
            audio_output_tokens=usage.candidates_token_count if usage is not None else None,
        )

    @with_retry_async()
    async def _request_speech(
        self, request: AudioSynthesisRequest
    ) -> tuple[bytes, str | None, GenerateContentResponseUsageMetadata | None]:
        """Submit the synthesis request (billed stage), returning the audio bytes, mime and this call's usage.

        提交合成请求（计费段），返回音频字节、mime 与这次调用的 usage。"""
        types = self._types
        # prebuilt_voice_config is accepted by both old and new TTS models; voice_config.voice added in 3.8 isn't recognized by old models
        # prebuilt_voice_config 新旧 TTS 模型都接受；3.8 新增的 voice_config.voice 旧模型不认
        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=request.voice),
                )
            ),
        )
        logger.info(
            "调用 %s 语音合成 API model=%s voice=%s chars=%d",
            self.name,
            self._model,
            request.voice,
            len(request.text),
        )
        response = await self._client.aio.models.generate_content(
            model=self._model, contents=request.text, config=config
        )
        for candidate in response.candidates or []:
            for part in (candidate.content.parts if candidate.content else None) or []:
                if part.inline_data is not None and part.inline_data.data:
                    return part.inline_data.data, part.inline_data.mime_type, response.usage_metadata
        # Synthesis is already billed on the provider side; retrying would bill again, so fail directly and let the task layer handle it
        # 合成已在供应商侧计费，重试等于再次计费，直接失败交由任务层处理
        raise RuntimeError("Gemini speech synthesis returned no audio")
