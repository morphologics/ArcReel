# vidu

Vidu image and video media provider.

- Main entry: [Vidu open platform documentation](https://platform.vidu.cn/docs)
- APIs and capabilities: [Model map](https://platform.vidu.cn/docs/overview/model-map.md) (look up capabilities and parameters by model), [Feature list](https://platform.vidu.cn/docs/overview/function-list.md) (feature categories, without model IDs), [Image generation](https://platform.vidu.cn/docs/reference-to-image), [Text-to-video](https://platform.vidu.cn/docs/text2video), [Image-to-video](https://platform.vidu.cn/docs/img2video), [Start/end-frame-to-video](https://platform.vidu.cn/docs/start-end2video), [Reference-to-video](https://platform.vidu.cn/docs/reference2video)
- Billing: [Pricing (credit consumption details)](https://platform.vidu.cn/docs/overview/pricing); the factory snapshot price list is in `lib/catalog/snapshot/catalog.json`. Video is priced from the price list (per output second or per clip, tiered by resolution and by whether it is reference-to-video), no longer delegated to the `credits` in the response
- Code: `lib/config/registry.py::PROVIDER_REGISTRY["vidu"]`, `lib/backends/image_backends/vidu.py::ViduImageBackend`, `lib/backends/video_backends/vidu.py::ViduVideoBackend` (metering: `ViduVideoBackend.metered_usage`)
