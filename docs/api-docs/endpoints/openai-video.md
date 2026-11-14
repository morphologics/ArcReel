# openai-video

- Protocol: [OpenAI video generation](https://developers.openai.com/api/docs/guides/video-generation)
- Billing: [API pricing](https://developers.openai.com/api/docs/pricing)
- Code: `lib/custom_provider/endpoints.py::ENDPOINT_REGISTRY["openai-video"]`, `lib/backends/video_backends/openai.py::OpenAIVideoBackend`
- Task status: official Sora only emits the status strings listed in the protocol docs, but proxy gateways forwarding non-Sora models pass through the underlying vendor's spelling, so statuses go through the shared normalizer `packages/arcreel-market-core/src/arcreel_market_core/video_backend_contract.py::normalize_provider_status`
