# openai

OpenAI official text and image media provider. Text is called via the Responses API. Video and TTS reuse the OpenAI protocol only through custom endpoints (see [openai-video](../endpoints/openai-video.md), [openai-tts](../endpoints/openai-tts.md)); they are not built-in lanes of this provider.

- Main entry: [OpenAI API documentation](https://developers.openai.com/api/docs)
- APIs and capabilities: [Models](https://developers.openai.com/api/docs/models), [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create), [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [Image generation](https://developers.openai.com/api/docs/guides/image-generation), [Text to speech](https://developers.openai.com/api/docs/guides/text-to-speech)
- Billing: [API pricing](https://developers.openai.com/api/docs/pricing)
- Code: `lib/config/registry.py::PROVIDER_REGISTRY["openai"]`, `lib/backends/text_backends/openai_responses.py::OpenAIResponsesTextBackend`, `lib/backends/image_backends/openai.py::OpenAIImageBackend`
