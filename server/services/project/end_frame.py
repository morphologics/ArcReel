"""Service layer for setting and clearing a shot's end frame snapshot.

The end frame is a persistent user-intent property of the shot (``end_frame_image``), not a runtime output, so it
does not go into ``generated_assets``. The two setting channels (upload any image / give the relative path of an
existing image in the project) converge on the same landing point here: normalized to PNG, then the snapshot is
copied to ``end_frames/scene_{id}.png``, and the script entry only stores that fixed relative path. Source image and
snapshot are thereby fully decoupled: regenerating, rolling back or deleting the source image cannot touch the set
end frame, and dangling references cannot exist structurally.

Setting writes the snapshot file + writes the field; clearing moves the snapshot file into the project trash +
empties the field; each completes entirely within the critical section of the same script lock
(`ProjectManager.locked_script`), mutually exclusive with the other, so there is no interleaving where one side
finishes writing the file and the other deletes it before the field is written back, structurally ruling out
dangling references. If it fails inside the critical section because the target shot was deleted mid-flight, the
whole write-back is skipped, leaving no half state.

`locked_script` completes script persistence inside the lock, and the lock is released before validation/save
exceptions propagate to the caller, so filesystem side effects (writing / deleting the snapshot) cannot be rolled
back directly with a try/except outside the lock; that rollback would itself clash with another concurrent request
that successfully saved in the same gap. Compensation instead re-acquires the lock and does a conditional rollback
based on "is this shot's operation generation still the one before the failure" (see
`_restore_after_persist_failure`), undoing only its own effects and not overwriting others' saved results.
Generation rather than file content: content comparison has a degenerate-value problem: another concurrent clear
also leaves the file "absent", indistinguishable from the expected content of "nobody took over" (None), so the
result of a concurrent clear would be misjudged as nobody taking over, and the rollback would produce an orphan file
that should not exist. The generation is incremented on every entry into the critical section, whether or not that
operation eventually succeeds; if anyone entered the critical section between this failure and the rollback
re-acquiring the lock, the generation must have advanced, which reliably decides whether to skip. Orphan snapshot
files (e.g. the process killed after the file is written but before the lock is released) may still remain; they are
harmless, consistent with the current state of storyboards, and no cleanup mechanism is added.

The generation key must normalize the script file name with `ProjectManager.normalize_script_filename`:
`episode_1.json` and `scripts/episode_1.json` are valid aliases of the same script; without normalization the two
aliases would each produce a generation key, and concurrent requests operating on the same shot via different
aliases could not see each other. Normalization calls the public entry of `ProjectManager` rather than replicating
its internal rules in this file, so upstream changes (e.g. supporting a new alias form) do not make the two places
drift silently and split the generation key again.

The rollback baseline (`old_bytes`) must be read after acquiring the script lock and before the mutation, not
pre-read outside the lock: if pre-read outside the lock and another concurrent write succeeded in saving before this
operation, it would read stale content older than "before the operation", and on failure would overwrite that
successful write with it, even though the generation check itself is fine.

The "compensable" mark (`entered`) must be set only *after the baseline read succeeds*, not before: if set before,
when reading the baseline itself fails (permission / transient IO error) `old_bytes` is still the initial `None`,
and compensation would misjudge "read failed" as "the target file did not exist", then unlink an existing snapshot
this operation never touched.

Advancing the operation generation (`_advance_shot_generation`) must happen after the shot is matched, right before
the first file mutation, not earlier: if advanced earlier, a request that exits empty-handed because the shot was
not found would also advance the generation, so another request in the same time window that genuinely failed to
persist and is waiting to re-acquire the lock for compensation would misjudge this empty-handed exit as "taken over,
own state consistent" and skip rollback, leaving its own half-done effects from before the failure on disk.

分镜尾帧快照的设置与清除服务层。

尾帧是分镜的用户意图持久属性（``end_frame_image``），不是运行时产出，故不进
``generated_assets``。设置的两条通道（上传任意图片 / 指定项目内已有图片的相对路径）
在这里汇成同一落点：归一为 PNG 后快照复制到 ``end_frames/scene_{id}.png``，剧本条目
只存该固定相对路径。源图与快照就此彻底解耦——源图重生成、版本回滚、删除都动不到
已定尾帧，结构上不存在悬空引用。

设置写快照文件 + 写字段、清除把快照文件移进项目回收站 + 置空字段，各自整段落在同一把剧本锁
（`ProjectManager.locked_script`）临界区内完成，与对方互斥——不会出现一方写完文件、
对方抢在字段写回前把文件删掉的交错，结构上杜绝悬空引用。临界区内因目标分镜
mid-flight 被删除而失败时跳过整段写回，不留半截状态。

`locked_script` 在锁内完成剧本持久化，校验/落盘异常向调用方传出前锁已释放，故文件
系统副作用（写快照 / 删快照）不能靠锁外的 try/except 直接回滚——那样回滚本身会跟同
一间隙内另一个成功落盘的并发请求打架。补偿改为重新过锁并按「本分镜的操作代次是否仍是
失败前那一次」做条件回滚（见 `_restore_after_persist_failure`），只撤销自己的效果、不
覆盖旁人已落盘的成果。代次而非文件内容——内容比对有退化值问题：并发的另一次清除同样会
让文件落到「不存在」，与「无人接手」的期望内容（None）无法区分，会把并发清除的结果误判
为无人接手，回滚出不该存在的孤儿文件。代次在临界区内随每次进入自增，无论那次操作最终
成功与否，只要有人在本次失败到回滚重新过锁之间进入过临界区，代次必然前移，据此可靠判定
是否跳过。孤儿快照文件（如进程在文件写完、锁释放前被杀）仍可能残留，无害，与 storyboards
现状一致，不做清理机制。

代次键须按 `ProjectManager.normalize_script_filename` 归一化剧本文件名——`episode_1.json`
与 `scripts/episode_1.json` 是同一剧本的合法别名，不归一的话两个别名各自生成一把代次键，
用不同别名操作同一分镜的并发请求就互相看不见。归一化调用 `ProjectManager` 的公开入口而非
在本文件复刻其内部规则，避免上游改动（如支持新别名形式）时两处静默漂移、代次键重新分裂。

回滚基线（`old_bytes`）须在取得剧本锁之后、mutation 之前读取，不能在锁外预读——锁外预读的话，
若本次操作前还有一次并发写入抢先成功落盘，读到的就是比"操作前"更早的陈旧内容，失败时会用
它覆盖那次已成功的写入，即便代次判定本身没有问题。

标记"可补偿"（`entered`）须在基线读取*成功之后*才置位，不能在读之前——读之前置位的话，
读基线本身失败（权限 / 临时 IO 错误）时 `old_bytes` 仍是初始的 `None`，补偿会把"读失败"
误判成"目标文件原本不存在"，进而 unlink 一个从未被本次操作触碰过的既有快照。

操作代次的推进（`_advance_shot_generation`）须放在分镜匹配成功、紧邻首次文件 mutation
之前，不能在此之前就推进——提前推进的话，一次因分镜未找到而空手退出的请求也会让代次
前移，导致同一时间段内另一个真正持久化失败、正在等待重新过锁补偿的请求，把这次空手
退出误判成"已被接管、自身状态自洽"而跳过回滚，把它自己失败前的半截效果留在磁盘上。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from lib.infra.async_thread import run_sync_transaction
from lib.infra.image_utils import normalize_storyboard_upload
from lib.infra.json_io import domain_error_on_value_error
from lib.infra.path_safety import PathTraversalError, safe_join
from lib.project.media_trash import trash_end_frame
from lib.project.project_change_hints import project_change_source
from lib.project.project_manager import ProjectManager, get_project_manager, is_reference_video_project
from lib.project.resource_paths import END_FRAME_RESOURCE_TYPE, resource_relative_path
from lib.script.storyboard_sequence import find_storyboard_item, get_storyboard_items
from server.services.currency.upload_finalize import UPLOAD_IMAGE_MAX_BYTES, write_bytes_atomic

logger = logging.getLogger(__name__)

_ShotKey = tuple[str, str, str]
_generation_lock = threading.Lock()
_shot_generations: dict[_ShotKey, int] = {}


def _current_shot_generation(key: _ShotKey) -> int:
    with _generation_lock:
        return _shot_generations.get(key, 0)


def _advance_shot_generation(key: _ShotKey) -> int:
    """Advance this shot's operation generation and return the new value. Called inside the critical section, before
    the mutation; regardless of later failure, it represents "an operation entered the critical section", which the
    failing side's compensating rollback uses to decide whether it was taken over.

    推进该分镜的操作代次，返回新值。在临界区内、mutation 之前调用——无论后续
    是否失败，都代表「一次操作进入过临界区」，供失败一方的补偿回滚据此判断有无被接手。
    """
    with _generation_lock:
        generation = _shot_generations.get(key, 0) + 1
        _shot_generations[key] = generation
        return generation


class EndFrameError(Exception):
    """Domain error of end frame operations. The router layer translates it into an HTTP response by (status_code, key,
    params).

    尾帧操作的领域错误。路由层按 (status_code, key, params) 翻译为 HTTP 响应。"""

    def __init__(self, key: str, *, status_code: int = 400, **params: object):
        super().__init__(key)
        self.key = key
        self.status_code = status_code
        self.params = params


def _locate_shot(project_name: str, script_file: str, shot_id: str) -> Path:
    """Validate that this shot can have an end frame set and return the absolute project path; raise a domain error
    when it cannot.

    The reference-to-video path has no concept of first/last frames and always refuses. The check is made by the
    project.json generation mode (``is_reference_video_project``), not the script-level ``generation_mode`` stamp:
    script skeletons of the ad content mode do not carry that stamp (see ``script_generator``), and looking only at
    the script would let the "ad + reference-to-video" combination through, letting the user set an end frame that
    generation never consumes. All content modes share this rule.

    校验该分镜可设尾帧，返回项目绝对路径；不可设时抛领域错误。

    参考生视频路径无首尾帧概念，一律拒绝。该判定按 project.json 的生成模式
    （``is_reference_video_project``）作出，不看剧本级 ``generation_mode`` 戳——ad 创作类型的
    剧本骨架不携带该戳（见 ``script_generator``），只看剧本会放过「ad + 参考生视频」组合，让用户
    设下一个生成时永不被消费的尾帧。各创作类型共用这一口径。
    """
    manager = get_project_manager()
    try:
        project_path = manager.get_project_path(project_name)
    except FileNotFoundError as exc:
        raise EndFrameError("project_not_found", status_code=404, name=project_name) from exc
    except ValueError as exc:
        raise EndFrameError("invalid_project_name", status_code=400, name=project_name) from exc

    # A corrupted script file (JSON syntax error or non-UTF-8 bytes) must not be misjudged as an "invalid
    # script_file"; it is handled as 500 by the fallback branch of _translated_errors
    # 剧本文件损坏（JSON 语法错误或非 UTF-8 字节）不能误判为「非法 script_file」，
    # 交由 _translated_errors 的兜底分支收口为 500
    with domain_error_on_value_error(
        lambda _exc: EndFrameError("invalid_script_file", status_code=400, name=script_file),
        extra_passthrough=(UnicodeDecodeError,),
    ):
        script = manager.load_script(project_name, script_file)

    try:
        project = manager.load_project(project_name)
    except FileNotFoundError as exc:
        raise EndFrameError("project_not_found", status_code=404, name=project_name) from exc
    if is_reference_video_project(project):
        raise EndFrameError("end_frame_reference_video_unsupported")

    items, id_field, _, _, _ = get_storyboard_items(script)
    if find_storyboard_item(items, id_field, shot_id) is None:
        raise EndFrameError("segment_not_found", status_code=404, id=shot_id)
    return project_path


def _snapshot_target(project_path: Path, shot_id: str) -> tuple[Path, str]:
    """Return the end frame snapshot's (absolute path, project-relative path); raise a domain error when the path built
    from shot_id goes out of bounds.

    返回尾帧快照的 (绝对路径, 项目内相对路径)；shot_id 拼出的路径越界时抛领域错误。"""
    relative = resource_relative_path(END_FRAME_RESOURCE_TYPE, shot_id)
    try:
        target = safe_join(project_path, relative)
    except PathTraversalError as exc:
        raise EndFrameError("invalid_resource_id", resource_id=shot_id) from exc
    return target, relative


class _RollbackDone(Exception):
    """Sentinel: marks that the compensation branch has finished, used to skip the normal script write-back when
              `locked_script` exits.

    `locked_script` is based on `@contextmanager`: whether the `with` body falls out normally or `return`s, the
    generator continues to run `_write_script_unlocked` after `yield`. Compensation only changes the snapshot file
    and never reads or changes `script`; falling out normally would trigger a redundant save (also updating
    `metadata["updated_at"]`, triggering project sync and change events, and on failure being misrecorded as
    "rollback failed" by the `except Exception` here). Raising this sentinel exception makes the generator re-raise
    directly at `yield`, bypassing the write-back; the `except _RollbackDone: pass` below swallows it.

    哨兵：标记补偿分支已跑完，用于跳过 `locked_script` 退出时的正常剧本写回。

    `locked_script` 基于 `@contextmanager`：`with` 体内无论正常落出还是 `return`，生成器都
    会在 `yield` 之后继续跑 `_write_script_unlocked`。补偿只改快照文件，从不读改 `script`，
    正常落出会触发一次多余落盘（附带更新 `metadata["updated_at"]`、触发项目同步与变更事件，
    失败时还会被这里的 `except Exception` 误记成"回滚失败"）。抛这个哨兵异常换生成器在
    `yield` 处直接重新抛出，绕过写回；下面的 `except _RollbackDone: pass` 吞掉它。
    """


def _restore_after_persist_failure(
    manager: ProjectManager,
    project_name: str,
    script_file: str,
    target: Path,
    old_bytes: bytes | None,
    key: _ShotKey,
    expected_generation: int,
) -> None:
    """Compensation after script persistence fails: conditionally roll back the snapshot file within a new script lock
    critical section.

    Compensation itself must re-acquire the lock: `locked_script` completes persistence inside the lock, and the
    lock is released before the exception propagates; writing back the caller's old bytes directly outside the lock
    would clash with another concurrent request that successfully saved in the same gap.
    Before rolling back, use `expected_generation` to recheck whether this shot's operation generation is still the
    one before this failure: if so, nobody entered the critical section in the meantime and rollback is safe; if it
    advanced, a concurrent operation has taken over (whether it succeeded or not), the current state may already be
    consistent, and rolling back would only overwrite its results with stale bytes, so skip it.

    剧本持久化失败后的补偿：在新的一次剧本锁临界区内条件回滚快照文件。

    补偿本身必须重新过锁——`locked_script` 在锁内完成持久化，异常向上传播前锁已释放，
    若在锁外直接按调用方持有的旧字节回写，会跟同一间隙内另一个成功落盘的并发请求打架。
    回滚前用 `expected_generation` 复核该分镜的操作代次是否仍是本次失败前那一次：仍是
    则说明这段时间无人进入过临界区，安全回滚；已前移则说明并发操作已经接管（不论其
    自身成败），当前状态可能已自洽，回滚只会用陈旧字节覆盖它的成果，直接跳过。
    """
    try:
        with manager.locked_script(project_name, script_file):
            if _current_shot_generation(key) != expected_generation:
                raise _RollbackDone
            if old_bytes is not None:
                write_bytes_atomic(old_bytes, target)
            else:
                target.unlink(missing_ok=True)  # hard-delete: rollback -- remove the snapshot created here when setting the end frame fails / 设置尾帧失败时移除本次新建的快照
            raise _RollbackDone
    except _RollbackDone:
        pass
    except Exception:
        logger.exception(
            "尾帧快照回滚失败：project=%s script=%s target=%s（原持久化异常见上一条 traceback）",
            project_name,
            script_file,
            target,
        )


def _write_snapshot_and_field(
    project_name: str,
    script_file: str,
    shot_id: str,
    target: Path,
    png_bytes: bytes,
    relative: str,
) -> None:
    """Complete "write snapshot file + write field" within the script lock critical section, mutually exclusive with
    clearing.

    When script persistence fails (validation/save exception), restore the snapshot file to its pre-operation state;
    otherwise, although the field write is rolled back, the old content of the snapshot file has been silently
    replaced, and the caller gets a failure response without seeing that content was lost.
    Old bytes must be read after acquiring the script lock and before writing: if pre-read outside the lock and
    another concurrent write queued before this operation succeeded in saving first, the pre-read would be stale
    content older than "before the operation", and on failure it would overwrite that successful write.

    在剧本锁临界区内完成「写快照文件 + 写字段」，与清除操作互斥。

    剧本持久化失败（校验/落盘异常）时把快照文件还原成操作前状态——否则字段写入
    虽被回滚，快照文件的旧内容已被静默替换，调用方收到失败响应却看不出内容已丢失。
    读旧字节须在取得剧本锁之后、写入之前——锁外预读的话，若排在本次操作之前还有一次
    并发写入抢先成功落盘，预读到的就是比"操作前"更早的陈旧内容，失败时会用它把那次
    已成功的写入覆盖掉。
    """
    manager = get_project_manager()
    key = (project_name, ProjectManager.normalize_script_filename(script_file), shot_id)
    generation = 0
    old_bytes: bytes | None = None
    entered = False
    try:
        with project_change_source("webui"), manager.locked_script(project_name, script_file) as script:
            old_bytes = target.read_bytes() if target.exists() else None
            items, id_field, _, _, _ = get_storyboard_items(script)
            matched = find_storyboard_item(items, id_field, shot_id)
            if matched is None:
                raise EndFrameError("segment_not_found", status_code=404, id=shot_id)
            generation = _advance_shot_generation(key)
            entered = True
            write_bytes_atomic(png_bytes, target)
            matched[0]["end_frame_image"] = relative
    except EndFrameError:
        raise
    except BaseException:
        if entered:
            _restore_after_persist_failure(manager, project_name, script_file, target, old_bytes, key, generation)
        raise


def _clear_snapshot_and_field(project_name: str, script_file: str, shot_id: str, target: Path) -> None:
    """Complete "empty the field + move the snapshot file into the project trash" within the script lock critical
    section, mutually exclusive with setting.

    The snapshot file is moved into the trash only after the script is written back, so if script persistence fails
    it is still in place; compensation still writes back conditionally by generation, ensuring the field does not
    point to a missing file after rollback. Old bytes must be read after acquiring the script lock, for the same
    reason as `_write_snapshot_and_field`.

    在剧本锁临界区内完成「置空字段 + 把快照文件移进项目回收站」，与设置操作互斥。

    快照文件在剧本写回之后才移进回收站，剧本持久化失败时它仍在原处；补偿照旧按代次
    条件回写，保证字段回滚后不指向缺失的文件。读旧字节须在取得剧本锁之后，理由同
    `_write_snapshot_and_field`。
    """
    manager = get_project_manager()
    key = (project_name, ProjectManager.normalize_script_filename(script_file), shot_id)
    generation = 0
    old_bytes: bytes | None = None
    entered = False
    trash = False

    def prepare_trash(script: dict) -> Callable[[Path], None] | None:
        # The deletion record is created after the script write-back, inside the same project lock: if any step fails,
        # the script is rolled back and the snapshot stays in place.
        # 删除记录在剧本写回之后、同一把项目锁内建立：任一步失败时剧本回滚、快照留在原处。
        if not trash:
            return None

        def record(_output: Path) -> None:
            trash_end_frame(manager.get_project_path(project_name), key[1], shot_id, script)

        return record

    try:
        with (
            project_change_source("webui"),
            manager.locked_script(project_name, script_file, prepare_on_commit=prepare_trash) as script,
        ):
            old_bytes = target.read_bytes() if target.exists() else None
            items, id_field, _, _, _ = get_storyboard_items(script)
            matched = find_storyboard_item(items, id_field, shot_id)
            if matched is None:
                raise EndFrameError("segment_not_found", status_code=404, id=shot_id)
            generation = _advance_shot_generation(key)
            entered = True
            matched[0]["end_frame_image"] = None
            trash = target.is_file() and not target.is_symlink()
    except EndFrameError:
        raise
    except BaseException:
        if entered:
            _restore_after_persist_failure(manager, project_name, script_file, target, old_bytes, key, generation)
        raise


def read_project_image(project_path: Path, source_path: str) -> bytes:
    """Read the bytes of an existing image in the project; raise a domain error when the path is out of bounds, the
    file is missing or over the limit.

    The source subdirectory is not restricted: storyboard images / characters / scenes / grid crops can all be
    chosen directly; out-of-bounds protection is handled uniformly by ``safe_join`` (same rule as data_validator's
    path field validation).

    Reading is bounded by the same limit as the upload channel (``UPLOAD_IMAGE_MAX_BYTES``) rather than reading
    everything into memory, so a legitimately existing large file in the project (e.g. a video) chosen as the image
    source does not cause unbounded memory use.

    读取项目内已有图片的字节；路径越界、文件缺失或超限时抛领域错误。

    不限定来源子目录——分镜图 / 角色 / 场景 / 宫格切图都可直接选用，越界防护
    由 ``safe_join`` 统一负责（与 data_validator 的路径字段校验同口径）。

    读取按上传通道同一上限（``UPLOAD_IMAGE_MAX_BYTES``）有界读取，不整读入内存——
    项目内合法存在的大文件（如视频）被当作选图源时不至于无界内存占用。
    """
    normalized = source_path.strip().replace("\\", "/")
    if not normalized:
        raise EndFrameError("invalid_end_frame_source", path=source_path)
    try:
        resolved = safe_join(project_path, normalized)
    except PathTraversalError as exc:
        raise EndFrameError("invalid_end_frame_source", path=source_path) from exc
    if not resolved.is_file():
        raise EndFrameError("end_frame_source_not_found", status_code=404, path=normalized)
    with resolved.open("rb") as f:
        content = f.read(UPLOAD_IMAGE_MAX_BYTES + 1)
    if len(content) > UPLOAD_IMAGE_MAX_BYTES:
        raise EndFrameError(
            "end_frame_source_too_large", max_mb=UPLOAD_IMAGE_MAX_BYTES // (1024 * 1024), path=normalized
        )
    return content


async def _apply_snapshot(
    *,
    project_path: Path,
    project_name: str,
    script_file: str,
    shot_id: str,
    content: bytes,
) -> str:
    """Shared landing point of the two setting channels: normalize to PNG -> write the fixed path -> write back the
    field; returns the relative path.

    Changing the image overwrites the same path in place: the field value is unchanged, and the frontend cache-busts
    by the asset fingerprint.

    两条设置通道的共同落点：归一为 PNG → 写固定路径 → 写回字段，返回相对路径。

    换图即原地覆盖同一路径：字段值不变，前端靠资产指纹 cache-bust。
    """
    target, relative = _snapshot_target(project_path, shot_id)

    try:
        png_bytes = await asyncio.to_thread(normalize_storyboard_upload, content)
    except ValueError as exc:
        raise EndFrameError("invalid_image_file") from exc

    await run_sync_transaction(
        _write_snapshot_and_field, project_name, script_file, shot_id, target, png_bytes, relative
    )
    return relative


async def set_end_frame_from_bytes(
    *,
    project_name: str,
    script_file: str,
    shot_id: str,
    content: bytes,
) -> str:
    """Upload channel: save the uploaded image bytes as this shot's end frame snapshot.

    上传通道：把上传的图片字节落成该分镜的尾帧快照。"""
    project_path = await run_sync_transaction(_locate_shot, project_name, script_file, shot_id)
    return await _apply_snapshot(
        project_path=project_path,
        project_name=project_name,
        script_file=script_file,
        shot_id=shot_id,
        content=content,
    )


async def set_end_frame_from_project_image(
    *,
    project_name: str,
    script_file: str,
    shot_id: str,
    source_path: str,
) -> str:
    """In-project image selection channel: read the source image bytes, then go through exactly the same normalization
    + snapshot landing as upload.

    项目内选图通道：读源图字节后走与上传完全相同的归一 + 快照落点。"""
    project_path = await run_sync_transaction(_locate_shot, project_name, script_file, shot_id)
    content = await asyncio.to_thread(read_project_image, project_path, source_path)
    return await _apply_snapshot(
        project_path=project_path,
        project_name=project_name,
        script_file=script_file,
        shot_id=shot_id,
        content=content,
    )


async def clear_end_frame(*, project_name: str, script_file: str, shot_id: str) -> None:
    """Clear a shot's end frame: within the same script lock critical section, empty the field and move the snapshot
    file into the project trash, mutually exclusive with setting.

    清除分镜尾帧：在同一剧本锁临界区内把字段置空并把快照文件移进项目回收站，与设置操作互斥。"""
    project_path = await run_sync_transaction(_locate_shot, project_name, script_file, shot_id)
    target, _ = _snapshot_target(project_path, shot_id)
    await run_sync_transaction(_clear_snapshot_and_field, project_name, script_file, shot_id, target)
