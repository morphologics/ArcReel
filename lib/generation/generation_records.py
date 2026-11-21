"""Generation records (API G16): one record per generation attempt, joining its task, ledger calls and result version.

Sources, each the owner of its part:

- the task row (``tasks``): target, status, failure, client, times, and the request snapshot taken at enqueue
  (``lib.generation.request_snapshot``). Tasks queued before snapshots existed get a reduced request built from their
  payload (``schema`` 0: request prompt and declared options, no editable prompt or references);
- the usage ledger (``api_calls``, by ``task_id``): every provider attempt the task made, with the prompt actually
  sent, the options as sent, reference paths, cost and duration. Empty when the attempt was refused or cancelled
  before reaching a provider;
- the task result (``version``, ``file_path``) and the version record of that version in ``versions/versions.json``:
  the version's own snapshot file (immutable, unlike the current file) and whether the record names this task
  (``linked``: the exact link written since G16, or the video checkpoint's ``execution_task_id``).

The models stay generic: no review, approval or round semantics. Lineage (which result fed which generation, G17)
builds on ``request.references`` (resource + version + digest) and the exact version link; it is not resolved here.

生成记录（API G16）：每次生成尝试一条记录，把它的任务、账本调用与结果版本拼在一起。

来源（各自拥有自己那部分）：

- 任务行（``tasks``）：目标、状态、失败、客户端、时间，以及入队时取下的请求快照（``lib.generation.request_snapshot``）。
  快照出现之前入队的任务按载荷拼出简化的请求（``schema`` 0：请求提示词与声明的选项，没有可编辑提示词与参考）；
- 用量账本（``api_calls``，按 ``task_id``）：任务发起的每次供应商尝试，带实际发出的提示词、发出时的选项、参考路径、费用与
  耗时。尝试在到达供应商之前被拒绝或取消时为空；
- 任务结果（``version``、``file_path``）与 ``versions/versions.json`` 里该版本的记录：版本自己的快照文件（不可变，不同于
  current 文件），以及记录是否点名了本任务（``linked``：G16 起写入的精确关联，或视频检查点的 ``execution_task_id``）。

模型保持通用：没有审阅、批准或轮次语义。血缘（哪个结果喂给了哪次生成，G17）建立在 ``request.references``（资源 + 版本 +
摘要）与精确版本关联之上，这里不解析。
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from lib.artifacts.version_manager import GENERATION_TASK_VERSION_FIELD
from lib.generation.request_snapshot import (
    episode_of_script_file,
    generation_resource_type,
)

#: Payload keys carried into a reduced (``schema`` 0) request for tasks without a snapshot.
#: 没有快照的任务拼简化请求（``schema`` 0）时带上的载荷键。
_LEGACY_OPTION_KEYS = ("seed", "duration_seconds", "aspect_ratio", "resolution", "generate_audio", "service_tier")

#: Width of the result thumbnail URL for images (``GET /files/…?w=``). / 图片结果缩略图 URL 的宽度。
RESULT_THUMBNAIL_WIDTH = 320


class GenerationResource(BaseModel):
    """The generation's target: version-history resource type (``storyboards``, ``videos``, ``characters``, …; null
    for task types without versions), id, and the episode for script-bound targets.

    生成目标：版本历史资源类型（无版本的任务类型为 null）、id，绑定剧本的目标另带集号。"""

    model_config = ConfigDict(extra="forbid")

    type: str | None
    id: str
    episode: int | None = None
    script_file: str | None = None


class GenerationReference(BaseModel):
    """One reference input as declared at enqueue. / 入队时声明的一条参考输入。"""

    model_config = ConfigDict(extra="allow")

    #: asset_sheet | original | previous_storyboard | start_frame | end_frame | owner_sheet | edit_source
    role: str
    resource_type: str | None = None
    resource_id: str
    #: Project-relative file declared at enqueue; null when not declared yet. / 入队时声明的项目相对路径；未声明为 null。
    file: str | None = None
    #: Current version of a generated reference at enqueue. / 入队时生成产物参考的当前版本。
    version: int | None = None
    #: Content digest of that version when its record has one (videos / audio). / 该版本记录有内容摘要时带上。
    digest: str | None = None


class GenerationPrompt(BaseModel):
    """Editable prompt at enqueue and the request's own prompt when different. Null in list items unless
    ``include=prompts``.

    入队时的可编辑提示词，以及与之不同时请求自带的提示词。列表项里除非 ``include=prompts`` 否则为 null。"""

    model_config = ConfigDict(extra="forbid")

    #: Text or structured JSON as stored in the script / asset. / 剧本 / 资产里存的文本或结构化 JSON。
    editable: Any = None
    #: Where ``editable`` came from: script.image_prompt | script.video_prompt | script.text | asset.description | …
    source: str | None = None
    request: Any = None


class GenerationModelChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Provider derived at enqueue (advisory; the call's provider is authoritative). / 入队时派生的供应商（参考）。
    provider: str | None = None
    #: Model the request chose; null = the configured one. / 请求选定的模型；null 表示用配置的模型。
    model: str | None = None


class GenerationRequest(BaseModel):
    """The inputs the attempt was submitted with (snapshot at enqueue). ``schema`` 0 = reduced, rebuilt from the task
    payload of a task queued before snapshots existed.

    尝试提交时的输入（入队快照）。``schema`` 0 表示简化版：快照出现之前入队的任务，由载荷拼出。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(alias="schema", serialization_alias="schema")
    taken_at: str | None = None
    prompt: GenerationPrompt
    model: GenerationModelChoice
    options: dict[str, Any] = Field(default_factory=dict)
    references: list[GenerationReference] = Field(default_factory=list)


class GenerationCall(BaseModel):
    """One provider attempt from the usage ledger (``api_calls``). / 用量账本里的一次供应商尝试。"""

    model_config = ConfigDict(extra="forbid")

    id: int
    media_type: str
    provider: str
    model: str
    #: pending | success | failed | cancelled
    status: str
    #: The prompt actually sent; null in list items unless ``include=prompts``. / 实际发出的提示词。
    prompt: str | None = None
    resolution: str | None = None
    duration_seconds: int | None = None
    aspect_ratio: str | None = None
    generate_audio: bool | None = None
    #: Inputs as sent: reference images ``[{path, label, role}]``, start / end frame, audio, extra parameters.
    inputs: Any = None
    output_path: str | None = None
    error_code: str | None = None
    error_params: Any = None
    #: null = unpriced, 0 = priced at zero. / null 表示未定价，0 表示定价为零。
    cost_amount: float | None = None
    currency: str | None = None
    unpriced_reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    client: str | None = None


class GenerationCost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Priced spend per currency (calls costing 0 or unpriced are not in it). / 按币种合计的已定价花费。
    by_currency: dict[str, float] = Field(default_factory=dict)
    unpriced_calls: int = 0
    pending_calls: int = 0


class GenerationResult(BaseModel):
    """The version the attempt committed. / 尝试提交的版本。"""

    model_config = ConfigDict(extra="forbid")

    version: int
    #: The version's own snapshot (``versions/…``, immutable); null when the record is gone. / 版本自己的快照。
    file: str | None = None
    #: The resource's current file path (holds whichever version is current now). / 资源的当前文件路径。
    current_file: str | None = None
    #: ``GET /files/{project}/{file}?w=320`` for image results; null for video / audio. / 图片结果的缩略图 URL。
    thumbnail_url: str | None = None
    created_at: str | None = None
    #: The version record names this task (exact link); false = taken from the task result only.
    #: 版本记录点名了本任务（精确关联）；false 表示只来自任务结果。
    linked: bool = False


class GenerationRecord(BaseModel):
    """One generation attempt (succeeded, failed, cancelled, refused, queued or running) with its inputs, provider
    calls and result.

    一次生成尝试（成功、失败、取消、被拒、排队或进行中），带输入、供应商调用与结果。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    batch_id: str | None = None
    project: str
    resource: GenerationResource
    task_type: str
    media_type: str
    #: queued | running | succeeded | failed | cancelled
    status: str
    error_code: str | None = None
    error_params: Any = None
    #: Failure text in the request's language (never branch on it). / 请求语言的失败文本（不要据此分支）。
    error_message: str | None = None
    cancelled_by: str | None = None
    #: Client label that created the task (``webui``, ``mcp``, ``embedded`` or an ``X-Client`` value).
    client: str
    queued_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str | None = None
    #: Provider / model of the latest call, else the request's choice. / 最近一次调用的供应商 / 模型，否则取请求的选择。
    provider: str | None = None
    model: str | None = None
    request: GenerationRequest | None = None
    calls: list[GenerationCall] = Field(default_factory=list)
    cost: GenerationCost
    result: GenerationResult | None = None


class GenerationRecordPage(BaseModel):
    """A page of generation records, newest first. ``next_cursor`` null = last page; ``server_time`` = the next
    ``updated_since`` (API G24).

    一页生成记录，新的在前。``next_cursor`` 为 null 表示末页；``server_time`` 即下一次的 ``updated_since``（API G24）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[GenerationRecord]
    next_cursor: str | None = None
    server_time: str


def legacy_request(payload: Mapping[str, Any], provider_id: str | None) -> dict[str, Any]:
    """Reduced request of a task queued before snapshots existed (``schema`` 0). / 快照出现之前入队的任务的简化请求。"""
    prompt = payload.get("prompt")
    model = payload.get("image_model") or payload.get("video_model_override")
    return {
        "schema": 0,
        "taken_at": None,
        "prompt": {"editable": None, "source": None, "request": prompt},
        "model": {
            "provider": payload.get("image_provider") or provider_id,
            "model": model if isinstance(model, str) and model else None,
        },
        "options": {key: payload[key] for key in _LEGACY_OPTION_KEYS if key in payload},
        "references": [],
    }


def _cost(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_currency: dict[str, float] = {}
    unpriced = pending = 0
    for call in calls:
        if call["status"] == "pending":
            pending += 1
        elif call["cost_amount"] is None:
            unpriced += 1
        elif call["cost_amount"] > 0:
            currency = call["currency"] or "USD"
            by_currency[currency] = round(by_currency.get(currency, 0.0) + float(call["cost_amount"]), 6)
    return {"by_currency": by_currency, "unpriced_calls": unpriced, "pending_calls": pending}


def _result_version(task: Mapping[str, Any]) -> tuple[str | None, int] | None:
    """``(resource type, version)`` from a succeeded task's result. / 成功任务结果里的 ``(资源类型, 版本)``。"""
    result = task.get("result")
    if task.get("status") != "succeeded" or not isinstance(result, Mapping):
        return None
    version = result.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None
    resource_type = result.get("resource_type") or generation_resource_type(
        task["task_type"], task.get("resource_type")
    )
    return (resource_type if isinstance(resource_type, str) else None), version


#: Parsed ``versions.json`` per project, keyed by the file's (mtime_ns, size): the file is rewritten atomically on
#: every version change, so a changed stat means a new parse. Parsing dominates the list's cost (≈ 50 ms for a
#: project with a few hundred versions); a few entries cover the projects a client pages through.
#: 按项目缓存解析后的 ``versions.json``，以文件的 (mtime_ns, size) 为键：每次版本变化都原子重写该文件，stat 变了就重新解析。
#: 解析占列表开销的大头（几百个版本的项目约 50 ms）；几条缓存足够覆盖客户端翻页涉及的项目。
_VERSIONS_CACHE_SIZE = 8
_versions_cache: OrderedDict[Path, tuple[tuple[int, int], Mapping[str, Any]]] = OrderedDict()
_versions_cache_lock = threading.Lock()


def _versions_data(project_path: Path) -> Mapping[str, Any]:
    """The project's version history (read-only; shared between callers, never mutate). / 项目的版本历史（只读，勿改）。"""
    versions_file = project_path / "versions" / "versions.json"
    try:
        stat = versions_file.stat()
    except FileNotFoundError:
        return {}
    key = (stat.st_mtime_ns, stat.st_size)
    with _versions_cache_lock:
        cached = _versions_cache.get(versions_file)
        if cached is not None and cached[0] == key:
            _versions_cache.move_to_end(versions_file)
            return cached[1]
    data = json.loads(versions_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    with _versions_cache_lock:
        _versions_cache[versions_file] = (key, data)
        _versions_cache.move_to_end(versions_file)
        while len(_versions_cache) > _VERSIONS_CACHE_SIZE:
            _versions_cache.popitem(last=False)
    return data


def load_result_versions(
    tasks: Sequence[Mapping[str, Any]], project_path_of: Any
) -> dict[tuple[str, str, int], Mapping[str, Any]]:
    """Version records of the tasks' result versions, keyed ``(project, task_id, version)``: at most one
    ``versions.json`` parse per project on the page, none when the file is unchanged since the last read (sync; run in
    a worker thread). Unreadable projects are skipped.

    各任务结果版本的版本记录，键为 ``(项目, task_id, 版本)``：本页每个项目至多解析一次 ``versions.json``，文件自上次读取后
    未变则不解析（同步；放进工作线程）。读不了的项目跳过。"""
    wanted: dict[str, list[tuple[Mapping[str, Any], str, int]]] = {}
    for task in tasks:
        found = _result_version(task)
        if found is None or found[0] is None:
            continue
        resource_type, version = found
        wanted.setdefault(task["project_name"], []).append((task, resource_type, version))
    records: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    for project_name, items in wanted.items():
        try:
            versions = _versions_data(Path(project_path_of(project_name)))
        except (OSError, ValueError):
            continue
        for task, resource_type, version in items:
            bucket = versions.get(resource_type)
            history = bucket.get(task["resource_id"]) if isinstance(bucket, Mapping) else None
            if not isinstance(history, Mapping):
                continue
            for record in history.get("versions") or []:
                if isinstance(record, Mapping) and record.get("version") == version:
                    records[(project_name, task["task_id"], version)] = record
                    break
    return records


def build_generation_record(
    task: Mapping[str, Any],
    calls: Sequence[Mapping[str, Any]],
    *,
    version_record: Mapping[str, Any] | None,
    error: Mapping[str, Any],
    include_prompts: bool,
) -> GenerationRecord:
    """Assemble one record. ``error`` carries the rendered ``error_code`` / ``error_params`` / ``error_message`` of
    the task (the caller localizes). Without ``include_prompts`` the request's prompts and the calls' sent prompts
    are left out.

    拼一条记录。``error`` 是任务渲染好的 ``error_code`` / ``error_params`` / ``error_message``（由调用方本地化）。
    不带 ``include_prompts`` 时略去请求的提示词与调用发出的提示词。"""
    snapshot = task.get("request_snapshot")
    request = (
        dict(snapshot) if isinstance(snapshot, Mapping) else legacy_request(task["payload"], task.get("provider_id"))
    )
    if not include_prompts:
        request["prompt"] = {**request.get("prompt", {}), "editable": None, "request": None}
    resource_type = generation_resource_type(task["task_type"], task.get("resource_type"))
    latest = calls[-1] if calls else None
    model_choice = request.get("model") or {}

    result: GenerationResult | None = None
    found = _result_version(task)
    if found is not None:
        _resource_type, version = found
        task_result = task.get("result") or {}
        file = version_record.get("file") if version_record is not None else None
        linked = version_record is not None and task["task_id"] in (
            version_record.get(GENERATION_TASK_VERSION_FIELD),
            version_record.get("execution_task_id"),
        )
        thumbnail = (
            f"/api/v1/files/{quote(task['project_name'], safe='')}/{quote(file)}?w={RESULT_THUMBNAIL_WIDTH}"
            if isinstance(file, str) and task["media_type"] == "image"
            else None
        )
        result = GenerationResult(
            version=version,
            file=file if isinstance(file, str) else None,
            current_file=task_result.get("file_path") if isinstance(task_result.get("file_path"), str) else None,
            thumbnail_url=thumbnail,
            created_at=(version_record or {}).get("created_at") or task_result.get("created_at"),
            linked=linked,
        )

    return GenerationRecord(
        task_id=task["task_id"],
        batch_id=task.get("batch_id"),
        project=task["project_name"],
        resource=GenerationResource(
            type=resource_type,
            id=task["resource_id"],
            episode=episode_of_script_file(task.get("script_file")),
            script_file=task.get("script_file"),
        ),
        task_type=task["task_type"],
        media_type=task["media_type"],
        status=task["status"],
        error_code=error.get("error_code"),
        error_params=error.get("error_params"),
        error_message=error.get("error_message"),
        cancelled_by=task.get("cancelled_by"),
        client=task.get("source") or "webui",
        queued_at=task.get("queued_at"),
        started_at=task.get("started_at"),
        finished_at=task.get("finished_at"),
        updated_at=task.get("updated_at"),
        provider=latest["provider"] if latest else model_choice.get("provider"),
        model=latest["model"] if latest else model_choice.get("model"),
        request=GenerationRequest.model_validate(request),
        calls=[GenerationCall.model_validate(call) for call in calls],
        cost=GenerationCost.model_validate(_cost(calls)),
        result=result,
    )


__all__ = [
    "GenerationCall",
    "GenerationCost",
    "GenerationRecord",
    "GenerationRecordPage",
    "GenerationRequest",
    "GenerationResult",
    "build_generation_record",
    "legacy_request",
    "load_result_versions",
]
