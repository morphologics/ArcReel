"""Request snapshot of a media generation task, taken at enqueue (generation records, API G16).

A generation attempt is joined from three stores: the task row (what was asked, its outcome), the usage ledger's
``api_calls`` rows (what was sent to the provider, cost, duration) and the version record of the result. Only the
first exists for every attempt: a task refused or cancelled before any provider call has no call row, and the task
payload alone does not say which editable prompt, options or reference inputs it was submitted with (asset sheet
tasks carry an empty payload; storyboard / video payloads carry the request prompt but not the references). This
module takes that picture once, when the task is queued, and the queue stores it as ``tasks.request_snapshot_json``.

What it records is what the *request* was, read from the project at that moment, not what the worker later resolved:

- ``prompt.editable``: the editable prompt the result is made from (a shot's ``image_prompt`` / ``video_prompt`` as in
  the script, a video unit's ``text``, an asset's or derivative's ``description``), with ``prompt.source`` naming it;
  ``prompt.request`` is the request's own prompt (an image edit's instruction, a request-body prompt) when it differs.
  The final rendered prompt is only known when the worker calls the provider: it is ``api_calls.prompt``.
- ``model``: the advisory provider derived at enqueue and the model the request chose (``None`` = the configured one).
- ``options``: the options the request declared (seed, duration, aspect ratio, resolution, audio, service tier, …),
  plus the canvas aspect ratio the project implies when the request does not name one.
- ``references``: the reference inputs as declared at enqueue (asset sheets and original images named by the entry,
  the previous storyboard image, a video's start / end frame, a derivative's base sheet, an edit's current image), each
  with its project-relative file and, for generated artifacts, the current version number and (where the version
  record has one) its content digest. The worker's own reference resolution (trimming to the model's limit, usability
  checks, digests of the frozen bytes) stays in the version record's basis.

Snapshots are best effort: a failure to read the project never blocks the enqueue (the caller logs and stores none).

媒体生成任务在入队时取下的请求快照（生成记录，API G16）。

一次生成尝试由三处存储拼成：任务行（请求了什么、结果如何）、用量账本的 ``api_calls`` 行（发给供应商的内容、费用、耗时）与结果的
版本记录。只有第一处对每次尝试都存在：在任何供应商调用之前被拒绝或取消的任务没有调用行，而任务载荷本身说不清它提交时的可编辑提示词、
选项与参考输入（资产图任务的载荷是空的；分镜图 / 视频载荷带请求提示词但不带参考）。本模块在任务入队时取下这一画面，由队列存为
``tasks.request_snapshot_json``。

记录的是「请求」本身，按那一刻的项目读取，而不是 worker 之后解析出的内容：

- ``prompt.editable``：结果所依据的可编辑提示词（剧本里分镜的 ``image_prompt`` / ``video_prompt``、视频单元的 ``text``、资产或
  衍生的 ``description``），``prompt.source`` 说明出处；``prompt.request`` 是请求自带的提示词（改图指令、请求体里的提示词），与前者
  不同时才记。最终渲染出的提示词只在 worker 调用供应商时才知道：即 ``api_calls.prompt``。
- ``model``：入队时派生的 advisory 供应商，与请求选定的模型（``None`` 表示用配置的模型）。
- ``options``：请求声明的选项（种子、时长、宽高比、分辨率、音频、服务档位……），请求未给宽高比时补上项目隐含的画布比例。
- ``references``：入队时声明的参考输入（条目点名的资产图与原图、上一分镜图、视频的首 / 尾帧、衍生的本体资产图、改图的当前图），
  每条带项目内相对路径；对生成产物另带当前版本号，版本记录有内容摘要时一并带上。worker 自己的参考解析（按模型上限裁剪、可用性判定、
  冻结字节的摘要）仍在版本记录的依据里。

快照尽力而为：读项目失败绝不阻断入队（调用方记日志、不存快照）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lib.artifacts.video_visual_provenance import resolve_video_aspect_ratio
from lib.project.asset_derivatives import DERIVATIVE_TASK_TYPE, split_derivative_artifact_id
from lib.project.asset_types import ASSET_SPECS, DERIVATIVES_FIELD, asset_name_comparison_key
from lib.project.resource_paths import CHARACTER_DERIVATIVE_RESOURCE_TYPE, END_FRAME_RESOURCE_TYPE
from lib.references.reference_catalog import build_reference_catalog

#: Shape version of the snapshot; bump when a field changes meaning. / 快照形状版本；字段含义变化时递增。
REQUEST_SNAPSHOT_SCHEMA = 1

#: Media types whose tasks are generation attempts (text and render tasks are not). / 其任务算生成尝试的媒体类型（文本与渲染任务不算）。
GENERATION_MEDIA_TYPES: tuple[str, ...] = ("image", "video", "audio")

#: Version-history resource type (``lib.project.resource_paths``) of each generation task type. Image edits name the
#: edited kind in ``tasks.resource_type`` (singular, like the task types), mapped through the same table.
#: 各生成任务类型对应的版本历史资源类型（``lib.project.resource_paths``）。改图任务在 ``tasks.resource_type`` 里写明改的是哪类
#: （单数，同任务类型），经同一张表映射。
RESOURCE_TYPE_BY_TASK_TYPE: dict[str, str] = {
    "storyboard": "storyboards",
    "video": "videos",
    "reference_video": "reference_videos",
    "grid": "grids",
    "tts": "audio",
    **{asset_type: spec.subdir for asset_type, spec in ASSET_SPECS.items()},
    DERIVATIVE_TASK_TYPE: CHARACTER_DERIVATIVE_RESOURCE_TYPE,
}

IMAGE_EDIT_TASK_TYPE = "image_edit"

#: Payload keys that are request options, copied as declared. / 属于请求选项的载荷键，按声明原样复制。
_OPTION_KEYS: tuple[str, ...] = (
    "seed",
    "duration_seconds",
    "aspect_ratio",
    "resolution",
    "image_size",
    "quality",
    "generate_audio",
    "service_tier",
    "reference_request_options",
    "voice",
    "speed",
    "language_type",
)

_EPISODE_SCRIPT = re.compile(r"(?:^|/)episode_(\d+)\.json$")


def generation_resource_type(task_type: str, resource_type: str | None) -> str | None:
    """Version-history resource type of a task's target; ``None`` when the task type has no versioned result.

    任务目标的版本历史资源类型；任务类型没有带版本的结果时为 ``None``。"""
    if task_type == IMAGE_EDIT_TASK_TYPE:
        return RESOURCE_TYPE_BY_TASK_TYPE.get(resource_type or "")
    return RESOURCE_TYPE_BY_TASK_TYPE.get(task_type)


def task_types_for_resource_type(resource_type: str) -> tuple[str, ...]:
    """Task types whose target is this version-history resource type; an image edit of that kind names one of them in
    ``tasks.resource_type``.

    目标为该版本历史资源类型的任务类型；改这一类的改图任务在 ``tasks.resource_type`` 里写的也是其中之一。"""
    return tuple(kind for kind, mapped in RESOURCE_TYPE_BY_TASK_TYPE.items() if mapped == resource_type)


def episode_of_script_file(script_file: str | None) -> int | None:
    """Episode number of an ``episode_N.json`` script file name; ``None`` otherwise.

    ``episode_N.json`` 剧本文件名的集号；其余为 ``None``。"""
    if not script_file:
        return None
    match = _EPISODE_SCRIPT.search(script_file)
    return int(match.group(1)) if match else None


def build_request_snapshot(
    project_manager: Any,
    *,
    project_name: str,
    task_type: str,
    resource_id: str,
    resource_type: str | None,
    script_file: str | None,
    payload: Mapping[str, Any] | None,
    provider_id: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Take the request snapshot of one media generation task from the project as it is now (sync: reads
    ``project.json``, the script when the task has one, and ``versions.json`` once). Raises on unreadable data; the
    caller decides that a failure stores no snapshot.

    按项目此刻的状态取一条媒体生成任务的请求快照（同步：读 ``project.json``、任务有剧本时读剧本、读一次 ``versions.json``）。
    数据读不了时抛出；失败即不存快照由调用方决定。"""
    # Local import: lib.config pulls in the backend assembly; the read side of this module does not need it.
    # 局部导入：lib.config 会拉进后端装配；本模块的读侧用不到。
    from lib.config.resolver import VIDEO_MODEL_OVERRIDE_KEY

    payload = dict(payload or {})
    project = project_manager.load_project(project_name)
    project_path = Path(project_manager.get_project_path(project_name))
    script = project_manager.load_script(project_name, script_file) if script_file else None

    builder = _Builder(project, script)
    edit_kind = resource_type if task_type == IMAGE_EDIT_TASK_TYPE else None
    kind = edit_kind or task_type
    if kind == "storyboard" and task_type != IMAGE_EDIT_TASK_TYPE:
        builder.storyboard(resource_id)
    elif kind == "video":
        builder.video(resource_id)
    elif kind == "reference_video":
        builder.reference_video(resource_id)
    elif kind in ASSET_SPECS and task_type != IMAGE_EDIT_TASK_TYPE:
        builder.asset(kind, resource_id)
    elif kind == DERIVATIVE_TASK_TYPE and task_type != IMAGE_EDIT_TASK_TYPE:
        builder.derivative(resource_id)
    elif kind == "tts":
        builder.narration(resource_id)
    if task_type == IMAGE_EDIT_TASK_TYPE:
        builder.edit_source(edit_kind, resource_id)

    request_prompt = payload.get("prompt")
    options = {key: payload[key] for key in _OPTION_KEYS if key in payload}
    if "aspect_ratio" not in options and (ratio := builder.canvas_ratio(kind)) is not None:
        options["aspect_ratio"] = ratio
    chosen_model = payload.get("image_model") or payload.get(VIDEO_MODEL_OVERRIDE_KEY)
    return {
        "schema": REQUEST_SNAPSHOT_SCHEMA,
        "taken_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "prompt": {
            "editable": builder.editable,
            "source": builder.editable_source,
            "request": request_prompt if request_prompt is not None and request_prompt != builder.editable else None,
        },
        "model": {
            "provider": payload.get("image_provider") or provider_id,
            "model": chosen_model if isinstance(chosen_model, str) and chosen_model else None,
        },
        "options": options,
        "references": _with_versions(project_path, builder.references, project_manager=project_manager),
    }


class _Builder:
    """Collects the editable prompt and the declared references of one target. / 收集一个目标的可编辑提示词与声明的参考。"""

    def __init__(self, project: Mapping[str, Any], script: Mapping[str, Any] | None) -> None:
        self.project = project
        self.script = script
        self.editable: Any = None
        self.editable_source: str | None = None
        self.references: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str | None]] = set()

    # -- targets ----------------------------------------------------------------------------------------------

    def storyboard(self, resource_id: str) -> None:
        found = self._script_item(resource_id)
        if found is None:
            return
        item, index, items, id_field, fields = found
        self._editable(item.get("image_prompt"), "script.image_prompt")
        catalog = build_reference_catalog(self.project)
        for name in _names(item.get("products_in_shot")):
            self._asset_reference(catalog, "product", name)
        for asset_type, field in zip(("character", "scene", "prop"), fields, strict=True):
            for name in _names(item.get(field) if field else None):
                self._asset_reference(catalog, asset_type, name)
        if index > 0 and not item.get("segment_break"):
            previous = items[index - 1]
            if isinstance(previous, Mapping) and (previous_id := str(previous.get(id_field) or "").strip()):
                self._add(
                    "previous_storyboard",
                    "storyboards",
                    previous_id,
                    _generated(previous, "storyboard_image"),
                )

    def video(self, resource_id: str) -> None:
        found = self._script_item(resource_id)
        if found is None:
            return
        item = found[0]
        self._editable(item.get("video_prompt"), "script.video_prompt")
        self._add("start_frame", "storyboards", resource_id, _generated(item, "storyboard_image"))
        if (last := _generated(item, "storyboard_last_image")) is not None:
            self._add("end_frame", END_FRAME_RESOURCE_TYPE, resource_id, last)

    def reference_video(self, resource_id: str) -> None:
        unit = self._video_unit(resource_id)
        if unit is None:
            return
        text = unit.get("text")
        self._editable(text, "script.text")
        if not isinstance(text, str):
            return
        # Local import: the text parser pulls in the reference-video request projection, which only these tasks need.
        # 局部导入：正文解析会拉进参考生视频的请求投影，只有这类任务需要。
        from lib.script.reference_video.text_parser import derive_references_from_text

        catalog = build_reference_catalog(self.project)
        references, _missing = derive_references_from_text(text, dict(self.project), catalog=catalog)
        for reference in references:
            self._asset_reference(catalog, reference.type, reference.name)

    def asset(self, asset_type: str, name: str) -> None:
        spec = ASSET_SPECS[asset_type]
        entry = _asset_entry(self.project.get(spec.bucket_key), name)
        if entry is None:
            return
        identity, asset = entry
        self._editable(asset.get("description"), "asset.description")
        for field in spec.original_image_fields:
            values = asset.get(field)
            for path in values if isinstance(values, list) else [values]:
                if isinstance(path, str) and path:
                    self._add("original", None, identity, path, logical_type=asset_type)

    def derivative(self, resource_id: str) -> None:
        try:
            owner, derivative = split_derivative_artifact_id(resource_id)
        except ValueError:
            return
        spec = ASSET_SPECS["character"]
        entry = _asset_entry(self.project.get(spec.bucket_key), owner)
        if entry is None:
            return
        owner_name, owner_asset = entry
        derivatives = owner_asset.get(DERIVATIVES_FIELD)
        found = _asset_entry(derivatives, derivative)
        if found is not None:
            self._editable(found[1].get("description"), "asset.derivative.description")
        sheet = owner_asset.get(spec.sheet_field)
        self._add("owner_sheet", spec.subdir, owner_name, sheet if isinstance(sheet, str) and sheet else None)

    def narration(self, resource_id: str) -> None:
        found = self._script_item(resource_id)
        if found is not None:
            item = found[0]
            for field in ("narration", "novel_text", "dialogue"):
                if item.get(field) is not None:
                    self._editable(item.get(field), f"script.{field}")
                    return

    def edit_source(self, edit_kind: str | None, resource_id: str) -> None:
        resource_type = RESOURCE_TYPE_BY_TASK_TYPE.get(edit_kind or "")
        if resource_type is None:
            return
        file: str | None = None
        if edit_kind == "storyboard":
            found = self._script_item(resource_id)
            file = _generated(found[0], "storyboard_image") if found is not None else None
        elif edit_kind in ASSET_SPECS:
            spec = ASSET_SPECS[edit_kind]
            entry = _asset_entry(self.project.get(spec.bucket_key), resource_id)
            sheet = entry[1].get(spec.sheet_field) if entry is not None else None
            file = sheet if isinstance(sheet, str) and sheet else None
        self._add("edit_source", resource_type, resource_id, file)

    def canvas_ratio(self, kind: str) -> str | None:
        if kind in ("storyboard", "grid"):
            return resolve_video_aspect_ratio(self.project, "storyboards")
        if kind in ("video", "reference_video"):
            return resolve_video_aspect_ratio(self.project, "videos")
        if kind in ASSET_SPECS or kind == DERIVATIVE_TASK_TYPE:
            # Local import: generation_input is a heavy module; only its constant is needed here.
            # 局部导入：generation_input 较重，这里只要它的常量。
            from lib.artifacts.generation_input import ASSET_SHEET_CANVAS_RATIO

            return ASSET_SHEET_CANVAS_RATIO
        return None

    # -- helpers ----------------------------------------------------------------------------------------------

    def _editable(self, value: Any, source: str) -> None:
        self.editable = value
        self.editable_source = source

    def _script_item(self, resource_id: str) -> tuple[Mapping[str, Any], int, list[Any], str, tuple] | None:
        if not isinstance(self.script, Mapping):
            return None
        from lib.script.storyboard_sequence import find_storyboard_item, get_storyboard_items

        items, id_field, char_field, scene_field, prop_field = get_storyboard_items(dict(self.script))
        found = find_storyboard_item(items, id_field, resource_id)
        if found is None:
            return None
        item, index = found
        return item, index, items, id_field, (char_field, scene_field, prop_field)

    def _video_unit(self, resource_id: str) -> Mapping[str, Any] | None:
        if not isinstance(self.script, Mapping):
            return None
        from lib.script.script_editor import resolve_items

        items, id_field, _kind = resolve_items(dict(self.script))
        for item in items:
            if isinstance(item, Mapping) and str(item.get(id_field)) == str(resource_id):
                return item
        return None

    def _asset_reference(self, catalog: Any, asset_type: str, name: str) -> None:
        entry = catalog.lookup(asset_type, name)
        if entry is None:
            # Declared but not registered: kept, so the record shows what the request named.
            # 声明了却未登记：照样记下，记录里能看到请求点了什么名。
            self._add("asset_sheet", ASSET_SPECS[asset_type].subdir, name, None, registered=False)
            return
        sheet = entry.asset.get(entry.spec.sheet_field)
        self._add("asset_sheet", entry.spec.subdir, entry.name, sheet if isinstance(sheet, str) and sheet else None)
        originals = entry.asset.get("reference_images") if asset_type == "product" else None
        for path in originals if isinstance(originals, list) else []:
            if isinstance(path, str) and path:
                self._add("original", None, entry.name, path, logical_type=asset_type)

    def _add(
        self,
        role: str,
        resource_type: str | None,
        resource_id: str,
        file: str | None,
        *,
        logical_type: str | None = None,
        registered: bool = True,
    ) -> None:
        key = (file or f"{resource_type}:{resource_id}", role)
        if key in self._seen:
            return
        self._seen.add(key)
        reference: dict[str, Any] = {
            "role": role,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "file": file,
        }
        if logical_type is not None:
            reference["asset_type"] = logical_type
        if not registered:
            reference["registered"] = False
        self.references.append(reference)


def _names(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value]


def _generated(item: Mapping[str, Any], field: str) -> str | None:
    assets = item.get("generated_assets")
    value = assets.get(field) if isinstance(assets, Mapping) else None
    return value if isinstance(value, str) and value else None


def _asset_entry(bucket: object, name: str) -> tuple[str, Mapping[str, Any]] | None:
    """Entry of ``name`` in an asset table, by asset identity (same rule as the generation input). / 按资产身份在资产表里取条目。"""
    identity = asset_name_comparison_key(name)
    found: tuple[str, Mapping[str, Any]] | None = None
    if isinstance(bucket, Mapping):
        for key, entry in bucket.items():
            if isinstance(key, str) and asset_name_comparison_key(key) == identity and isinstance(entry, Mapping):
                found = (identity, entry)
    return found


def _with_versions(
    project_path: Path, references: Iterable[dict[str, Any]], *, project_manager: Any
) -> list[dict[str, Any]]:
    """Add the current version number (and its content digest when recorded) of each generated reference; one read of
    ``versions.json``.

    给每条生成产物参考补上当前版本号（有记录时连同内容摘要）；只读一次 ``versions.json``。"""
    references = list(references)
    keys = [(ref["resource_type"], ref["resource_id"]) for ref in references if ref.get("resource_type")]
    if not keys:
        return references
    from lib.artifacts.version_manager import VERSION_CONTENT_DIGEST, VersionManager

    records = VersionManager(project_path).resource_records(keys)
    for reference in references:
        record = records.get(reference.get("resource_type") or "", {}).get(reference["resource_id"])
        if not isinstance(record, Mapping):
            continue
        current = record.get("current_version")
        if not isinstance(current, int) or isinstance(current, bool) or current < 1:
            continue
        reference["version"] = current
        for version in record.get("versions") or []:
            if isinstance(version, Mapping) and version.get("version") == current:
                if (digest := version.get(VERSION_CONTENT_DIGEST)) is not None:
                    reference["digest"] = digest
                break
    return references


__all__ = [
    "GENERATION_MEDIA_TYPES",
    "IMAGE_EDIT_TASK_TYPE",
    "REQUEST_SNAPSHOT_SCHEMA",
    "RESOURCE_TYPE_BY_TASK_TYPE",
    "build_request_snapshot",
    "episode_of_script_file",
    "generation_resource_type",
    "task_types_for_resource_type",
]
