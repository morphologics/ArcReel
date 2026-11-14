# agnes

Agnes OpenAI-compatible text, image and video media provider.

- Main entry: [Agnes AI official model catalog](https://github.com/AgnesAI-Labs/AgnesAI-Models)
- APIs and capabilities: [Model catalog](https://github.com/AgnesAI-Labs/AgnesAI-Models/blob/main/MODEL_CATALOG.md) and the official model doc links in it
- Billing: [Pricing](https://wiki.agnes-ai.com/en/docs/pricing.md) (official pricing page; promotional prices are as stated in platform announcements and account bills)
- Rate limits and quotas: [Current rate limits](https://github.com/AgnesAI-Labs/AgnesAI-Models/blob/main/MODEL_CATALOG.md#current-rate-limits), [Current subscription quotas](https://github.com/AgnesAI-Labs/AgnesAI-Models/blob/main/MODEL_CATALOG.md#current-subscription-quotas)
- Code: `lib/config/registry.py::PROVIDER_REGISTRY["agnes"]`, `lib/backends/text_backends/agnes.py::AgnesTextBackend`, `lib/backends/image_backends/agnes.py::AgnesImageBackend`, `lib/backends/video_backends/agnes.py::AgnesVideoBackend`, `lib/backends/agnes_shared.py::agnes_base_url`
