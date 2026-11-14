# gemini-aistudio

Google Gemini Developer API / AI Studio media provider.

- Main entry: [Gemini API documentation](https://ai.google.dev/gemini-api/docs)
- APIs and capabilities: [generateContent API](https://ai.google.dev/api/generate-content), [Image generation](https://ai.google.dev/gemini-api/docs/image-generation), [Video generation with Veo](https://ai.google.dev/gemini-api/docs/video), [Speech generation](https://ai.google.dev/gemini-api/docs/speech-generation)
- Billing: [Gemini Developer API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- Code: `lib/config/registry.py::PROVIDER_REGISTRY["gemini-aistudio"]`, `lib/backends/text_backends/gemini.py::GeminiTextBackend`, `lib/backends/image_backends/gemini.py::GeminiImageBackend`, `lib/backends/video_backends/gemini.py::GeminiVideoBackend`, `lib/backends/audio_backends/gemini.py::GeminiAudioBackend`
