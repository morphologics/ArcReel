"""Path safety utilities: the single validation entry in the project for "a path must lie within some base dir".

The implementation deliberately uses ``os.path.realpath`` + ``startswith`` prefix comparison rather than
``Path.resolve()`` with ``relative_to()`` / ``is_relative_to()``: both defend equally (``realpath`` also expands
symlinks), but prefix comparison is the sanitizer pattern CodeQL ``py/path-injection`` recognizes, while the latter is
not recognized and keeps producing standing alerts.

Return values are always built from ``realpath`` output, not the raw path the caller passed, so tainted values don't
keep flowing out.

路径安全工具：项目内「路径必须落在某个基准目录之内」的唯一校验入口。

实现刻意使用 ``os.path.realpath`` + ``startswith`` 前缀比较，而不是 ``Path.resolve()``
配合 ``relative_to()`` / ``is_relative_to()``：两者防御强度等价（``realpath`` 同样展开
symlink），但前缀比较是 CodeQL ``py/path-injection`` 能识别的 sanitizer 收敛模式，后者
不被识别、会持续产生存量告警。

返回值一律由 ``realpath`` 的输出构造，而非调用方传入的原始路径，避免污染值继续外流。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "PathTraversalError",
    "safe_exists",
    "safe_join",
    "safe_resolve",
    "try_safe_join",
]


class PathTraversalError(ValueError):
    """The resolved path escaped the base dir.

    Subclasses ``ValueError``, so existing ``except ValueError`` call sites catch it, while call sites that need to tell
    "out of bounds" from "other invalid input" can catch it precisely.

    解析后的路径逃出了基准目录。

    继承 ``ValueError``，既能被 ``except ValueError`` 的既有调用点接住，也允许需要区分
    「越界」与「其它非法入参」的调用点精确捕获。
    """


def _realpath(value: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.fspath(value))


def safe_join(
    base: str | os.PathLike[str],
    *parts: str | os.PathLike[str],
    allow_base: bool = False,
    must_exist: bool = False,
    require_file: bool = False,
) -> Path:
    """Join untrusted ``parts`` under ``base``, check it stays within bounds and return the normalized absolute path.

    An absolute path in ``parts`` drops the prefix per ``os.path.join`` semantics and must still pass the containment
    check, so "passing an absolute path" equals "checking whether that absolute path is inside base".

    Args:
        base: base dir; the result must lie within it.
        parts: path fragments to join, usually from user input or untrusted data on disk.
        allow_base: whether a result exactly equal to ``base`` itself passes. Default False.
        must_exist: when True the result must already exist (file or dir), otherwise raises ``FileNotFoundError``.
        require_file: when True the result must be an existing **file**, otherwise raises ``FileNotFoundError``.

    Raises:
        PathTraversalError: the resolved result is not within ``base``, or ``parts`` contains file-system-illegal
            characters that make ``os.path.realpath`` fail (e.g. an embedded NUL byte).
        FileNotFoundError: the ``must_exist`` / ``require_file`` check failed.
        TypeError: ``parts`` contains non-path types (e.g. dirty data in project.json).

    把不可信的 ``parts`` 拼到 ``base`` 下，校验未越界后返回规范化的绝对路径。

    ``parts`` 中出现绝对路径时按 ``os.path.join`` 语义丢弃前缀，随后仍要通过包含校验，
    因此「传入绝对路径」等价于「校验该绝对路径是否在 base 内」。

    Args:
        base: 基准目录，结果必须落在其内。
        parts: 待拼接的路径片段，通常来自用户输入或磁盘上的不可信数据。
        allow_base: 拼接结果恰等于 ``base`` 本身时是否算通过。默认 False。
        must_exist: 为 True 时结果必须已存在（文件或目录），否则抛 ``FileNotFoundError``。
        require_file: 为 True 时结果必须是已存在的**文件**，否则抛 ``FileNotFoundError``。

    Raises:
        PathTraversalError: 解析结果不在 ``base`` 内，或 ``parts`` 含文件系统非法字符
            导致 ``os.path.realpath`` 解析失败（如内嵌 NUL 字节）。
        FileNotFoundError: ``must_exist`` / ``require_file`` 校验未通过。
        TypeError: ``parts`` 含非路径类型（如 project.json 里的脏数据）。
    """
    base_real = _realpath(base)
    # Pre-append the trailing separator: when base is exactly the file system root / a Windows drive root, base_real
    # already ends with a separator (e.g. "/", "C:\\"); appending again gives "//"/"C:\\\\", so no valid subpath would
    # match the prefix and all would be misjudged as out of bounds.
    # 预先补齐末尾分隔符：base 恰为文件系统根 / Windows 盘符根时 base_real 本身已带
    # 分隔符（如 "/"、"C:\\"），再拼一次会变成 "//"/"C:\\\\"，导致任何合法子路径都
    # 无法匹配前缀而被误判越界。
    base_prefix = base_real if base_real.endswith(os.sep) else base_real + os.sep
    # os.path.join accepts PathLike args itself (it calls os.fspath internally), no pre-conversion needed; an extra
    # generator expression would only insert unnecessary intermediate nodes into CodeQL's dataflow.
    #
    # parts come from untrusted input and may contain file-system-illegal characters (e.g. an embedded NUL byte): JSON can
    # express such values but they make os.path.realpath raise a native ValueError/OSError. Callers generally only catch
    # PathTraversalError, so convert here in one place, instead of patching each call site, where misses would degrade to 500.
    # os.path.join 本身接受 PathLike 参数（内部会调 os.fspath），不需要预先转换；
    # 额外包一层生成器表达式只会在 CodeQL 的 dataflow 里插入不必要的中间节点。
    #
    # parts 来自不可信输入，可能含文件系统非法字符（如内嵌 NUL 字节）：这类值 JSON
    # 可表达但会让 os.path.realpath 直接抛出原生 ValueError/OSError。调用方普遍只捕获
    # PathTraversalError，在这里统一转换，避免每个调用点各自补漏、遗漏的会退化成 500。
    try:
        candidate_real = os.path.realpath(os.path.join(base_real, *parts))
    except (OSError, ValueError) as exc:
        raise PathTraversalError(f"路径解析失败：{parts!r}") from exc

    # Branch on candidate_real == base_real separately: when base is the file system root / a drive root,
    # base_prefix == base_real, and an equal candidate path would also pass the startswith branch below;
    # merging the allow_base check into that branch would let allow_base=False be bypassed for the root dir.
    # candidate_real == base_real 单独判支：base 为文件系统根 / 盘符根时
    # base_prefix == base_real，相等的候选路径也会通过下面的 startswith 分支，
    # 若把 allow_base 判断合并进该分支会让根目录下的 allow_base=False 被绕过。
    if candidate_real == base_real:
        if not allow_base:
            raise PathTraversalError(f"路径越界：{candidate_real!r} 不在 {base_real!r} 内")
    elif not candidate_real.startswith(base_prefix):
        raise PathTraversalError(f"路径越界：{candidate_real!r} 不在 {base_real!r} 内")

    # is_file/exists check candidate_real directly (the string carrying the barrier) rather than wrapping it in
    # Path first: a Path() wrapper would also break the recognition above.
    #
    # CodeQL still flags the two sinks below as unvalidated; they were dismissed via the GitHub code scanning UI (false
    # positive, reasons in the dismiss comment of each alert, with PR link): the bounds check logic is isomorphic to code
    # CodeQL already judged safe before this repo's migration (the bounds check in server/app.py spa_deep_link), the
    # protection strength is unchanged, and tests/unit/lib/infra/test_path_safety.py locks it with dedicated path traversal
    # cases (including symlink escape); this function, as the repo's only bounds check implementation, necessarily operates
    # on the unsanitized candidate path internally, and the flag is attributed to a recognition gap in CodeQL's sink
    # attribution after the sanitizer's own branches merge.
    # is_file/exists 直接查 candidate_real（携带 barrier 的字符串），而不是先包进
    # Path 再查：Path() 包装同样会打断上面这层识别。
    #
    # 下方两处 CodeQL 仍判定为未经校验的 sink，已通过 GitHub code scanning UI
    # dismiss（false positive，理由见对应 alert 的 dismiss comment，含 PR 链接）：
    # 越界校验逻辑与本仓库迁移前已被 CodeQL 判定安全的写法（server/app.py
    # spa_deep_link 的越界校验）同构，防护强度未变，
    # 且 tests/unit/lib/infra/test_path_safety.py 有专门的路径穿越用例（含符号链接逃逸）锁定；
    # 本函数自身作为该仓库唯一的越界校验实现，其内部操作的正是未净化的候选路径，
    # 判定为 CodeQL 对 sanitizer 自身跨分支合流后的 sink 归因存在识别缺口。
    if require_file and not os.path.isfile(candidate_real):
        raise FileNotFoundError(candidate_real)
    if must_exist and not os.path.exists(candidate_real):
        raise FileNotFoundError(candidate_real)
    return Path(candidate_real)


def try_safe_join(
    base: str | os.PathLike[str],
    *parts: str | os.PathLike[str],
    allow_base: bool = False,
    must_exist: bool = False,
    require_file: bool = False,
) -> Path | None:
    """Silent version of ``safe_join``: out of bounds / missing / dirty data all return None.

    For call sites that "skip if unavailable" rather than "reject the request" (validation summaries, candidate path
    traversal, etc.).

    ``safe_join`` 的静默版本：越界 / 不存在 / 脏数据一律返回 None。

    供「拿不到就跳过」而非「拒绝请求」的调用点使用（校验汇总、候选路径遍历等）。
    """
    try:
        return safe_join(
            base,
            *parts,
            allow_base=allow_base,
            must_exist=must_exist,
            require_file=require_file,
        )
    except (OSError, ValueError, TypeError):
        # TypeError: fragments come from raw project.json fields; dirty data (dict/int) is treated as "missing"
        # TypeError：片段来自 project.json 原始字段，脏数据（dict/int）按「不存在」处理
        return None


def safe_resolve(base: Path, rel_path: str | None) -> Path | None:
    """Resolve a relative path within base and return the absolute path; return None when out of bounds / dirty data / not an existing file.

    解析 base 内的相对路径，返回绝对路径；越界/脏数据/不是已存在的文件时返回 None。"""
    if not rel_path:
        return None
    return try_safe_join(base, rel_path, require_file=True)


def safe_exists(base: Path, rel_path: str) -> bool:
    """Whether rel_path is a valid relative path within base and the file exists (guards against path traversal).

    rel_path 是否为 base 内的合法相对路径且文件存在（防路径穿越）。"""
    return safe_resolve(base, rel_path) is not None
