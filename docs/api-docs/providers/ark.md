# ark

Volcengine Ark standard `/api/v3` media provider.

- Main entry: [Volcengine Ark documentation](https://www.volcengine.com/docs/82379/?lang=zh)
- APIs and capabilities: [Chat API](https://api.volcengine.com/api-docs/view?action=ChatCompletions&serviceCode=ark&version=2024-01-01), [Image generation API](https://www.volcengine.com/docs/82379/1666946?lang=zh), [Model list](https://www.volcengine.com/docs/82379/1330310?lang=zh), [Create a video generation task](https://www.volcengine.com/docs/82379/1520757?lang=zh)
- Billing: [Volcengine Ark pricing](https://www.volcengine.com/pricing?product=ark_bd&tab=1), [Model prices](https://www.volcengine.com/docs/82379/1544106); the factory snapshot price list is in `lib/catalog/snapshot/catalog.json`. Seedance video is billed by video tokens: settlement uses the reported `usage.completion_tokens`, and the pre-submit estimate is converted using the formula published on the model pricing page, (input video seconds + output seconds) × width × height × 24 / 1024 (`ArkVideoBackend.estimate_video_tokens`)
- Deprecation: [Model deprecation notice](https://docs.volcengine.com/docs/ark/model-deprecation-notice?lang=zh)
- Code: `lib/config/registry.py::PROVIDER_REGISTRY["ark"]`, `lib/backends/text_backends/ark.py::ArkTextBackend`, `lib/backends/image_backends/ark.py::ArkImageBackend`, `lib/backends/video_backends/ark.py::ArkVideoBackend` (metering: `ArkVideoBackend.metered_usage`)
