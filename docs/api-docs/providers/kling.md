# kling

Kling image and video media provider.

- Main entry: [Kling AI developer documentation](https://app.klingai.com/global/dev/document-api)
- APIs and capabilities: the Image Generation, Text to Video, Image to Video and Multi-image to Video APIs in the main entry
- Model notes: [Kling Video 3 model guide](https://app.klingai.com/cn/quickstart/klingai-video-3-model-user-guide)
- Error codes: [Kling AI Error Codes](https://kling.ai/document-api/api/get-started/error-codes)
- Billing: [Video billing (China site)](https://klingai.com/document-api/pricing/base/video), [Image billing (China site)](https://klingai.com/document-api/pricing/base/image), [Resource packages](https://app.klingai.com/global/dev/document-api/productBilling/prePaidResourcePackage); the factory snapshot price list is in `lib/catalog/snapshot/catalog.json`
- Code: `lib/config/registry.py::PROVIDER_REGISTRY["kling"]`, `lib/backends/image_backends/kling.py::KlingImageBackend`, `lib/backends/video_backends/kling.py::KlingVideoBackend`
