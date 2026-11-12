"""Сообщения проверки данных и диагностики архивов (русский)."""

MESSAGES = {
    # ---- passthrough ----
    "val_literal": "{text}",
    # ---- generic field shape ----
    "val_missing_field": "Отсутствует обязательное поле: {field}",
    "val_missing_field_at": "{prefix}: отсутствует обязательное поле {field}",
    "val_field_type_string": "Ошибка типа поля: {field} должно быть строкой",
    "val_field_type_bool": "Ошибка типа поля: {field} должно быть логическим значением",
    "val_field_type_integer": "Ошибка типа поля: {field} должно быть целым числом",
    "val_field_type_number": "Ошибка типа поля: {field} должно быть числом",
    "val_field_out_of_range": "Значение {value} поля {field} вне допустимого диапазона; оно должно быть от {min} до {max}",
    "val_field_must_be_string": "{field} должно быть строкой",
    "val_field_must_be_string_typed": "{field} должно быть строкой, получено {actual}",
    "val_field_must_be_array": "{field} должно быть массивом",
    "val_field_must_be_nonempty_array": "{field} должно быть непустым массивом",
    "val_field_must_be_nonempty_string": "{field} должно быть непустой строкой",
    "val_field_must_be_object": "{field} должно быть объектом",
    "val_field_invalid": "{field} недопустимо: {detail}",
    "val_ledger_source_file_not_relative": "source_file должен быть POSIX-путём относительно проекта",
    "val_ledger_source_file_escapes": "source_file не должен быть абсолютным или содержать ..",
    "val_ledger_start_after_end": "start не должен быть больше end",
    "val_field_bad_timestamp": "{field} не является допустимой меткой времени ISO8601: {value}",
    "val_item_must_be_object": "{prefix}: должно быть объектом",
    "val_item_format_object": "{prefix}: неверный формат данных, ожидается объект",
    # ---- path references ----
    "val_path_empty": "{field}: путь не должен быть пустым",
    "val_path_traversal": "{field}: путь ссылки выходит за пределы проекта: {path}",
    "val_path_outside_dir": "{field}: путь ссылки должен находиться в {dir}/: {path}",
    "val_path_missing": "{field}: файл по ссылке не существует: {path}",
    "val_path_must_be_relative": "{field} должен быть путём относительно проекта: {path}",
    # ---- project-level fields ----
    "val_content_mode_invalid": "Недопустимый content_mode: «{value}», допустимые значения: {allowed}",
    "val_source_kind_invalid": "{prefix}: недопустимый source_kind «{value}», допустимые значения: {allowed}",
    "val_generation_mode_invalid": "Недопустимый generation_mode: «{value}», допустимые значения: {allowed}",
    "val_deprecated_clues": (
        "project.json содержит устаревшее поле clues; дождитесь автоматической миграции или перезапустите сервис"
    ),
    "val_deprecated_field_removable": "{field} устарело (теперь вычисляется при чтении) и может быть безопасно удалено",
    "val_cannot_load_project_json": "Не удалось загрузить project.json: {path}",
    "val_cannot_load_script": "Не удалось загрузить файл сценария: {path}",
    "val_unrecognized_entry": "Обнаружен лишний неизвестный файл/каталог: {name}",
    "val_novel_must_be_object": "Поле novel должно быть объектом",
    # ---- episode entries and ledger ----
    "val_ledger_status_type": "{prefix}: ledger_status должен быть строкой, получено: {value}",
    "val_source_origin_invalid": "{prefix}: недопустимый source_origin «{value}», допустимые значения: {allowed}",
    "val_source_range_requires_whole_source": "{prefix}: source_range может быть только у эпизода, вырезанного из полного исходного текста (source_origin whole_source)",
    "val_whole_source_file_invalid": "whole_source_files[{index}] должен быть объектом, у которого source_file указывает на файл .txt или .md непосредственно в source/",
    "val_episode_missing_num_at": "{prefix}: отсутствует обязательное поле episode (целое число)",
    "val_episode_missing_title_at": "{prefix}: отсутствует обязательное поле title (строка, может быть пустой)",
    "val_episode_missing_num": "Отсутствует обязательное поле: episode (целое число)",
    # ---- ad / short-film projects ----
    "val_ad_only_field": "{field} доступно только для проектов рекламы/короткометражек (content_mode=ad)",
    "val_ad_missing_target_duration": (
        "Отсутствует обязательное поле: target_duration (целевая общая длительность в секундах для проектов рекламы/короткометражек)"
    ),
    "val_ad_target_duration_invalid": "Недопустимый target_duration: {value}, должно быть положительное целое число секунд",
    "val_ad_no_default_duration": (
        "Проекты рекламы/короткометражек не содержат default_duration "
        "(длительность кадров распределяется по кадрам в рамках target_duration)"
    ),
    "val_ad_no_episode_target_duration": (
        "Проекты рекламы/короткометражек не содержат episode_target_duration "
        "(общая длительность эпизода распределяется в рамках target_duration)"
    ),
    "val_ad_no_grid_storyboard": "Проекты рекламы/короткометражек не поддерживают сетки раскадровки (grid_storyboard)",
    "val_ad_episodes_single": "У проекта рекламы/короткометражки всегда должна быть ровно одна запись эпизода (эпизод 1)",
    "val_ad_shots_missing": "В рекламном сценарии отсутствует массив shots",
    "val_ad_duration_drift": (
        "Общая длительность сценария {total}s отклоняется от target_duration {target}s на {delta:.0%}, "
        "что выше порога наблюдения {threshold:.0%} (только для информации, сохранение не блокируется)"
    ),
    # ---- asset catalogs ----
    "val_asset_format_object": "{asset_type} «{name}»: неверный формат данных, ожидается объект",
    "val_asset_field_must_be_object": "{asset_type} «{name}».{field} должно быть объектом",
    "val_asset_field_must_be_string": "{asset_type} «{name}».{field} должно быть строкой, получено {actual}",
    "val_asset_field_bad_timestamp": "{asset_type} «{name}».{field} не является допустимой меткой времени ISO8601: {value}",
    "val_asset_field_must_be_string_list": "{asset_type} «{name}».{field} должно быть списком строк, получено {actual}",
    "val_asset_field_item_must_be_string": "{asset_type} «{name}».{field}[{index}] должно быть строкой, получено {actual}",
    "val_asset_name_duplicate": (
        "Повторяющееся имя ассета проекта: {duplicate_type} «{duplicate_name}» конфликтует с "
        "{first_type} «{first_name}» после strip и нормализации Unicode NFC"
    ),
    # ---- item-level references ----
    "val_refs_unregistered": "{prefix}: {field} ссылается на записи {asset_type}, которых нет в project.json: {names}",
    "val_missing_defaults_empty_array": "{prefix}: {field} отсутствует, по умолчанию используется пустой массив",
    # ---- generic item checks ----
    "val_id_format": "{prefix}: неверный формат {field} «{value}», ожидается E{{n}}S{{nn}}",
    "val_missing_duration_default": "{prefix}: duration_seconds отсутствует, по умолчанию {default}",
    "val_duration_invalid": "{prefix}: недопустимый duration_seconds «{value}», должно быть положительное целое число",
    # ---- drama utterances ----
    "val_utterance_must_be_object": "{prefix} должно быть объектом",
    "val_utterance_kind_invalid": "{prefix}: kind должен быть dialogue или voiceover",
    "val_utterance_text_invalid": "{prefix}: text должен быть непустой строкой",
    "val_utterance_speaker_type": "{prefix}: speaker должен быть строкой или null",
    "val_utterance_dialogue_speaker": "{prefix}: у dialogue должен быть непустой speaker",
    "val_utterance_voiceover_speaker": "{prefix}: у voiceover не должно быть speaker",
    "val_scene_speech_overflow": (
        "{prefix}: оценочная длительность речи {spoken:.1f}s превышает длительность сцены {duration}s более чем на "
        "{tolerance:.0%} (верхний предел допуска {budget:.1f}s); длинный диалог может не уместиться или звучать торопливо "
        "(только для информации, сохранение не блокируется)"
    ),
    # ---- ad shots ----
    "val_shot_duration_missing_zero": "{prefix}: duration_seconds отсутствует, в общей длительности учитывается как 0",
    "val_shot_duration_out_of_range": (
        "{prefix}: недопустимый duration_seconds «{value}», режим reference_video требует целое число "
        "от {low} до {high}"
    ),
    "val_shot_missing_voiceover_text": (
        "{prefix}: отсутствует обязательное поле voiceover_text (закадровый текст, может быть пустой строкой)"
    ),
    # ---- reference-video units ----
    "val_unit_id_missing": "{prefix}: отсутствует unit_id",
    "val_unit_id_missing_required": "{prefix}: отсутствует обязательное поле unit_id",
    "val_unit_id_duplicate": "{prefix}: повторяющийся unit_id «{value}»",
    "val_video_units_missing": "В сценарии reference_video отсутствует массив video_units",
    "val_unit_duration_range": "{prefix}: duration_seconds должен быть целым числом от {low} до {high}",
    # ---- skeleton / route mismatch ----
    "val_skeleton_noun_segments": "сегменты",
    "val_skeleton_noun_scenes": "сцены",
    "val_skeleton_noun_shots": "кадры",
    "val_skeleton_noun_video_units": "видеоблоки",
    "val_route_reference_video": "референс в видео (reference_video)",
    "val_route_storyboard": "раскадровка в видео (storyboard)",
    "val_skeleton_mismatch_reference_known": (
        "Каркас сценария не соответствует режиму генерации проекта: режим — {route}, для него нужен каркас "
        "{expected} ({expected_noun}), а этот сценарий использует {actual} ({actual_noun}). "
        "Вызовите generate_script_plan, чтобы заново разбить этот эпизод, затем перегенерируйте сценарий. "
        "Сценарий по-прежнему можно просматривать, редактировать и экспортировать."
    ),
    "val_skeleton_mismatch_reference_none": (
        "Каркас сценария не соответствует режиму генерации проекта: режим — {route}, для него нужен каркас "
        "{expected} ({expected_noun}), а в этом сценарии вообще нет массива каркаса. "
        "Вызовите generate_script_plan, чтобы заново разбить этот эпизод, затем перегенерируйте сценарий. "
        "Сценарий по-прежнему можно просматривать, редактировать и экспортировать."
    ),
    "val_skeleton_mismatch_storyboard_known": (
        "Каркас сценария не соответствует режиму генерации проекта: режим — {route}, для него нужен каркас "
        "{expected} ({expected_noun}), а этот сценарий использует {actual} ({actual_noun}). "
        "Заново запустите разбивку на эпизоды (script_plan), чтобы разбить этот эпизод, затем перегенерируйте сценарий. "
        "Сценарий по-прежнему можно просматривать, редактировать и экспортировать."
    ),
    "val_skeleton_mismatch_storyboard_none": (
        "Каркас сценария не соответствует режиму генерации проекта: режим — {route}, для него нужен каркас "
        "{expected} ({expected_noun}), а в этом сценарии вообще нет массива каркаса. "
        "Заново запустите разбивку на эпизоды (script_plan), чтобы разбить этот эпизод, затем перегенерируйте сценарий. "
        "Сценарий по-прежнему можно просматривать, редактировать и экспортировать."
    ),
    # ---- reference-video duration consolidation migration ----
    "val_unit_duration_clamped": (
        "длительность блока {unit_id} {target}s вне разумного диапазона {low}-{high}s; ограничена до {clamped}s"
    ),
    "val_unit_duration_slotted": (
        "длительность блока {unit_id} {duration}s не входит в варианты длительности модели ({durations}); "
        "округлена до {slot}s"
    ),
    # ---- archive repair and import/export diagnostics ----
    "arch_source_encoding_unconverted": (
        "Не удалось определить кодировку исходного файла, он не преобразован в UTF-8: source/{name} "
        "(планирование эпизодов не сможет прочитать этот файл)"
    ),
    "arch_non_standard_entry_excluded": "Нестандартный каталог/файл верхнего уровня «{entry}» исключён из экспорта",
    "arch_invalid_project_json": "Не удалось разобрать {file}: {path}",
    "arch_version_history_malformed": "{location}: история версий имеет неожиданный формат",
    "arch_version_snapshot_path_unmanaged": "{location}: путь снимка в записи версии находится вне каталога версий своего типа ресурса",
    "arch_script_file_repaired": "{location}: автоматически исправлено на {path}",
    "arch_missing_script_file_pending": "{location}: сценарий ещё не сгенерирован: {path}",
    "arch_missing_script_file": "{location}: файл по ссылке не существует: {path}",
    "arch_invalid_script_json": "Не удалось разобрать файл сценария: {path}",
    "arch_deprecated_source_file_removed": "Поле novel.source_file устарело и удалено",
    "arch_deprecated_field_removed": "{field} устарело (теперь вычисляется при чтении) и удалено",
    "arch_deprecated_clue_field_removed": (
        "{items_key}[{index}]: устаревшее поле {field} удалено (используйте scenes/props)"
    ),
    "arch_missing_field_filled": "{items_key}[{index}]: заполнено отсутствующее поле {field}",
    "arch_unit_unresolved_mentions": (
        "video_units[{index}]: в тексте упоминаются имена ассетов, которых нет в project.json: {names}; "
        "для них не будут созданы референсные изображения"
    ),
    "arch_generated_assets_defaults": "{label}[{index}].generated_assets: заполнены поля по умолчанию {fields}",
    "arch_missing_generated_assets": "{label}[{index}]: заполнено отсутствующее поле generated_assets",
    "arch_invalid_generated_assets": (
        "{label}[{index}]: generated_assets имеет неверный формат ({actual}) и сброшено к структуре по умолчанию"
    ),
    "arch_placeholder_character_added": "Автоматически добавлено отсутствующее описание персонажа: {name}",
    "arch_placeholder_asset_added": "Автоматически добавлено отсутствующее описание {asset_type}: {name}",
    "arch_canonical_path_normalized": "{location}: нормализовано в {path}",
    "arch_current_asset_materialized": "{location}: текущий файл {target} восстановлен из {source}",
    "arch_current_asset_restored_from_version": "{location}: текущий файл {target} восстановлен из {source}",
    # ---- archive import errors ----
    "arch_invalid_conflict_policy": "Недопустимая политика конфликтов",
    "arch_conflict_policy_unsupported": "conflict_policy поддерживает только prompt, rename или overwrite; получено: {value}",
    "arch_import_validation_failed": "Пакет импорта не прошёл проверку",
    "arch_artifact_activation_failed": "Состояние артефактов импортированного проекта несогласованно: {reason}",
    "arch_project_migration_failed": "Данные проекта в архиве нельзя обновить до текущей версии: {reason}",
    "arch_import_confirmation_required": "При обновлении архива часть содержимого изменилась. Подтвердите перед импортом",
    "arch_not_a_zip": "Загруженный файл не является допустимым ZIP-архивом",
    "arch_zip_encrypted_entry": "ZIP содержит зашифрованный элемент и не может быть импортирован: {name}",
    "arch_zip_absolute_path_entry": "ZIP содержит элемент с абсолютным путём: {name}",
    "arch_zip_traversal_entry": "ZIP содержит элемент с выходом за пределы каталога: {name}",
    "arch_zip_symlink_entry": "ZIP содержит символическую ссылку: {name}",
    "arch_zip_unparsable_member": "Не удалось разобрать {label}: {path}",
    "arch_multiple_manifests": (
        "ZIP содержит несколько файлов arcreel-export.json; невозможно определить корень проекта"
    ),
    "arch_manifest_missing_project_json": "В официальном пакете экспорта отсутствует project.json",
    "arch_no_project_json": "В ZIP не найден project.json",
    "arch_multiple_project_json": "ZIP содержит несколько файлов project.json; невозможно определить корень проекта",
    "arch_extract_path_traversal": "Путь распаковки выходит за пределы целевого каталога: {path}",
    "arch_conflict_detected": "Обнаружен конфликт ID проекта",
    "arch_project_name_conflict": (
        "Проект с ID «{name}» уже существует. Перезапишите существующий проект или импортируйте под новым именем."
    ),
    # ---- Custom endpoint definition validation ----
    "val_ce_missing_field": "Отсутствует обязательное поле: {field}",
    "val_ce_unknown_field": "Неизвестное поле: {field}",
    "val_ce_removed_field": "Поле удалено из формата: {field} - {reason}",
    "val_ce_invalid_type": "Неверный тип; ожидается {expected}",
    "val_ce_invalid_enum_value": "Значение здесь недопустимо; допустимые: {allowed}",
    "val_ce_invalid_value": "Значение не соответствует формату: {detail}",
    "val_ce_schema_violation": "Не соответствует формату определения: {detail}",
    "val_ce_schema_minimum_constraint": "Минимально допустимое: {limit}",
    "val_ce_schema_maximum_constraint": "Максимально допустимое: {limit}",
    "val_ce_schema_format_constraint": "Не соответствует требуемому формату: {constraint}",
    "val_ce_schema_forbidden_constraint": "Значение попадает под запрещающее ограничение",
    "val_ce_schema_generic_constraint": "Ограничение определения не выполнено ({keyword})",
    "val_ce_removed_reason_request_query": (
        "статические и динамические параметры запроса задаются в шаблоне url, query с учётными данными — в auth.query"
    ),
    "val_ce_removed_reason_status_codes": (
        "обработка HTTP-статусов — политика среды выполнения: 2xx — успех, 429 и 5xx — повтор, всё остальное — ошибка"
    ),
    "val_ce_removed_reason_polling_policy": "интервал и тайм-аут опроса — политика среды выполнения, а не часть определения",
    "val_ce_removed_reason_extract_source": "извлечение всегда начинается с тела ответа; HTTP-статус не является путём",
    "val_ce_removed_reason_extract_usage_keys": "usage теперь находится в poll.extract.usage",
    "val_ce_removed_reason_mime_types": "форматы ассетов не ограничиваются списком; провайдер сам отклоняет то, что не может принять",
    "val_ce_removed_reason_comfyui_capabilities": (
        "эндпоинт ComfyUI выводит свои возможности из привязок узлов, поэтому определение не содержит их объявления"
    ),
    "val_ce_malformed_placeholder": (
        "{fragment} не является допустимым плейсхолдером: поддерживаются только простые переменные "
        "(например, prompt или inputs.first_frame) — без фильтров, индексов и выражений, "
        "и каждая открывающая скобка должна быть закрыта"
    ),
    "val_ce_undeclared_variable": "Плейсхолдер {name} ссылается на необъявленную переменную",
    "val_ce_api_key_outside_auth": (
        "api_key может встречаться только в разделе auth: учётные данные не должны попадать в тело запроса и URL, "
        "а также в определения, которыми вы делитесь"
    ),
    "val_ce_auth_without_api_key": (
        "Раздел auth не пуст, но нигде не ссылается на api_key: для API без учётных данных оставьте его пустым, "
        "иначе пусть он записывает учётные данные"
    ),
    "val_ce_auth_header_conflict": (
        "{header} совпадает с auth.headers (без учёта регистра): заголовки с учётными данными может записывать только раздел auth"
    ),
    "val_ce_header_name_duplicate": (
        "{header} отличается от {first} в той же карте только регистром: имена HTTP-заголовков не зависят от регистра, "
        "поэтому будут отправлены оба"
    ),
    "val_ce_auth_query_conflict": (
        "URL уже содержит параметр запроса {param}, объявленный в auth.query: "
        "параметры запроса с учётными данными может записывать только раздел auth"
    ),
    "val_ce_auth_query_reserved": "Запись auth.query {param} совпадает с параметром, который уже есть у загрузки артефакта; при загрузке учётные данные будут перезаписаны, выберите другое имя",
    "val_ce_task_id_out_of_scope": "task_id доступен только в разделах poll и result",
    "val_ce_result_id_out_of_scope": "result_id доступен только в разделе result",
    "val_ce_result_id_without_extract": "Есть ссылка на result_id, но poll.extract не объявляет result_id",
    "val_ce_input_out_of_scope": (
        "На ассет {name} можно ссылаться только из submit: запросы poll и result не передают ассеты"
    ),
    "val_ce_list_input_requires_each": "{name} — ассет-список; разверните его через $each вместо подстановки",
    "val_ce_each_in_not_list_input": "$each.in указывает на {name}, который не объявлен как ассет-список",
    "val_ce_each_shape_invalid": (
        "$each принимает либо item — для развёртывания элементов массива, либо key и value — для развёртывания записей объекта; "
        "смешивать эти формы нельзя"
    ),
    "val_ce_each_position_mismatch": (
        "Форма $each не соответствует своей позиции: в позиции массива используйте item для развёртывания элементов, "
        "а в позиции объекта — key и value для развёртывания записей"
    ),
    "val_ce_each_alias_reserved": (
        "{name} — зарезервированная переменная внутри тела цикла и не может быть псевдонимом элемента $each"
    ),
    "val_ce_when_unknown_input": "$when указывает на {name}, который не объявлен как ассет",
    "val_ce_input_not_referenced": (
        "Ассет объявлен, но на него нет ссылок из submit: он никогда не отправляется и не может обеспечивать возможность"
    ),
    "val_ce_enum_map_variable_not_allowed": "{variable} нельзя сопоставлять; допустимые переменные: {allowed}",
    "val_ce_default_variable_not_allowed": "{variable} не может иметь значения по умолчанию; переменные, которые могут: {allowed}",
    "val_ce_default_value_type_invalid": "Значение по умолчанию для {variable} должно иметь тип {expected}",
    "val_ce_default_value_not_in_enum_map": "Значение по умолчанию {value} для {variable} отсутствует в enum_maps; доступные значения: {allowed}",
    "val_ce_status_map_target_invalid": "Статус {target} не входит в {allowed}; сопоставляйте истечение срока с failed",
    "val_ce_capability_declared_without_input": (
        "{capability} объявлена, но submit не ссылается ни на один ассет {source}, поэтому возможность была бы ложной"
    ),
    "val_ce_capability_input_without_declaration": (
        "submit ссылается на ассет {source}, не объявляя {capability}, "
        "поэтому ассет отправляется, а интерфейс скрывает эту возможность"
    ),
    "val_ce_capability_incoherent": "Возможность {capability} противоречит своей группе; требуется: {requirement}",
    "val_ce_capability_not_declared": "Объявите хотя бы одну возможность со значением true в capabilities: {allowed}",
    "val_ce_media_type_field_not_allowed": "{name} недоступно для определения с типом медиа {media_type}",
    "val_ce_artifact_extract_missing": "Нет пути извлечения артефакта; укажите хотя бы одно из: {keys}",
    "val_ce_jsonpath_not_a_string": "Путь извлечения должен быть строкой: {path_expression}",
    "val_ce_jsonpath_surrounding_whitespace": "Путь извлечения не должен окружаться пробелами: {path_expression}",
    "val_ce_jsonpath_missing_root": "Путь извлечения должен начинаться с $: {path_expression}",
    "val_ce_jsonpath_recursive_descent": (
        "Рекурсивный спуск в путях извлечения не допускается (символ {position}): {path_expression}"
    ),
    "val_ce_jsonpath_union": (
        "Селекторы объединения в путях извлечения не допускаются (символ {position}): {path_expression}"
    ),
    "val_ce_jsonpath_slice_step": (
        "Шаг среза в путях извлечения не допускается (символ {position}): {path_expression}"
    ),
    "val_ce_jsonpath_function_extension": (
        "Функции-расширения в путях извлечения не допускаются (символ {position}): {path_expression}"
    ),
    "val_ce_jsonpath_filter_root_reference": (
        "Фильтр не может ссылаться на корневой узел (символ {position}): {path_expression}"
    ),
    "val_ce_jsonpath_filter_non_singular": (
        "В фильтре допускаются только одиночные запросы (символ {position}): {path_expression}"
    ),
    "val_ce_jsonpath_regex_operator": (
        "Оператор сопоставления с регулярным выражением в путях извлечения не допускается (символ {position}): {path_expression}"
    ),
    "val_ce_jsonpath_syntax": "Синтаксическая ошибка в пути извлечения (символ {position}): {path_expression}",
    "val_ce_jsonpath_evaluation_failed": "Не удалось вычислить путь извлечения: {path_expression}",
    "val_ce_template_render_failed": "Не удалось отрисовать шаблон запроса: {detail}",
    "val_ce_auth_literal_credential": (
        "Похоже, это значение содержит настоящий секретный ключ: ссылайтесь на учётные данные через плейсхолдер "
        "api_key, иначе ключ будет раскрыт при экспорте или передаче определения"
    ),
    "val_ce_template_url_not_string": "Шаблон URL должен давать строку",
    "val_ce_unknown_asset_encoding": "Неизвестная кодировка ассета: {encoding}",
    "val_ce_enum_map_value_missing": "В enum_maps.{name} нет записи для «{value}»",
    "val_ce_template_text_variable_null": "Переменная {name} равна null и не может быть встроена в текст",
    "val_ce_each_value_not_list": "$each.in указывает на {name}, значение которого во время выполнения не является списком",
    # ---- ComfyUI endpoint: node bindings and import routing ----
    "val_ce_comfyui_binding_required": (
        "Перед сохранением эндпоинта {binding_key} нужно привязать к узлу: "
        "промпт определяет, что рисуется, а выход — какой файл забирается"
    ),
    "val_ce_comfyui_binding_key_not_allowed": (
        "У эндпоинта {media_type} нет привязки {binding_key}; доступные ключи: {allowed}"
    ),
    "val_ce_comfyui_node_not_found": (
        "В workflow нет узла {node}: ID узлов меняются при каждом редактировании workflow, "
        "поэтому импортируйте его заново и подтвердите привязки узлов"
    ),
    "val_ce_comfyui_input_not_found": "У узла {node} нет входа с именем {input}",
    "val_ce_comfyui_input_is_link": (
        "Вход {input} узла {node} подключён от вышестоящего узла и будет перезаписан при выполнении; "
        "привяжите вместо него поле-литерал"
    ),
    "val_ce_comfyui_target_collision": (
        "Во вход {input} узла {node} уже записывается {owner}, поэтому {binding_key} не может писать в то же "
        "поле: что заполнится последним, то перезапишет другое"
    ),
    "val_ce_comfyui_class_type_mismatch": (
        "В записи узел {node} указан как {class_type}, но в workflow он сейчас {actual}: повторное сопоставление "
        "определяет узлы по типу, поэтому несовпадение переносит привязку в другое место. Импортируйте заново и подтвердите привязки"
    ),
    "val_ce_comfyui_consumer_not_fed": (
        "Поток от узла референсного изображения {node} не доходит до входа {input} узла {consumer}: при неверно "
        "записанном потребителе запрос с меньшим числом изображений перепривяжет посторонний вход"
    ),
    "val_ce_comfyui_fps_conflict": (
        "У частоты кадров несколько противоречащих источников ({values}): для перевода длительности в число кадров нужна "
        "ровно одна частота кадров, оставьте только один из них"
    ),
    "val_ce_comfyui_ui_format_workflow": (
        "Это workflow ComfyUI в UI-формате, его нельзя отправить; экспортируйте его в ComfyUI через Export (API)"
    ),
    # ---- ComfyUI endpoint: node binding inference signals and hints ----
    "val_ce_infer_manual_binding": "Сохранённая привязка узла; цель, уже подтверждённая пользователем, сохраняется",
    "val_ce_infer_title_marker": "Заголовок узла содержит метку ARCREEL:",
    "val_ce_infer_external_node_family": "Узел параметров {family}, объявляющий параметр {parameter}",
    "val_ce_infer_sampler_port_trace": "Прослежено от порта кондиционирования {port} узла {node}",
    "val_ce_infer_alias_with_class_type": "{class_type} обычно несёт эту семантику",
    "val_ce_infer_alias_only": "Имя входа {input} — распространённое имя для этой семантики",
    "val_ce_infer_link_trace": "Его выход подаётся на вход {input} узла {consumer}",
    "val_ce_infer_class_type_tier": "{class_type} — более надёжный носитель среди кандидатов",
    "val_ce_infer_output_chain": "Находится в цепочке, которая создаёт итоговый выход",
    "val_ce_infer_title_polarity": "Заголовок узла «{title}» указывает на эту полярность",
    "val_ce_infer_note_reference_consumer_unknown": (
        "Референсные изображения подаются на вход {input} узла {node} ({class_type}); этот вход нельзя перепривязать, когда "
        "изображений меньше, поэтому последнее изображение повторяется, чтобы заполнить оставшиеся слоты"
    ),
    "val_ce_infer_note_batch_size_above_one": (
        "Вход {input} узла {node} ({class_type}) равен {value}, поэтому одна отправка даёт несколько изображений; "
        "ArcReel не меняет это значение"
    ),
    "val_ce_infer_note_manual_only_node": (
        "Узел {node} — это {class_type}; {binding_keys} в нём нельзя определить автоматически, привяжите их вручную"
    ),
    "val_ce_infer_note_computed_source": (
        "Кандидат {binding_key} попадает на вход {input} узла {node}, но его значение вычисляется выше по потоку "
        "и не может быть записано; укажите его вручную"
    ),
    "val_ce_infer_note_rematched": "Записи {binding_key} повторно сопоставлены с узлом {nodes}",
    "val_ce_infer_note_binding_lost": (
        "У записей {binding_key} нет цели в новом workflow (ранее узел {nodes}); "
        "определение выполнено заново, подтвердите его"
    ),
    "val_ce_infer_note_target_taken": (
        "Кандидат {binding_key} попадает на вход {input} узла {node}, в который также пишет {others}; "
        "одно поле может нести только одну привязку, поэтому он исключён из кандидатов {binding_key}"
    ),
    "val_ce_poll_without_task_id": "Запрос опроса нигде не ссылается на task_id; убедитесь, что так и задумано",
    "val_ce_jsonpath_wildcard_order": (
        "{path_expression} использует подстановочный знак: подстановка по объекту берёт только первый элемент, "
        "а порядок ключей в предпросмотре и на сервере может различаться"
    ),
    # ---- market source checks ----
    "val_market_index_unreadable": "Не удалось прочитать файл индекса: {detail}",
    "val_market_unsupported_schema_version": "schema_version индекса {version} новее поддерживаемой {supported}",
    "val_market_missing_field": "Отсутствует обязательное поле: {field}",
    "val_market_invalid_type": "Неверный тип; ожидается {expected}",
    "val_market_invalid_value": "Значение нарушает ограничение {keyword}: {constraint}",
    "val_market_slug_invalid": (
        "Недопустимый slug «{value}»: используйте строчные латинские буквы, цифры и дефисы, "
        "начинайте с буквы или цифры, не более 64 символов"
    ),
    "val_market_slug_duplicate": "Slug «{slug}» используется в этом источнике маркетплейса более одного раза",
    "val_market_slug_directory_mismatch": "Slug «{slug}» не совпадает с каталогом определения «{directory}»",
    "val_market_path_not_relative": (
        "«{value}» должен быть относительным путём внутри источника маркетплейса: "
        "без ведущего / или схемы, без .., и он не должен указывать за пределы источника"
    ),
    "val_market_file_missing": "Файл по ссылке не существует: {value}",
    "val_market_symlink_not_allowed": (
        "«{value}» — символическая ссылка: клиенты, загружающие через GitHub raw, получают текст пути цели ссылки, "
        "поэтому используйте обычный файл или каталог"
    ),
    "val_market_icon_format_invalid": "Иконка должна быть читаемым PNG, WebP или SVG с расширением, соответствующим содержимому",
    "val_market_icon_too_large": "Размер иконки {size} байт, что больше лимита {limit} байт",
    "val_market_icon_not_square": "Иконка должна быть квадратной; сейчас {width}×{height}",
    "val_market_icon_ambiguous": "Каталог записи может содержать только один файл иконки; найдено: {icons}",
    "val_market_definition_unreadable": "Не удалось прочитать определение: {detail}",
    "val_market_definition_invalid": "Определение не прошло проверку ({code}): {detail}",
    "val_market_projection_mismatch": (
        "Поле индекса {field} равно «{index_value}», что отличается от «{definition_value}» в meta определения"
    ),
    "val_market_min_app_version_invalid": "min_app_version «{value}» не соответствует semver (x.y.z)",
    "val_market_detail_meta_not_object": "В определении нет объекта meta",
    "val_market_cli_check_passed": "Проверка источника маркетплейса пройдена: {directory}",
    "val_market_cli_check_failed": "Проверка источника маркетплейса не пройдена, проблем: {count}",
    "val_market_cli_generate_written": "Записан {path} (записей: {count})",
    "val_market_cli_generate_failed": "Не удалось сгенерировать индекс, проблем: {count}",
}
