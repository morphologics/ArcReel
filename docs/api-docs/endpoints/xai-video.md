# xai-video

- Protocol: [Videos REST reference](https://docs.x.ai/developers/rest-api-reference/inference/videos), [Video generation](https://docs.x.ai/developers/model-capabilities/video/generation), [Reference-to-video](https://docs.x.ai/developers/model-capabilities/video/reference-to-video)
- Models and billing: [Models and pricing](https://docs.x.ai/developers/models)
- Code: `lib/custom_provider/builtin_endpoints/xai-video.json`, `lib/custom_provider/declarative_backend.py::DeclarativeVideoBackend`
- Task status: polled statuses are `pending`, `done`, `failed`, `expired`; `done` is not in the shared synonym table, so the definition's `status_map` maps it to success
- Relationship to the built-in `grok` provider: the latter uses gRPC via xai-sdk (`lib/backends/video_backends/grok.py::GrokVideoBackend`); this endpoint lets custom providers call it over REST
