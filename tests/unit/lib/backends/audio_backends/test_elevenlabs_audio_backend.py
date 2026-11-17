"""ElevenLabsAudioBackend (YouReel): one synthesis per request, the account's voice list with a fallback."""

from __future__ import annotations

from pathlib import Path

import httpx

from lib.backends.audio_backends import AudioCapability, AudioSynthesisRequest
from lib.backends.audio_backends import elevenlabs
from lib.backends.audio_backends.elevenlabs import ElevenLabsAudioBackend
from tests.http_capture import capture_http, only_request, request_json

_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/voice123"


async def test_synthesize_writes_the_audio_and_clamps_speed(tmp_path: Path):
    with capture_http() as router:
        route = router.post(_TTS_URL).mock(return_value=httpx.Response(200, content=b"RIFFwavbytes"))
        # a base URL entered with /v1 still reaches the right path
        b = ElevenLabsAudioBackend(api_key="xi", base_url="https://api.elevenlabs.io/v1/", model="eleven_v3")
        out = tmp_path / "o.wav"
        result = await b.synthesize(AudioSynthesisRequest(text="Hello", output_path=out, voice="voice123", speed=1.5))

    submitted = only_request(route)
    assert submitted.headers["xi-api-key"] == "xi"
    assert submitted.url.params["output_format"] == "wav_24000"
    assert request_json(submitted) == {"text": "Hello", "model_id": "eleven_v3", "voice_settings": {"speed": 1.2}}
    assert out.read_bytes() == b"RIFFwavbytes"
    assert result.provider == "elevenlabs"


async def test_mp3_output_asks_for_mp3(tmp_path: Path):
    with capture_http() as router:
        route = router.post(_TTS_URL).mock(return_value=httpx.Response(200, content=b"ID3"))
        b = ElevenLabsAudioBackend(api_key="xi")
        await b.synthesize(AudioSynthesisRequest(text="Hi", output_path=tmp_path / "o.mp3", voice="voice123"))
    submitted = only_request(route)
    assert submitted.url.params["output_format"] == "mp3_44100_128"
    assert "voice_settings" not in request_json(submitted)


def test_lists_account_voices_and_falls_back(monkeypatch):
    elevenlabs._VOICE_CACHE.clear()
    with capture_http() as router:
        router.get("https://api.elevenlabs.io/v2/voices").mock(
            return_value=httpx.Response(
                200,
                json={
                    "voices": [
                        {"voice_id": "a1", "name": "Narrator", "category": "cloned"},
                        {"voice_id": "b2", "name": "Sarah", "category": "premade"},
                    ],
                    "has_more": False,
                    "next_page_token": None,
                },
            )
        )
        b = ElevenLabsAudioBackend(api_key="xi-ok", provider_name="el")
        voices = b.list_voices()
    assert [(v.id, v.label) for v in voices] == [("a1", "Narrator (cloned)"), ("b2", "Sarah")]
    assert AudioCapability.SPEECH_SPEED in b.capabilities
    assert b.name == "el"

    elevenlabs._VOICE_CACHE.clear()
    with capture_http() as router:
        router.get("https://api.elevenlabs.io/v2/voices").mock(return_value=httpx.Response(401, json={}))
        fallback = ElevenLabsAudioBackend(api_key="xi-bad").list_voices()
    assert "JBFqnCBsd6RMkjVDRZzb" in [v.id for v in fallback]
