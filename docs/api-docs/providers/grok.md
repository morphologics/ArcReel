# grok

xAI Grok / Imagine media provider.

- Main entry: [xAI API documentation](https://docs.x.ai/)
- APIs and capabilities: [Generate text](https://docs.x.ai/developers/model-capabilities/text/generate-text), [Chat API](https://docs.x.ai/developers/rest-api-reference/inference/chat), [Image generation](https://docs.x.ai/developers/model-capabilities/images/generation), [Multi-image editing](https://docs.x.ai/developers/model-capabilities/images/multi-image-editing), [Video generation](https://docs.x.ai/developers/model-capabilities/video/generation), [Reference-to-video](https://docs.x.ai/developers/model-capabilities/video/reference-to-video)
- Models and billing: [Models and pricing](https://docs.x.ai/developers/models); per-second video rates, tiered by resolution, are on each model page: [grok-imagine-video](https://docs.x.ai/developers/models/grok-imagine-video), [grok-imagine-video-1.5](https://docs.x.ai/developers/models/grok-imagine-video-1.5), [grok-imagine-video-1.5-lite](https://docs.x.ai/developers/models/grok-imagine-video-1.5-lite)
- Code: `lib/config/registry.py::PROVIDER_REGISTRY["grok"]`, `lib/backends/text_backends/grok.py::GrokTextBackend`, `lib/backends/image_backends/grok.py::GrokImageBackend`, `lib/backends/video_backends/grok.py::GrokVideoBackend`
