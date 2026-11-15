"""Path derivation for the two-level Agent memory directories: single source of truth for project memory, and user id validation.

Zero-I/O pure functions -- paths are derived even if directories don't exist. Session assembly (redirecting native auto
memory), the user memory store service and the two-layer fence projection of ``AgentAccessPolicy`` all get paths from
here: if the directory the fence allows and the directory actually written were each built by string concatenation,
a typo in either would make the Agent write outside the fence and be silently rejected.

- Project memory: ``<project dir>/.arcreel/memory/``
- User memory: location given by the data root layout (``DataRootLayout.user_memory_dir``)

It lives in ``.arcreel/`` rather than ``.claude/``: the latter is the profile materialization tree, and both a manifest
mismatch and "restore built-in profile" delete the whole tree.

Agent 记忆两级目录的路径派生：项目记忆的单一真相源与用户 id 校验。

零 I/O 纯函数——目录不存在也照常派生。会话装配（重定向原生 auto memory）、
用户记忆存储服务与 ``AgentAccessPolicy`` 的两层围栏投影都从这里取路径：
围栏放行的目录与实际写入的目录一旦各自拼串，任一处笔误都会让 Agent 写到
围栏外并被静默拒绝。

- 项目记忆：``<项目目录>/.arcreel/memory/``
- 用户记忆：位置由数据根布局给出（``DataRootLayout.user_memory_dir``）

落 ``.arcreel/`` 而非 ``.claude/``：后者是 profile 物化树，manifest 失配与
「恢复内置 profile」都会整树删除。
"""

from __future__ import annotations

from pathlib import Path

#: Name of ArcReel's internal state directory: the validator skips dot directories and archive export only copies visible items,
#: so memory naturally stays out of archives and is not reported as an unrecognized directory.
#: ArcReel 内部状态目录名：校验器跳过点目录、归档导出只拷贝可见项，记忆因此
#: 天然不入归档、不报未识别目录。
ARCREEL_DIRNAME = ".arcreel"

#: Memory directory name. Both levels use the same level name so "memory" is recognizable at a glance in both places.
#: 记忆目录名。两级共用同一层级名，便于「记忆」在两处一眼可辨。
MEMORY_DIRNAME = "memory"


def project_memory_dir(project_dir: Path) -> Path:
    """Project memory directory: ``<project dir>/.arcreel/memory/``. / 项目记忆目录：``<项目目录>/.arcreel/memory/``。"""
    return project_dir / ARCREEL_DIRNAME / MEMORY_DIRNAME


def is_valid_memory_user_id(user_id: str) -> bool:
    """Whether ``user_id`` can safely be used as a single path segment of the user memory directory.

    The criterion is the intersection of all platforms, not relaxed for the current platform: ``:`` is an ordinary
    character on POSIX, but on Windows joining ``<data root>/users`` with ``C:`` gives a drive-relative path (``Path``
    drops the whole left part), so the memory directory would land in that drive's current directory rather than under
    the data root. When OIDC identities (``issuer:subject`` form) are added in future they must be normalized into a
    single segment before being passed in.

    ``user_id`` 是否可安全用作用户记忆目录的单个路径段。

    判据取所有平台的交集，不按当前平台放宽：``:`` 在 POSIX 上是普通字符，在
    Windows 上让 ``<数据根>/users`` 与 ``C:`` 拼出的是驱动器相对路径
    （``Path`` 会丢掉左侧整段），记忆目录因此落到该驱动器的当前目录而不是数据根下。
    未来接 OIDC 身份（``issuer:subject`` 形态）时须先规整成单段再传进来。
    """
    if not user_id or user_id in {".", ".."}:
        return False
    return not any(char in user_id for char in ("/", "\\", ":", "\x00"))
