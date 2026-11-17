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


def test_eleven_model_ids_infer_the_elevenlabs_endpoint():
    from lib.custom_provider.endpoints import infer_endpoint

    assert infer_endpoint("eleven_multilingual_v2", "openai") == "elevenlabs-tts"
    assert infer_endpoint("eleven_v3", "elevenlabs") == "elevenlabs-tts"


async def test_discovery_lists_tts_models_with_the_xi_key():
    from lib.custom_provider.discovery import discover_models

    with capture_http() as router:
        route = router.get("https://api.elevenlabs.io/v1/models").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"model_id": "eleven_v3", "can_do_text_to_speech": True},
                    {"model_id": "eleven_english_sts_v2", "can_do_text_to_speech": False},
                ],
            )
        )
        found = await discover_models(discovery_format="elevenlabs", base_url="https://api.elevenlabs.io", api_key="xi")
    assert only_request(route).headers["xi-api-key"] == "xi"
    assert [(m["model_id"], m["endpoint"]) for m in found] == [("eleven_v3", "elevenlabs-tts")]


async def test_check_falls_back_to_voices_and_echoes_the_reason():
    from server.routers.custom_providers import _check_elevenlabs

    def t(key, **kw):
        return f"{key}:{kw.get('err_msg', '')}"

    denied = httpx.Response(401, json={"detail": {"status": "missing_permissions", "message": "needs models_read"}})
    with capture_http() as router:
        router.get("https://api.elevenlabs.io/v1/models").mock(return_value=denied)
        router.get("https://api.elevenlabs.io/v2/voices").mock(return_value=httpx.Response(200, json={"voices": []}))
        ok = await _check_elevenlabs("https://api.elevenlabs.io", "xi", t)
    assert ok.success

    with capture_http() as router:
        router.get("https://api.elevenlabs.io/v1/models").mock(return_value=denied)
        router.get("https://api.elevenlabs.io/v2/voices").mock(return_value=denied)
        bad = await _check_elevenlabs("https://api.elevenlabs.io", "xi", t)
    assert not bad.success
    assert "missing_permissions" in bad.message


async def test_discovery_offers_known_models_to_a_restricted_key():
    from lib.custom_provider.discovery import discover_models

    with capture_http() as router:
        router.get("https://api.elevenlabs.io/v1/models").mock(return_value=httpx.Response(401, json={}))
        found = await discover_models(discovery_format="elevenlabs", base_url=None, api_key="xi")
    assert "eleven_multilingual_v2" in [m["model_id"] for m in found]
