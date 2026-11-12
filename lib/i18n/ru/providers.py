"""Русские переводы метаданных реестра провайдеров."""

MESSAGES: dict[str, str] = {
    # Provider display names
    "provider_name_gemini-aistudio": "AI Studio",
    "provider_name_gemini-vertex": "Vertex AI",
    "provider_name_ark": "Volcengine Ark",
    "provider_name_ark-agent-plan": "Volcengine Ark Agent Plan",
    "provider_name_grok": "Grok",
    "provider_name_openai": "OpenAI",
    "provider_name_vidu": "Vidu",
    "provider_name_dashscope": "Alibaba Model Studio",
    "provider_name_minimax": "MiniMax",
    "provider_name_kling": "Kling",
    "provider_name_agnes": "Agnes",
    # 失败文案里目录查不到的供应商（已被删除）的泛称
    "deleted_provider_display_name": "Удалённый провайдер",
    "deleted_agent_provider_display_name": "Удалённый провайдер агента",
    # Provider descriptions
    "provider_desc_gemini-aistudio": "Google AI Studio предоставляет модели Gemini для генерации изображений, видео и речи — подходит для быстрого прототипирования и личных проектов.",
    "provider_desc_gemini-vertex": "Корпоративная платформа Google Cloud Vertex AI с моделями Gemini и Imagen, повышенными квотами и генерацией аудио.",
    "provider_desc_ark": "AI-платформа ByteDance Volcengine Ark: генерация видео Seedance и изображений Seedream, с поддержкой аудио и управления seed.",
    "provider_desc_ark-agent-plan": "Volcengine Ark Agent Plan объединяет Doubao и другие крупные модели для генерации текста, изображений и видео.",
    "provider_desc_grok": "Модели xAI Grok для генерации видео и изображений.",
    "provider_desc_openai": "Платформа OpenAI: текст GPT-5.4 и генерация изображений GPT Image.",
    "provider_desc_vidu": "Видеоплатформа Shengshu Vidu: текст в видео, изображение в видео, первый и последний кадр, референс в видео и референс в изображение. Только изображения и видео.",
    "provider_desc_dashscope": "Мультимодальная платформа Alibaba Cloud Model Studio (DashScope): текст Qwen, изображения Qwen-Image / Wan и видео HappyHorse / Wan (включая референс в видео).",
    "provider_desc_minimax": "Мультимодальная платформа MiniMax (Hailuo) для генерации текста, изображений, видео и речи. По умолчанию подключается к китайскому сайту; для доступа из-за рубежа укажите в base_url международный сайт.",
    "provider_desc_kling": "Платформа Kuaishou Kling для генерации видео и изображений. Аутентификация по API Key работает со всеми моделями; Access Key + Secret Key (JWT) поддерживает только модели версии 3.0 и ниже. Выберите один способ — если заданы оба, приоритет у API Key.",
    "provider_desc_agnes": "Мультимодальная платформа Agnes (в стиле OpenAI) с аутентификацией по Bearer API-ключу; сейчас поддерживает генерацию изображений, текста и видео.",
    # Agent preset notes (lib/agent/agent_provider_catalog.py)
    "preset_notes_deepseek": "Официальный Anthropic-совместимый эндпоинт DeepSeek; нужен ключ с префиксом sk-.",
    "preset_notes_xiaomi_mimo": "Xiaomi MiMo принимает только известные имена моделей; публичного списка моделей нет.",
    "preset_notes_ark_coding_plan": "Volcengine Ark Coding Plan; эндпоинта списка моделей нет — выберите рекомендуемую модель, включённую в консоли Ark.",
    "preset_notes_ark_agent_plan": "Volcengine Ark Agent Plan; эндпоинта списка моделей нет — выберите рекомендуемую модель, входящую в ваш тариф.",
}
