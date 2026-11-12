import type enEvents from '../en/events';

// Project change notification copy. `label.*` mirrors the stable `label_key`
// carried by each change event (see lib/i18n/*/events.py); the remaining keys
// build the sentence around it. Group sentences only render for counts above
// one, so `entity.*` and `noun.*` use plural wording.
// (ru: `entity.*` / `noun.*` are genitive plural so they read as "Добавлено персонажей: 3".)
export default {
  "label.grid": 'Сетка раскадровки «{{id}}»',
  "label.grid_split": 'Разбивка сетки раскадровки «{{id}}»',
  "label.voice_sample": 'Образец голоса для «{{id}}»',
  "label.asset_image_character": 'Изображение ассета персонажа «{{id}}»',
  "label.asset_image_scene": 'Изображение ассета сцены «{{id}}»',
  "label.asset_image_prop": 'Изображение ассета реквизита «{{id}}»',
  "label.asset_image_product": 'Изображение ассета товара «{{id}}»',
  "label.asset_image_character_derivative": 'Изображение ассета варианта персонажа «{{id}}»',
  "label.skeleton_segments": 'Сегмент «{{id}}»',
  "label.skeleton_scenes": 'Сцена «{{id}}»',
  "label.skeleton_shots": 'Кадр «{{id}}»',
  "label.skeleton_video_units": 'Видеоблок «{{id}}»',
  "label.narration_audio": 'Озвучка «{{id}}»',
  "label.named_entity_character": 'Персонаж «{{id}}»',
  "label.named_entity_scene": 'Сцена «{{id}}»',
  "label.named_entity_prop": 'Реквизит «{{id}}»',
  "label.named_entity_product": 'Товар «{{id}}»',
  "label.named_entity_character_derivative": 'Вариант персонажа «{{id}}»',
  "label.character_reference_audio": 'Референсное аудио персонажа «{{id}}»',
  "label.project_settings": "Настройки проекта",
  "label.overview": "Обзор проекта",
  "label.episode": "{{episode_name}}",
  "label.draft_normalized_script": "Нормализованный сценарий для {{episode_name}}",
  "label.draft_segment_splitting": "Разбивка на сегменты для {{episode_name}}",
  "label.draft_script_plan": "Черновик плана сценария для {{episode_name}}",
  "label.draft_prompt_authoring": "Черновик промптов для {{episode_name}}",

  "entity.project": "проектов",
  "entity.character": "персонажей",
  "entity.scene": "сцен",
  "entity.prop": "реквизита",
  "entity.product": "товаров",
  "entity.segment": "сегментов",
  "entity.drama_scene": "сцен",
  "entity.shot": "кадров",
  "entity.reference_unit": "видеоблоков",
  "entity.episode": "эпизодов",
  "entity.overview": "обзоров проекта",
  "entity.draft": "результатов плана сценария",
  "entity.grid": "сеток раскадровки",
  // Task terminal states are refresh signals (important=false) and never reach
  // notification copy; this entry only keeps the mapping exhaustive.
  "entity.task": "задач",
  "entity.fallback": "элементов",

  "noun.storyboard_image": "изображений раскадровки",
  "noun.video": "видео",
  "noun.grid": "сеток раскадровки",
  "noun.narration_audio": "озвучек",
  "noun.voice_sample": "образцов голоса",

  "single.storyboard_ready": "Изображение раскадровки для {{label}} готово",
  "single.video_ready": "Видео для {{label}} готово",
  "single.generated": "{{label}}: готово",
  "single.completed": "{{label}}: завершено",
  "single.created": "{{label}}: создано",
  "single.deleted": "{{label}}: удалено",
  "single.updated": "{{label}}: обновлено",

  "deferred.storyboard_ready":
    "ИИ только что сгенерировал изображение раскадровки для {{label}}. Нажмите, чтобы посмотреть",
  "deferred.video_ready":
    "ИИ только что сгенерировал видео для {{label}}. Нажмите, чтобы посмотреть",
  "deferred.generated": "{{label}}: готово",
  "deferred.completed": "{{label}}: завершено",
  "deferred.created": "ИИ только что добавил: {{label}}. Нажмите, чтобы посмотреть",
  "deferred.deleted": "ИИ только что удалил: {{label}}. Нажмите, чтобы посмотреть",
  "deferred.updated": "ИИ только что обновил: {{label}}. Нажмите, чтобы посмотреть",

  "group.generated": "Сгенерировано {{entity}}: {{count}} ({{summary}})",
  "group.created": "Добавлено {{entity}}: {{count}} ({{summary}})",
  "group.deleted": "Удалено {{entity}}: {{count}} ({{summary}})",
  "group.updated": "Обновлено {{entity}}: {{count}} ({{summary}})",

  "group_deferred.generated":
    "ИИ только что сгенерировал {{entity}}: {{count}} ({{summary}}). Нажмите, чтобы посмотреть",
  "group_deferred.created":
    "ИИ только что добавил {{entity}}: {{count}} ({{summary}}). Нажмите, чтобы посмотреть",
  "group_deferred.deleted":
    "ИИ только что удалил {{entity}}: {{count}} ({{summary}}). Нажмите, чтобы посмотреть",
  "group_deferred.updated":
    "ИИ только что обновил {{entity}}: {{count}} ({{summary}}). Нажмите, чтобы посмотреть",

  "list.separator": ", ",
  "list.more_suffix": " и другие",
} satisfies Record<keyof typeof enEvents, string>;
