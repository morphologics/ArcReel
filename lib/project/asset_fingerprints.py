"""Asset file fingerprinting -- mtime-based support for content-addressed caching / 资产文件指纹计算 — 基于 mtime 的内容寻址缓存支持"""

from pathlib import Path

# Media subdirectories to scan: aligned with the public media endpoint (``server.routers.files.PUBLIC_MEDIA_DIRS``),
# except ``renders/`` -- final cuts are addressed by version key and don't take part in cache-bust.
# 扫描的媒体子目录：与公开媒体端点（``server.routers.files.PUBLIC_MEDIA_DIRS``）对齐，
# 只差 ``renders/``——成片按版本键定址，不参与 cache-bust。
_MEDIA_SUBDIRS = (
    "storyboards",
    "end_frames",
    "videos",
    "thumbnails",
    "characters",
    "scenes",
    "props",
    "products",
    "grids",
    "reference_videos",
    "audio",
    "bgm",
)

# Known media files in the root directory (e.g. style reference image) / 根目录下的已知媒体文件（如风格参考图）
_ROOT_MEDIA_SUFFIXES = frozenset((".png", ".jpg", ".jpeg", ".webp", ".mp4"))

#: Directory name of version snapshot buckets. Snapshots live in ``versions/{subdir}/`` under the project root (see
#: ``lib.project.resource_paths.version_snapshot_dir``), so they are not in ``_MEDIA_SUBDIRS`` anyway; this skip
#: handles same-named directories inside media subdirectories. Snapshots are addressed by version number and don't
#: take part in cache-bust; scanning them is wasted work.
#: 版本快照桶的目录名。快照落在项目根的 ``versions/{subdir}/``（见
#: ``lib.project.resource_paths.version_snapshot_dir``），本就不在 ``_MEDIA_SUBDIRS`` 里；这条跳过
#: 管的是媒体子目录内的同名目录。快照按版本号定址、不参与 cache-bust，扫到只是白算。
_VERSIONS_DIR = "versions"


def _scan_media_tree(prefix: str, dir_path: Path, fingerprints: dict[str, int]) -> None:
    """Recursively scan all files under a media subdirectory, skipping ``versions/`` at any depth and directory symlinks.

    No depth limit: derivative character sheets live three levels down, e.g.
    ``characters/derivatives/{本体}/{衍生}.png``. Directory symlinks are not descended -- the link target is not the
    project's own media, and when pointing to an ancestor it would recurse until the OS symlink resolution limit;
    same basis as ``os.walk(followlinks=False)`` in ``lib.agent.profile_manifest``.

    递归扫描一个媒体子目录下的全部文件，跳过任意层级的 ``versions/`` 与目录软链。

    深度不设限：角色衍生资产图落在 ``characters/derivatives/{本体}/{衍生}.png`` 这样的第三级。
    目录软链不下探——链接目标不是项目自己的媒体，且指向祖先时会一路递归到操作系统的软链
    解析上限才停；与 ``lib.agent.profile_manifest`` 的 ``os.walk(followlinks=False)`` 同口径。
    """
    for entry in dir_path.iterdir():
        if entry.is_file():
            fingerprints[f"{prefix}/{entry.name}"] = entry.stat().st_mtime_ns
        elif entry.is_dir() and not entry.is_symlink() and entry.name != _VERSIONS_DIR:
            _scan_media_tree(f"{prefix}/{entry.name}", entry, fingerprints)


def compute_asset_fingerprints(project_path: Path) -> dict[str, int]:
    """
    Scan all media files under the project directory and return a {relative path: mtime_ns_int} mapping.

    mtime_ns is a nanosecond integer, used as the URL cache-bust parameter, more precise than seconds.
    For about 50 files this takes <1ms (reads file-system metadata only).

    扫描项目目录下所有媒体文件，返回 {相对路径: mtime_ns_int} 映射。

    mtime_ns 为纳秒级整数，用作 URL cache-bust 参数，精度高于秒级。
    对约 50 个文件，耗时 <1ms（仅读文件系统元数据）。
    """
    fingerprints: dict[str, int] = {}

    for subdir in _MEDIA_SUBDIRS:
        dir_path = project_path / subdir
        # Symlinks are skipped at this level the same way as at every level inside the tree: `is_dir()` follows symlinks,
        # so if a media subdirectory itself were a symlink, the link target's files would become this project's
        # fingerprint keys and the whole external tree would be walked.
        # 软链在这一层与树内各层同口径跳过：`is_dir()` 跟随软链，媒体子目录自身是软链时
        # 会把链接目标的文件写成本项目的指纹键并遍历整棵外部目录。
        if dir_path.is_dir() and not dir_path.is_symlink():
            _scan_media_tree(subdir, dir_path, fingerprints)

    # Media files in the root directory (e.g. style_reference.png) / 根目录下的媒体文件（如 style_reference.png）
    for f in project_path.iterdir():
        if f.is_file() and f.suffix.lower() in _ROOT_MEDIA_SUFFIXES:
            fingerprints[f.name] = f.stat().st_mtime_ns

    return fingerprints
