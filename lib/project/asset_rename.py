"""Pure-function layer of cascading asset rename: script/draft reference rewriting, associated-file move planning and
the result report.

Assets use their name as identity (see docs/adr/0057), so a rename is a cascading transaction: the asset bucket
key, name references in all episode scripts and script_plan drafts (reference arrays / speaker / ``@[name]``
mentions), and associated files named after it (invariant "file stem = asset name") must all change at once. This
module holds the **side-effect-free** parts -- reference rewriting (edit dicts in place, return rewrite counts) and
file move planning (return a list of (src, dst)) -- so ProjectManager's orchestration entry point can scan first
(dry-run preview and execution share the same scan) and then write, under the lock. The reference preview before
deleting an asset (:class:`AssetDeletionPreview`) reuses the same scan.

Name references are always compared in comparison coordinates (strip + NFC, see
:func:`lib.project.asset_types.asset_name_comparison_key`): body text and reference arrays may carry surrounding
whitespace or be stored as NFD, and byte comparison would miss references to the same asset. File stems still only
use NFC, to avoid changing path semantics.

资产级联重命名的纯函数层：剧本/草稿引用改写、关联文件迁移规划与结果报告。

资产以 name 为身份（见 docs/adr/0057），重命名是一次级联事务：资产桶 key、全部剧集剧本
与 script_plan 草稿里的名称引用（引用数组 / speaker / ``@[名称]`` mention）、按名命名的关联文件
（不变式「文件 stem = 资产名」）须一次改齐。本模块承载其中**无副作用**的部分——引用改写
（就地改 dict、返回改写数）与文件迁移规划（返回 (src, dst) 列表）——供 ProjectManager 的
编排入口在锁内先扫描（dry-run 预览与执行共用同一套扫描）再落盘。删除资产前的引用预览
（:class:`AssetDeletionPreview`）复用同一套扫描。

名字引用判等一律走比对坐标系（strip + NFC，见
:func:`lib.project.asset_types.asset_name_comparison_key`）：正文与引用数组可能带两端空白或以 NFD
形式存量落盘，按字节比对会漏改同一资产的引用。文件 stem 仍只做 NFC，避免改变路径语义。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from lib.project.asset_types import (
    ASSET_SPECS,
    AssetSpec,
    asset_name_comparison_key,
    normalize_asset_name,
    rekey_equivalent_entries,
)
from lib.references.reference_catalog import renamed_reference, split_derivative_reference
from lib.script.grid.models import ReferenceImage
from lib.script.reference_video.text_parser import rewrite_mentions
from lib.script.script_references import iter_reference_sites


class AssetRenameNotFoundError(KeyError):
    """The old name doesn't exist in the asset bucket. The message includes a recovery hint (the previous rename may
    already have succeeded).

    旧名在资产桶中不存在。message 含恢复导向提示（可能上次重命名已成功）。"""


class AssetRenameConflictError(ValueError):
    """The target name conflicts with an existing same-type asset under normalization; rejected as a whole, nothing
    written.

    目标名与既有同类型资产归一化判定冲突，整体拒绝、不落盘。"""

    def __init__(self, conflict_name: str):
        super().__init__(
            f"Target name conflicts with existing asset {conflict_name!r} (NFC-normalized); "
            "choose another name or resolve that asset first"
        )
        self.conflict_name = conflict_name


class AssetRenameFileCollisionError(ValueError):
    """A move target file is already taken; rejected as a whole, nothing written.

    The occupant is an orphan file without a matching asset (when there is an asset, :class:`AssetRenameConflictError`
    stops it first), and the ``os.replace`` at write time would silently destroy it.

    迁移目标文件已被占用，整体拒绝、不落盘。

    占用者是无对应资产的孤儿文件（有资产时先被 :class:`AssetRenameConflictError` 拦下），
    落盘处的 ``os.replace`` 会静默销毁它。
    """

    def __init__(self, destination: Path):
        super().__init__(f"Migration target file already exists: {destination}; remove it or choose another name")
        self.destination = destination


class AssetRenameHistoryCollisionError(ValueError):
    """Retained version history already exists under the new name; rejected as a whole, nothing written.

    Earlier asset deletions only removed the asset bucket key, leaving version records and snapshots in the project:
    moving over them would overwrite that history irrecoverably. Same basis as orphan files -- better to fail loud and
    let the user deal with it first than to destroy silently.

    新名下已有保留的版本历史，整体拒绝、不落盘。

    早先的资产删除只删资产桶 key，版本记录与快照留在项目里：迁移过去会覆盖这份
    历史且不可恢复。与孤儿文件同口径——宁可 fail loud 让用户先处理，也不静默销毁。
    """

    def __init__(self, resource_id: str):
        super().__init__(
            f"New name {resource_id!r} already has retained version history; clear it or choose another name"
        )
        self.resource_id = resource_id


@dataclass(frozen=True)
class AssetRenameReport:
    """Impact report of a cascading rename. The dry-run preview and the actual run share the same scan, so the numbers
    always match.

    级联重命名的影响报告。dry-run 预览与实际执行共用同一次扫描，数字必然一致。"""

    table: str
    old_name: str
    new_name: str
    episodes: int
    references: int
    files: int
    dry_run: bool


@dataclass(frozen=True)
class AssetEpisodeReferences:
    """Number of references to an asset in one episode. / 一集里指向某资产的引用处数。"""

    episode: int
    references: int


@dataclass(frozen=True)
class AssetDeletionPreview:
    """Reference preview before deleting an asset: the same scan as rename; after deletion these references dangle.

    ``references`` is the total number of references, matching the rename preview for the same asset; ``episodes``
    lists, by episode number ascending, the part that can be attributed to an episode.

    删除资产前的引用预览：与重命名同一套扫描，删除后这些引用会悬空。

    ``references`` 是全部引用处数，与同一资产的重命名预览一致；``episodes`` 按集号升序列出
    其中能归到某一集的部分。
    """

    table: str
    name: str
    references: int
    episodes: tuple[AssetEpisodeReferences, ...]


#: "Name list" reference fields of each asset type in the script/draft skeleton. Only str elements in the list are
#: name references -- drama's top-level ``scenes`` is a list of shot dicts, same key but different shape from the
#: scene-name list in narration shots; element type is enough to tell them apart, no skeleton special case needed.
#: 各资产类型在剧本/草稿骨架里的「名称列表」引用字段。列表内只有 str 元素才是名称引用——
#: drama 顶层 ``scenes`` 是分镜 dict 列表，与 narration 分镜里的场景名列表同 key 不同形，
#: 按元素类型即可区分，无需骨架特例。
_LIST_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    asset_type: frozenset(spec.reference_list_fields) for asset_type, spec in ASSET_SPECS.items()
}


def rewrite_payload_references(payload: dict, asset_type: str, old_name: str, new_name: str) -> int:
    """Rewrite name references to *old_name* in a script/draft payload to *new_name* in place; return the rewrite count.

    The set of locations is defined by :func:`lib.script.script_references.iter_reference_sites` (reference arrays,
    the ``speaker`` field, ``@[name]`` marks in unit body text and shot visual descriptions); ``character`` additionally
    has the top-level ``characters`` mirror dict embedded in legacy scripts, which needs re-keying + syncing path
    fields.

    *old_name* may be of the form ``角色/衍生`` (character/derivative; derivative rename): matching is decided uniformly
    by :func:`lib.references.reference_catalog.renamed_reference` -- a base look rename also rewrites all derivative
    references under it, a derivative rename touches only that character's set. ``speaker`` carries only base names,
    so a derivative rename naturally doesn't match it.

    Only recognizes the skeleton structure, doesn't validate semantics: structural validation is backstopped by the
    "no worse" guard of the unified write entry point.

    就地把剧本/草稿 payload 中指向 *old_name* 的名称引用改写为 *new_name*，返回改写数。

    落点集合由 :func:`lib.script.script_references.iter_reference_sites` 定义（引用数组、
    ``speaker`` 字段、单元正文与分镜画面描述里的 ``@[名称]`` 记号）；``character`` 另有旧式剧本内嵌的顶层
    ``characters`` 镜像 dict 要 re-key + 同步路径字段。

    *old_name* 可以是 ``角色/衍生`` 形态（衍生改名）：命中判定由
    :func:`lib.references.reference_catalog.renamed_reference` 统一给出——本体改名连带改写其下全部
    衍生引用，衍生改名只动该角色的那一套。``speaker`` 只承载本体名，故衍生改名扫到它时
    自然不命中。

    只识别骨架结构、不校验语义：结构校验由写盘统一入口的「不更坏」守卫兜底。
    """
    list_fields = _LIST_FIELDS_BY_TYPE[asset_type]
    count = 0

    if asset_type == "character" and not split_derivative_reference(asset_name_comparison_key(old_name))[1]:
        # Legacy scripts embed a mirror of the character table in the top-level characters dict (the update_character_sheet
        # write path); re-key it and sync its sheet path field so the old name doesn't linger as a mirror. A derivative
        # rename doesn't touch the base look's key.
        # 旧式剧本把角色表镜像内嵌在顶层 characters dict（update_character_sheet 写入路径），
        # re-key 并同步其中的 sheet 路径字段，避免旧名以镜像形式残留。衍生改名不动本体的 key。
        embedded = payload.get("characters")
        if isinstance(embedded, dict):
            # Absorb unconditionally: when the winning key is already the new name (a rename that only changes encoding form),
            # the other equivalent key still carries the old path, so it can't be skipped just because the winning key looks
            # unchanged. The count looks at whether the key really changed.
            # 无条件收编：胜出 key 恰好已是新名（纯改编码形式的改名）时，另一条等价 key
            # 仍带着旧路径，不能因为胜出 key 看起来没变就跳过。计数看 key 是否真的变了。
            keys_before = list(embedded)
            entry = rekey_equivalent_entries(embedded, old_name, new_name)
            if entry is not None:
                if list(embedded) != keys_before:
                    count += 1
                if isinstance(entry, dict):
                    count += rewrite_entry_paths(entry, ASSET_SPECS[asset_type], old_name, new_name)

    for kind, value, write in iter_reference_sites(payload, list_fields, with_speaker=asset_type == "character"):
        if kind in ("text", "scene"):
            rewritten, changes = rewrite_mentions(value, old_name, new_name)
            if changes:
                write(rewritten)
                count += changes
            continue
        renamed = renamed_reference(value, old_name, new_name)
        if renamed is not None and renamed != value:
            write(renamed)
            count += 1
    return count


def rewrite_grid_reference_images(
    references: list[ReferenceImage],
    asset_type: str,
    old_name: str,
    new_name: str,
    path_rewrites: Mapping[str, str],
) -> int:
    """Rewrite entries pointing to *old_name* in the reference images frozen in grid records to *new_name* in place;
    return the rewrite count.

    A grid record freezes the reference images actually sent at generation time (path, name, type), and the basis of
    the grid and its grid-split shots is replayed from it; if a rename moves the sheet without updating the record,
    replay can't read the image and the grid is wrongly judged stale. Names are matched by :func:`renamed_reference`
    (base look renames include derivatives); paths are rewritten only when they hit *path_rewrites* (files actually
    moved by this rename, keys and values both project-relative paths) -- files not named after the asset aren't moved,
    so their original paths stay valid.

    就地把宫格记录冻结的参考图里指向 *old_name* 的条目改写为 *new_name*，返回改写数。

    宫格记录冻结的是生成那一刻实际发出的参考图（路径、名字、类型），宫格与切格分镜的依据
    按它重放；改名搬走了资产图却不改记录，重放就读不到图，宫格因此误判过期。名字按
    :func:`renamed_reference` 判定（本体改名连带衍生），路径只在命中 *path_rewrites*
    （本次改名实际迁移的文件，键与值都是项目相对路径）时改写——不是按名命名的文件不会被搬走，
    原路径仍然有效。
    """
    count = 0
    for reference in references:
        if reference.ref_type != asset_type:
            continue
        renamed = renamed_reference(reference.name, old_name, new_name)
        if renamed is None:
            continue
        path = path_rewrites.get(unicodedata.normalize("NFC", reference.path), reference.path)
        if renamed != reference.name or path != reference.path:
            reference.name = renamed
            reference.path = path
            count += 1
    return count


def renamed_file_stem(stem: str, old_name: str, new_name: str, *, allow_sequence: bool = False) -> str | None:
    """Derive the renamed stem from the "file stem = asset name" invariant; return None when unrelated to the old name.

    By default only accepts a stem that, after normalization, **exactly equals** the old name (sheet / reference image
    / reference audio). ``allow_sequence`` means all files at that location are mechanically named ``name_{seq}``
    (product multi-image sequences, see the sequenced naming in server/routers/files.py); then it **only** accepts
    ``oldname_{seq}``, no longer files whose stem exactly equals the old name: asset names themselves may contain an
    underscore plus digits (``validate_asset_name`` doesn't forbid it), so when products ``Cup_1`` and ``Cup`` coexist,
    the stem of ``Cup``'s first original image ``Cup_1.png`` exactly equals ``Cup_1``, and exact matching would treat
    it as ``Cup_1``'s own file. Don't enable ``allow_sequence`` for ordinary directories, otherwise a sibling asset
    "oldname_2"'s files would be swept along and its path fields left dangling.

    Comparison and joining are both done in normalized coordinates -- file names stored on macOS may be NFD.

    按「文件 stem = 资产名」不变式推导改名后的 stem；与旧名无关时返回 None。

    默认只认 stem 归一后**精确等于**旧名（sheet / 参考图 / 参考音频）。``allow_sequence``
    表示该位置的文件全部按 ``名_{序号}`` 机械命名（product 多图序列，见
    server/routers/files.py 的 sequenced 命名），此时**只**认 ``旧名_{序号}``，不再认 stem
    恰好等于旧名的文件：资产名本身允许下划线加数字（``validate_asset_name`` 不禁），
    商品 ``Cup_1`` 与 ``Cup`` 并存时，``Cup`` 的第 1 张原图 ``Cup_1.png`` 的 stem 恰好等于
    ``Cup_1``，按精确同名认会把它当成 ``Cup_1`` 自己的文件。普通目录不要开 ``allow_sequence``，
    否则兄弟资产「旧名_2」的文件会被一并卷走、路径字段改成悬空。

    比对与拼接都在归一坐标系上进行——macOS 落盘的文件名可能是 NFD。
    """
    normalized = normalize_asset_name(stem)
    target = normalize_asset_name(old_name)
    if not allow_sequence:
        return new_name if normalized == target else None
    prefix = f"{target}_"
    if normalized.startswith(prefix) and normalized[len(prefix) :].isdigit():
        return f"{new_name}_{normalized[len(prefix) :]}"
    return None


def renamed_relpath(rel: str, old_name: str, new_name: str, *, allow_sequence: bool = False) -> str | None:
    """Change the file stem in a path field value from the old name to the new name; return None when the stem is
    unrelated to the old name.

    把路径字段值中的文件 stem 从旧名改为新名；stem 与旧名无关时返回 None。"""
    path = PurePosixPath(rel.replace("\\", "/"))
    new_stem = renamed_file_stem(path.stem, old_name, new_name, allow_sequence=allow_sequence)
    if new_stem is None:
        return None
    return str(path.with_name(new_stem + path.suffix))


def rewrite_entry_paths(entry: dict, spec: AssetSpec, old_name: str, new_name: str) -> int:
    """Sync the name-based path fields in an asset entry in place (sheet / reference image / reference audio / multi-image
    sequence); return the rewrite count.

    The rewrite scope is aligned dimension by dimension with the move scope of :func:`plan_asset_file_renames`, both
    using the same predicate: only touch values directly in the asset type's directory or its ``refs`` /
    ``refs_audio`` subdirectories whose stem matches the old name; the ``oldname_{seq}`` form is allowed only in the
    ``refs`` subdirectory of multi-image sequence assets, exact name everywhere else. Paths the user manually pointed
    elsewhere (e.g. ``thumbnails/oldname.png``) are not touched -- files there are outside the move scope, and changing
    the field would blank a previously valid reference.

    就地同步资产 entry 内按名命名的路径字段（sheet / 参考图 / 参考音频 / 多图序列），返回改写数。

    改写范围与 :func:`plan_asset_file_renames` 的迁移范围逐维对齐，两侧用同一个谓词：只动
    落在该资产类型目录本级及其 ``refs`` / ``refs_audio`` 子目录下、且 stem 命中旧名的值；
    ``旧名_{序号}`` 形态只在多图序列资产的 ``refs`` 子目录放行，其余位置一律精确同名。
    用户手动指到别处的路径（如 ``thumbnails/旧名.png``）不动——那里的文件不在迁移范围内，
    改了字段就会把一条原本有效的引用指成空。
    """
    base = PurePosixPath(spec.subdir)
    migrated_dirs = {base, base / "refs", base / "refs_audio"}
    sequenced_refs = "reference_images" in spec.extra_list_fields

    def rewrite(value: str, *, sequenced_field: bool = False) -> str | None:
        parent = PurePosixPath(value.replace("\\", "/")).parent
        if parent not in migrated_dirs:
            return None
        allow_sequence = sequenced_field and sequenced_refs and parent == base / "refs"
        renamed = renamed_relpath(value, old_name, new_name, allow_sequence=allow_sequence)
        return renamed if renamed != value else None

    count = 0
    for field in (spec.sheet_field, "reference_image", "reference_audio"):
        value = entry.get(field)
        if isinstance(value, str) and value:
            renamed = rewrite(value)
            if renamed is not None:
                entry[field] = renamed
                count += 1
    images = entry.get("reference_images")
    if isinstance(images, list):
        for i, value in enumerate(images):
            if isinstance(value, str) and value:
                renamed = rewrite(value, sequenced_field=True)
                if renamed is not None:
                    images[i] = renamed
                    count += 1
    return count


def _named_file_layout(spec: AssetSpec) -> tuple[tuple[PurePosixPath, bool], ...]:
    """Directories (project-relative) holding name-based asset files, and whether each allows the ``name_{seq}`` form.

    按名命名的资产文件所在的目录（项目相对），及该目录是否放行 ``名字_{序号}`` 形态。"""
    base = PurePosixPath(spec.subdir)
    sequenced_refs = "reference_images" in spec.extra_list_fields
    return ((base, False), (base / "refs", sequenced_refs), (base / "refs_audio", False))


def _named_file_dirs(project_dir: Path, spec: AssetSpec) -> list[tuple[Path, bool]]:
    candidates = ((project_dir.joinpath(*directory.parts), allow) for directory, allow in _named_file_layout(spec))
    return [(directory, allow_sequence) for directory, allow_sequence in candidates if directory.is_dir()]


#: Path fields in an asset entry registering uploaded originals (reference images, reference audio, multi-image
#: sequences), owned by name.
#: 资产条目里登记上传原件的路径字段（参考图、参考音频、多图序列），按名归属。
REFERENCE_FILE_FIELDS = ("reference_image", "reference_audio", "reference_images")


def named_file_fields(spec: AssetSpec) -> tuple[str, ...]:
    """Name-owned file fields in entries of this type: the sheet field, plus the reference file fields registered for this
    type.

    该类型条目里按名归属的文件字段：资产图字段，与该类型登记的参考文件字段。"""
    return (
        spec.sheet_field,
        *(
            field
            for field in REFERENCE_FILE_FIELDS
            if field in spec.extra_string_fields or field in spec.extra_list_fields
        ),
    )


def owns_named_file(spec: AssetSpec, name: str, rel: str) -> bool:
    """Project-relative path *rel* is a file under *name*; same criterion as :func:`asset_files_named`.

    Sheets, reference images and reference audio are owned by name and not shared across assets: rename only moves,
    delete only takes, and restore only adds back files under the asset's own name, so fields pointing elsewhere may
    dangle after these operations; writes therefore only accept paths under the asset's own name.

    项目相对路径 *rel* 是 *name* 名下的文件，与 :func:`asset_files_named` 同一判据。

    资产图、参考图与参考音频按名归属、不跨资产共用：改名只迁移、删除只带走、恢复只加回本资产名下的文件，
    指向别处的字段在这些操作之后可能悬空，所以写入时只接受本资产名下的路径。
    """
    path = PurePosixPath(rel.replace("\\", "/"))
    for directory, allow_sequence in _named_file_layout(spec):
        if path.parent == directory:
            return renamed_file_stem(path.stem, name, name, allow_sequence=allow_sequence) is not None
    return False


def asset_files_named(project_dir: Path, spec: AssetSpec, name: str) -> list[Path]:
    """Files named *name* in this asset type's storage directory (sheet, original image, reference audio); same scope as
    the rename move.

    该资产类型落盘目录里以 *name* 命名的文件（资产图、原图、参考音频），与改名迁移同一范围。"""
    return [
        file
        for directory, allow_sequence in _named_file_dirs(project_dir, spec)
        for file in sorted(directory.iterdir())
        if file.is_file() and renamed_file_stem(file.stem, name, name, allow_sequence=allow_sequence) is not None
    ]


def plan_asset_file_renames(
    project_dir: Path, spec: AssetSpec, old_name: str, new_name: str
) -> list[tuple[Path, Path]]:
    """Scan this asset type's storage directory, plan moves for files whose stem matches the old name, and return a list
    of ``(src, dst)``.

    Covers the sheet directory itself and its upload subdirectories (``refs`` / ``refs_audio``); version snapshots are
    moved separately by VersionManager. Scans by directory rather than trusting only the entry's path fields:
    generation intermediates may already be on disk under the old name with the field not yet written, and old-name
    files shouldn't linger.

    Hidden files are not excluded by file-name prefix: matching is decided only by whether the stem equals the asset
    name, which already excludes clutter (``.DS_Store``'s stem is ``.DS_Store``, atomic-write temp files look like
    ``tmpXXXX.tmp``), while a leading dot is a valid asset name; skipping them wholesale would leave stale path fields
    after renaming an asset named ``.甲``.

    The ``oldname_{seq}`` form is allowed only in the ``refs`` subdirectory of multi-image sequence assets (entry has
    ``reference_images``) -- file names there are generated mechanically by sequence number on upload and can't be
    another asset's name; all other directories require an exact name, otherwise a sibling asset "oldname_2"'s sheet
    would be swept along.

    Raises :class:`AssetRenameFileCollisionError` when a target is taken: planning precedes any write, so it rejects as
    a whole with nothing written. Occupancy has two sources, both must be stopped: orphan files already on disk, and two
    source files in the same batch (e.g. same-named files in NFC / NFD encodings) hitting the same target -- if the
    latter were allowed, the second ``os.replace`` would eat the result of the first.

    On case-insensitive or normalization-insensitive file systems (APFS / NTFS), a rename changing only case or only
    encoding form makes the target resolve back to the source file itself; that isn't occupancy and is exempted via
    ``samefile``.

    This doesn't affect "rerun after mid-way failure converges" -- a completed move's ``src`` no longer exists, so the
    scan won't plan it again.

    Raises:
        AssetRenameFileCollisionError: a move target path is already taken by someone else or by another move in the
            same batch.

    扫描该资产类型的落盘目录，规划 stem 命中旧名的文件迁移，返回 ``(src, dst)`` 列表。

    覆盖资产图目录本级与其上传子目录（``refs`` / ``refs_audio``），版本快照另由
    VersionManager 迁移。按目录扫描而非只信 entry 路径字段：生成中间产物可能已按旧名
    落盘而字段未写，旧名文件不应残留。

    不按文件名前缀排除隐藏文件：命中与否只由 stem 是否等于资产名决定，这已足以排除杂物
    （``.DS_Store`` 的 stem 就是 ``.DS_Store``，原子写的临时文件形如 ``tmpXXXX.tmp``），
    而前导点是合法资产名，一刀切跳过会让名为 ``.甲`` 的资产改名后留下失效的路径字段。

    ``旧名_{序号}`` 形态只在多图序列资产（entry 带 ``reference_images``）的 ``refs``
    子目录放行——那里的文件名由上传侧按序号机械生成，不会是别的资产的名字；其余目录
    一律精确同名，否则兄弟资产「旧名_2」的资产图会被一并卷走。

    目标已被占用时抛 :class:`AssetRenameFileCollisionError`：规划早于任何写入，因此整体
    拒绝、不落盘。占用有两种来源，都要拦：磁盘上已有的孤儿文件，以及同批两个源文件（如
    NFC / NFD 两种编码的同名文件）撞到同一个目标——后者若放行，第二次 ``os.replace``
    会吃掉第一次的成果。

    大小写不敏感或归一化不敏感的文件系统（APFS / NTFS）上，仅改大小写或仅改编码形式的
    改名会让目标解析回源文件自身，那不是占用，按 ``samefile`` 豁免。

    这不影响「中途失败重跑收敛」——已完成的迁移其 ``src`` 已不存在，扫描不会再把它规划进来。

    Raises:
        AssetRenameFileCollisionError: 某个迁移目标路径已被他人占用或被同批另一次迁移占用。
    """
    moves: list[tuple[Path, Path]] = []
    planned: set[Path] = set()
    for directory, allow_sequence in _named_file_dirs(project_dir, spec):
        for file in sorted(directory.iterdir()):
            if not file.is_file():
                continue
            new_stem = renamed_file_stem(file.stem, old_name, new_name, allow_sequence=allow_sequence)
            if new_stem is not None and file.stem != new_stem:
                destination = file.with_name(new_stem + file.suffix)
                if destination in planned:
                    raise AssetRenameFileCollisionError(destination)
                if destination.exists() and not destination.samefile(file):
                    raise AssetRenameFileCollisionError(destination)
                planned.add(destination)
                moves.append((file, destination))
    return moves


def foreign_named_file_field(
    spec: AssetSpec, name: str, entry: Mapping[str, object], values: Mapping[str, object]
) -> str | None:
    """If any path newly written into *values*' name-owned file fields is not under *name*, return the first such field;
    ``None`` if all are.

    Only looks at newly written values: values already in the entry committed unchanged don't count as new writes.

    *values* 新写进按名归属的文件字段的路径里有不在 *name* 名下的，返回第一个这样的字段；都在则为 ``None``。

    只看新写入的值：条目里已有的值原样提交不算新写入。
    """
    for field in named_file_fields(spec):
        value, current = values.get(field), entry.get(field)
        written = value if isinstance(value, list) else [value]
        kept = current if isinstance(current, list) else [current]
        if any(
            isinstance(item, str) and item and item not in kept and not owns_named_file(spec, name, item)
            for item in written
        ):
            return field
    return None


__all__ = [
    "REFERENCE_FILE_FIELDS",
    "AssetRenameConflictError",
    "AssetRenameFileCollisionError",
    "AssetRenameHistoryCollisionError",
    "AssetRenameNotFoundError",
    "AssetRenameReport",
    "asset_files_named",
    "foreign_named_file_field",
    "named_file_fields",
    "owns_named_file",
    "plan_asset_file_renames",
    "renamed_file_stem",
    "renamed_relpath",
    "rewrite_entry_paths",
    "rewrite_grid_reference_images",
    "rewrite_payload_references",
]
