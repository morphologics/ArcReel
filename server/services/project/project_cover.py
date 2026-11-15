"""
Project cover picker (computed at read time).

When listing projects in the project lobby, pick a relative resource path usable as the cover, in order of
preference:

    1. First frame of a generated video, `video_thumbnail`
       -- written in the script entry's `generated_assets.video_thumbnail`: storyboard mode stores it in
          `segments` (narration) / `scenes` (drama) / `shots` (ad) by content mode, reference-to-video in
          `video_units`; the entry array key comes from the skeleton registry `SKELETONS`. All are extracted
          and saved by `lib/infra/thumbnail.extract_video_thumbnail` after generation completes.
       The asset that best represents "the project's current output progress", highest priority.

    2. Generated storyboard image `storyboard_image`
       -- second-best choice when storyboard mode has no finished video yet but has storyboard images;
          for reference-to-video this field is always None and is skipped naturally.

    3. Scene reference image `scene_sheet`
       -- the core fallback for reference-to-video: even if no video has been generated, a scene sheet
          can show the project's art style. scene > character because a sense of environment/space looks more like a
          "cover".

    4. Character reference image `character_sheet`
       -- the last fallback; only story-driven projects might lack a scene and keep a character.

    5. None -- the frontend renders the placeholder FolderOpen icon.

Follows the same "compute at read time, store no redundancy" convention as the project summary projection: no
`cover_thumbnail` field is written into project.json; each list_projects re-resolves from the current on-disk truth.

项目封面选择器（读时计算）。

在项目大厅列出项目时，按偏好顺序挑一个可用作封面的相对资源路径：

    1. 已生成视频的首帧 `video_thumbnail`
       —— 写在剧本条目的 `generated_assets.video_thumbnail`：分镜图生视频按创作类型落在
          `segments`（narration）/ `scenes`（drama）/ `shots`（ad），参考生视频落在
          `video_units`；条目数组键取自骨架注册表 `SKELETONS`。均由
          `lib/infra/thumbnail.extract_video_thumbnail` 在生成完成后抽出并落盘。
       最能代表"项目当前产出进度"的资产，优先级最高。

    2. 已生成的分镜图 `storyboard_image`
       —— 分镜图生视频还没完成视频、但已出分镜图时的次优选择；
          参考生视频该字段永远为 None，自然跳过。

    3. 场景参考图 `scene_sheet`
       —— 参考生视频核心兜底：即使一次视频都没生成，也能用一张场景资产图
          展现项目美术风格。scene > character 是因为环境/空间感更像"封面"。

    4. 角色参考图 `character_sheet`
       —— 最后兜底，仅剧情向项目才可能缺 scene 保留 character。

    5. None —— 前端渲染占位 FolderOpen 图标。

与项目摘要投影同走"读时计算、不存冗余"的约定：不往 project.json
里写 `cover_thumbnail` 字段，每次 list_projects 按当前磁盘真相重新解析。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from lib.infra.path_safety import try_safe_join
from lib.script.script_models import get_generated_assets
from lib.script.script_skeleton import SKELETONS

if TYPE_CHECKING:
    from lib.project.project_manager import ProjectManager

logger = logging.getLogger(__name__)


def resolve_project_cover(
    manager: ProjectManager,
    project_name: str,
    project: dict,
    *,
    preloaded_scripts: dict[str, dict] | None = None,
) -> str | None:
    """Pick the first usable cover path in order of preference and return a `/api/v1/files/...` URL (with ``?v=`` when
    the file exists); None if there is none.

    ``preloaded_scripts`` lets the caller (e.g. list_projects) load scripts once and feed them to the project
    summary projection as well, avoiding duplicate JSON I/O on two paths. The key is the raw
    ``episode['script_file']`` value, the value is the script JSON dict; missing episodes (key not in the map) fall
    back to ``manager.load_script``.

    按偏好顺序挑第一个可用的封面路径，返回 `/api/v1/files/...` URL（文件在场时带 ``?v=``）；全无则 None。

    ``preloaded_scripts`` 允许调用方（如 list_projects）一次性加载剧本后同时喂给
    项目摘要投影，避免两路重复 JSON I/O。key 为 ``episode['script_file']``
    原值，value 为剧本 JSON dict；缺失集 (key 不在 map) 回退到 ``manager.load_script``。
    """

    def _url(rel: str) -> str:
        # Always go through the files static route rather than building disk paths directly; the relative path is
        # passed through to FileResponse as-is.
        # When the file exists, append ?v=<mtime_ns> as the version key (same rule as the asset fingerprint), so the
        # URL changes when the cover is regenerated and the lobby can cache long-term; when the file is missing or the
        # path is invalid no version key is appended, and the files route returns 404 as is.
        # 统一走 files 静态路由，不直接拼盘路径；相对路径原样透传给 FileResponse。
        # 文件在场时附 ?v=<mtime_ns> 作版本键（与资产指纹同口径），封面被重新生成后 URL 随之变化，
        # 大厅可长期缓存；文件缺失或路径非法时不附版本键，由 files 路由按原样回 404。
        url = f"/api/v1/files/{project_name}/{rel.lstrip('/')}"
        try:
            project_dir = manager.get_project_path(project_name)
        except (OSError, ValueError):
            return url
        resolved = try_safe_join(project_dir, rel, require_file=True)
        if resolved is None:
            return url
        try:
            mtime_ns = resolved.stat().st_mtime_ns
        except OSError:
            return url
        return f"{url}?v={mtime_ns}"

    # First pass: iterate over all episodes' scripts, scanning video_thumbnail across all of them first, then
    # storyboard_image across all of them. The two are separate passes rather than checked side by side on the same
    # item, so that "any episode has a video first frame" always beats "only storyboard images" instead of being
    # locked by episode order.
    # 第一趟：遍历所有 episode 的 script，先整体扫 video_thumbnail，再整体扫
    # storyboard_image。两趟分开而不是在同一 item 上并列判断，是为了保证
    # "任何一集有视频首帧" 都胜过 "仅有分镜图"，而不是被 episode 顺序锁死。
    scripts: list[dict] = []
    for ep in project.get("episodes") or []:
        script_file = ep.get("script_file")
        if not script_file:
            continue
        if preloaded_scripts is not None and script_file in preloaded_scripts:
            scripts.append(preloaded_scripts[script_file])
            continue
        try:
            scripts.append(manager.load_script(project_name, script_file))
        except (OSError, ValueError) as e:
            # A missing / corrupted / out-of-bounds script file does not block the project list; only log at debug and
            # try other episodes.
            # OSError covers I/O failures such as FileNotFoundError; ValueError covers JSONDecodeError and the invalid
            # path check of ProjectManager._safe_subpath. Kept consistent with the outer preload except in
            # list_projects.
            # 剧本文件缺失 / 损坏 / 路径越界不阻塞项目列表；仅记 debug，继续尝试其他集。
            # OSError 覆盖 FileNotFoundError 等 I/O 失败；ValueError 覆盖 JSONDecodeError
            # 与 ProjectManager._safe_subpath 的非法路径校验。与 list_projects 的外层
            # preload except 口径保持一致。
            logger.debug("加载剧本失败 project=%s script=%s err=%s", project_name, script_file, e)
            continue

    def _iter_items(script: dict) -> list[dict]:
        # Merge entries from all skeletons registered in SKELETONS (segments / scenes / shots / video_units) rather
        # than picking just one via evidence parsing: when several keys coexist or one is an empty shell, no structure
        # may hide the real assets of another.
        # Non-list arrays and non-dict entries are skipped as missing; asset paths only accept non-empty strings, and
        # empty-shell items and dirty values are ignored together.
        # 按 SKELETONS 登记的全部骨架（segments / scenes / shots / video_units）合并条目，不经
        # 取证解析只挑一种：多种键共存或其中一方为空壳时，任一结构都不能遮蔽另一结构的真实资产。
        # 非 list 数组与非 dict 条目按缺失跳过；资产路径只认非空字符串，空壳 item 与脏值一并忽略。
        items: list[dict] = []
        for kind in SKELETONS:
            raw = script.get(kind)
            if isinstance(raw, list):
                items.extend(item for item in raw if isinstance(item, dict))
        return items

    for script in scripts:
        for item in _iter_items(script):
            thumb = get_generated_assets(item).get("video_thumbnail")
            if isinstance(thumb, str) and thumb:
                return _url(thumb)

    for script in scripts:
        for item in _iter_items(script):
            sb = get_generated_assets(item).get("storyboard_image")
            if isinstance(sb, str) and sb:
                return _url(sb)

    # Project-level assets: scene > character (see the choice in the module docstring) / 项目级资产：scene > character（见模块 docstring 的抉择）
    for data in (project.get("scenes") or {}).values():
        sheet = (data or {}).get("scene_sheet")
        if sheet:
            return _url(sheet)

    for data in (project.get("characters") or {}).values():
        sheet = (data or {}).get("character_sheet")
        if sheet:
            return _url(sheet)

    return None
