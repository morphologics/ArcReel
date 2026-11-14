# gemini-vertex

Gemini, Imagen and Veo media provider on Google Vertex AI; it shares some model docs with AI Studio, but authentication, endpoints and billing are separate.

- Main entry: [Generative AI on Vertex AI](https://cloud.google.com/vertex-ai/generative-ai/docs)
- APIs and capabilities: [Google Gen AI SDK `generate_content`](https://googleapis.github.io/python-genai/genai.html#genai.models.Models.generate_content), [Generate images with Gemini (Vertex AI mode)](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/image-generation), [Veo video generation API](https://cloud.google.com/vertex-ai/generative-ai/docs/model-reference/veo-video-generation), [Gemini 3.8 Flash TTS](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash-tts)
- Billing: [Vertex AI generative AI pricing](https://cloud.google.com/vertex-ai/generative-ai/pricing)
- Code: `lib/config/registry.py::PROVIDER_REGISTRY["gemini-vertex"]`, `lib/backends/text_backends/gemini.py::GeminiTextBackend`, `lib/backends/image_backends/gemini.py::GeminiImageBackend`, `lib/backends/video_backends/gemini.py::GeminiVideoBackend`, `lib/backends/audio_backends/gemini.py::GeminiAudioBackend`
