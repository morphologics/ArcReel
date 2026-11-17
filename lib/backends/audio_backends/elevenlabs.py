"""ElevenLabsAudioBackend — ElevenLabs synchronous speech synthesis (``POST {host}/v1/text-to-speech/{voice_id}``).

YouReel addition (owner 2026-11-17), reached through the custom providers' ``elevenlabs-tts`` endpoint. The response
body is the audio file itself, requested as ``wav_24000`` (WAV and PCM at 44.1 kHz need a Pro plan) or as MP3 when the
output path ends in ``.mp3``. Voices come from the account (``GET /v2/voices``: premade, cloned, generated and
professional voices), cached for a few minutes per key, with ElevenLabs' default voices as the fallback when the list
can't be read. Schema per the official docs listed in docs/api-docs/endpoints/elevenlabs-tts.md.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path

import httpx

from lib.backends.artifact_download_guard import artifact_http_client
from lib.backends.audio_backends.base import (
    AudioCapability,
    AudioSynthesisRequest,
    AudioSynthesisResult,
    VoiceOption,
)
from lib.backends.backend_runtime import should_retry_submit, submit_post
from lib.backends.transport_layer import TransportLayer
from lib.infra.async_thread import run_noninterruptible_sync
from lib.infra.retry import with_retry_async

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_multilingual_v2"

# Speed range accepted in voice_settings.speed; the narration speed is clamped into it
_SPEED_MIN = 0.7
_SPEED_MAX = 1.2

# ElevenLabs' default (premade) voices, shown when the account's voice list can't be read
_DEFAULT_VOICES: tuple[VoiceOption, ...] = tuple(
    VoiceOption(id=voice_id, label=label)
    for voice_id, label in (
        ("EXAVITQu4vr4xnSDxMaL", "Sarah"),
        ("FGY2WhTYpPnrIDTdsKH5", "Laura"),
        ("IKne3meq5aSn9XLyUdCD", "Charlie"),
        ("JBFqnCBsd6RMkjVDRZzb", "George"),
        ("N2lVS1w4EtoT3dr4eOWO", "Callum"),
        ("TX3LPaxmHKxFdv7VOQHJ", "Liam"),
        ("XB0fDUnXU5powFXDhCwa", "Charlotte"),
        ("Xb7hH8MSUJpSbSDYk0k2", "Alice"),
        ("XrExE9yKIg1WjnnlVkGX", "Matilda"),
        ("bIHbv24MWmeRgasZH58o", "Will"),
        ("cgSgspJ2msm6clMCkdW9", "Jessica"),
        ("cjVigY5qzO86Huf0OWal", "Eric"),
        ("iP95p4xoKVk53GoZ742B", "Chris"),
        ("nPczCjzI2devNBz1zQrb", "Brian"),
        ("onwK4e9ZLuTAKqWW03F9", "Daniel"),
        ("pFZP5JQG7iQjIQuC4Bku", "Lily"),
        ("pqHfZKP75CvOlQylNhV4", "Bill"),
        ("SAz9YHcvj6GT2YYXdXww", "River"),
        ("CwhRBWXzGAHq8TQ4Fs17", "Roger"),
        ("9BWtsMINqrJLrRacOk9x", "Aria"),
    )
)

# Account voice lists, per base URL + key digest: (read at, voices)
_VOICE_CACHE: dict[str, tuple[float, list[VoiceOption]]] = {}
_VOICE_CACHE_TTL = 300.0
_VOICE_CACHE_LOCK = threading.Lock()


def elevenlabs_base_url(base_url: str | None) -> str:
    """The API host without a trailing slash or ``/v1`` (tolerates either being entered)."""
    url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if url.endswith("/v1") or url.endswith("/v2"):
        url = url[:-3]
    return url or DEFAULT_BASE_URL


#: Text-to-speech models offered when the account's model list can't be read (a key without the Models permission)
KNOWN_TTS_MODELS: tuple[str, ...] = ("eleven_multilingual_v2", "eleven_v3", "eleven_flash_v2_5", "eleven_v4")


def elevenlabs_error_text(resp: httpx.Response) -> str:
    """ElevenLabs' reason from an error reply (``detail.status`` / ``detail.message``), without the request echo."""
    try:
        detail = resp.json().get("detail")
    except Exception:
        return resp.reason_phrase or ""
    if isinstance(detail, dict):
        parts = [str(detail.get(k)) for k in ("status", "message") if detail.get(k)]
        return " — ".join(parts)[:300]
    return str(detail)[:300] if detail else (resp.reason_phrase or "")


def _output_format_for(output_path: Path) -> str:
    return "mp3_44100_128" if output_path.suffix.lower() == ".mp3" else "wav_24000"


def _fetch_account_voices(base_url: str, api_key: str, timeout: float) -> list[VoiceOption]:
    voices: list[VoiceOption] = []
    token: str | None = None
    with httpx.Client(timeout=timeout) as client:
        for _ in range(20):  # at most 2000 voices
            params: dict[str, str | int] = {"page_size": 100, "include_total_count": "false", "sort": "name"}
            if token:
                params["next_page_token"] = token
            resp = client.get(f"{base_url}/v2/voices", params=params, headers={"xi-api-key": api_key})
            resp.raise_for_status()
            body = resp.json()
            for voice in body.get("voices") or []:
                voice_id = voice.get("voice_id") if isinstance(voice, dict) else None
                if isinstance(voice_id, str) and voice_id:
                    name = str(voice.get("name") or voice_id)
                    category = voice.get("category")
                    label = name if category in (None, "premade") else f"{name} ({category})"
                    voices.append(VoiceOption(id=voice_id, label=label))
            token = body.get("next_page_token")
            if not body.get("has_more") or not token:
                break
    return voices


class ElevenLabsAudioBackend:
    """ElevenLabs synchronous speech synthesis backend."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        provider_name: str = "elevenlabs",
        http_timeout: float = 120.0,
        transport_layer: TransportLayer | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("ElevenLabs API key is required")
        self._api_key = api_key
        self._base_url = elevenlabs_base_url(base_url)
        self._model = model or DEFAULT_MODEL
        # When attached as a custom provider, bill with the real provider
        self._provider_name = provider_name
        self._http_timeout = http_timeout
        self._transport_layer = transport_layer

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[AudioCapability]:
        return {AudioCapability.TEXT_TO_SPEECH, AudioCapability.SPEECH_SPEED}

    def list_voices(self) -> list[VoiceOption]:
        """The account's voices (cloned ones included), cached for five minutes; the default voices when unreadable."""
        key = f"{self._base_url}|{hashlib.sha256(self._api_key.encode()).hexdigest()[:16]}"
        now = time.monotonic()
        with _VOICE_CACHE_LOCK:
            cached = _VOICE_CACHE.get(key)
            if cached and now - cached[0] < _VOICE_CACHE_TTL:
                return list(cached[1])
        try:
            voices = _fetch_account_voices(self._base_url, self._api_key, timeout=8.0)
        except Exception as exc:  # the list is a convenience: synthesis still works with any voice id
            logger.warning("ElevenLabs voice list unreadable, using the default voices: %s", exc)
            voices = []
        if not voices:
            voices = list(_DEFAULT_VOICES)
        with _VOICE_CACHE_LOCK:
            _VOICE_CACHE[key] = (now, voices)
        return list(voices)

    async def synthesize(self, request: AudioSynthesisRequest) -> AudioSynthesisResult:
        # language_type is DashScope's field; ElevenLabs detects the language from the text. The billed call is
        # separate from writing to disk: retries wrap the API call only.
        audio = await self._request_speech(request)
        # The provider has billed and returned; the disk write must not be interrupted by cancellation
        await run_noninterruptible_sync(request.output_path.write_bytes, audio)
        logger.info("ElevenLabs speech synthesis done: %s", request.output_path)
        return AudioSynthesisResult(
            provider=self._provider_name,
            model=self._model,
            text=request.text,
            output_path=request.output_path,
        )

    @with_retry_async(retry_if=should_retry_submit)
    async def _request_speech(self, request: AudioSynthesisRequest) -> bytes:
        payload: dict = {"text": request.text, "model_id": self._model}
        if request.speed is not None:
            payload["voice_settings"] = {"speed": min(_SPEED_MAX, max(_SPEED_MIN, float(request.speed)))}
        output_format = _output_format_for(request.output_path)
        logger.info(
            "calling %s speech synthesis model=%s voice=%s format=%s chars=%d",
            self.name,
            self._model,
            request.voice,
            output_format,
            len(request.text),
        )
        async with artifact_http_client(timeout=self._http_timeout, transport_layer=self._transport_layer) as client:
            # Synthesis is a billed POST: submit_post turns ambiguous transport errors into terminal failures
            resp = await submit_post(
                lambda: client.post(
                    f"{self._base_url}/v1/text-to-speech/{request.voice}",
                    params={"output_format": output_format},
                    json=payload,
                    headers={"xi-api-key": self._api_key, "Accept": "audio/*"},
                ),
                provider=self._provider_name,
                include_provider_reason=False,
            )
        audio = resp.content
        if not audio:
            raise RuntimeError("ElevenLabs speech synthesis returned no audio")
        return audio
