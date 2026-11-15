"""Core interface definitions of the speech synthesis (TTS) service layer. / 语音合成（TTS）服务层核心接口定义。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class AudioCapability(StrEnum):
    """音频后端支持的能力枚举。"""

    TEXT_TO_SPEECH = "text_to_speech"
    #: The synthesis request's ``speed`` is passed to the provider. Backends that don't declare it ignore speed, and the narration speed in project settings has no effect on them.
    #: 合成请求的 ``speed`` 会传给供应商。未声明的 backend 忽略语速，项目设置里的配音语速对它不生效。
    SPEECH_SPEED = "speech_speed"


@dataclass(frozen=True)
class VoiceOption:
    """Voice catalog entry. ``id`` is the ``voice`` parameter value carried by synthesis requests, ``label`` is the human-readable display name.

    音色目录条目。``id`` 是合成请求携带的 ``voice`` 参数值，``label`` 是给人看的展示名。"""

    id: str
    label: str


@dataclass
class AudioSynthesisRequest:
    """Generic speech synthesis request. Each Backend ignores fields it doesn't support. / 通用语音合成请求。各 Backend 忽略不支持的字段。"""

    text: str
    output_path: Path
    voice: str
    language_type: str = "Chinese"
    # Speed only takes effect for backends declaring ``AudioCapability.SPEECH_SPEED``; synchronous qwen3-tts-flash doesn't support it
    # (speech_rate is only available in the realtime WebSocket version), so the backend logs at debug and ignores it.
    # 语速只对声明 ``AudioCapability.SPEECH_SPEED`` 的 backend 生效；同步 qwen3-tts-flash 不支持
    # （speech_rate 仅 realtime WebSocket 版可用），后端记 debug log 忽略。
    speed: float | None = None


@dataclass
class AudioSynthesisResult:
    """Generic speech synthesis result.

    Usage is reported per the usage contract (docs/adr/0100): ``text`` is the raw text sent to the provider; when priced
    by character the calculator counts characters per the price table's "Han character counts as 2" attribute; token-priced
    models also report ``input_tokens`` (input tokens) and ``audio_output_tokens`` (output audio tokens), ``None`` when the
    provider doesn't report them.

    通用语音合成结果。

    用量按用量契约上报（docs/adr/0100）：``text`` 是发给供应商的原始文本，按字符计价时字符数由计算器按价目表
    的「汉字计 2」属性统计；按 token 计价的模型另报 ``input_tokens``（输入 token）与 ``audio_output_tokens``
    （输出音频 token），供应商没有回报时为 ``None``。
    """

    provider: str
    model: str
    text: str
    output_path: Path
    input_tokens: int | None = None
    audio_output_tokens: int | None = None


class AudioBackend(Protocol):
    """Speech synthesis backend protocol. / 语音合成后端协议。"""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def capabilities(self) -> set[AudioCapability]: ...

    def list_voices(self) -> list[VoiceOption]: ...

    async def synthesize(self, request: AudioSynthesisRequest) -> AudioSynthesisResult: ...
