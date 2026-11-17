# elevenlabs-tts

YouReel addition (2026-11-17).

- Protocol: [Create speech](https://elevenlabs.io/docs/api-reference/text-to-speech/convert) (`POST /v1/text-to-speech/{voice_id}`, header `xi-api-key`, `output_format` query)
- Voices: [List voices](https://elevenlabs.io/docs/api-reference/voices/search) (`GET /v2/voices`, `next_page_token` pagination)
- Models and limits: [Models](https://elevenlabs.io/docs/models)
- Code: `lib/custom_provider/endpoints.py::ENDPOINT_REGISTRY["elevenlabs-tts"]`, `lib/backends/audio_backends/elevenlabs.py::ElevenLabsAudioBackend`
