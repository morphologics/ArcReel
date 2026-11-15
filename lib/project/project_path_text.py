"""Rewrite absolute project-directory paths in message text into project-relative paths.

Messages facing external hosts should not leak the server's file-system layout; project-relative paths locate
files equally well for the embedded Agent, the web client and remote Agents. Only the project-directory prefix
itself is rewritten; the rest of the message body is kept as-is.

把消息文本里的项目目录绝对路径改写为项目内相对路径。

面向外部宿主的消息不应带出服务端文件系统布局；项目内相对路径对内嵌 Agent、Web 端与
远程 Agent 同样能定位文件。只改写项目目录前缀本身，消息的其余正文原样保留。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path, PurePath


def project_path_spellings(project_dir: PurePath) -> tuple[str, ...]:
    """The absolute-path spellings of the project directory that may appear in messages, longest first.

    Includes the literal spelling, the symlink-resolved spelling (macOS /tmp -> /private/tmp), the forward-slash
    spelling (Windows), and their escaped spellings in exception text: ``OSError`` quotes the file name via
    ``repr``, in which Windows backslashes appear doubled.

    项目目录在消息里可能出现的绝对路径写法，长者在前。

    包括原样写法、解析符号链接后的写法（macOS 的 /tmp → /private/tmp）、正斜杠写法（Windows），
    以及它们在异常文本里的转义写法：``OSError`` 按 ``repr`` 引用文件名，Windows 路径的反斜杠在其中成对出现。
    """
    roots = [project_dir, project_dir.resolve()] if isinstance(project_dir, Path) else [project_dir]
    plain = {spelling for root in roots for spelling in (str(root), root.as_posix())}
    escaped = {quoted for spelling in plain for quoted in _repr_spellings(spelling)}
    return tuple(sorted(plain | escaped, key=len, reverse=True))


def _repr_spellings(spelling: str) -> tuple[str, str]:
    """The two spellings of ``spelling`` as a file-name prefix inside ``repr``.

    ``repr`` picks quotes for the whole string: without double quotes, single quotes stay as-is; when the string
    contains both kinds of quotes, it is wrapped in single quotes and single quotes are escaped. Appending one double
    quote and taking ``repr`` gives exactly the prefix in the latter spelling.

    ``spelling`` 作为文件名前缀出现在 ``repr`` 里的两种写法。

    ``repr`` 按整串选引号：不含双引号时单引号原样保留；整串同时含两种引号时改用单引号包裹并转义单引号。
    拼上一个双引号再取 ``repr``，得到的正是后一种写法里的前缀。
    """
    return repr(spelling)[1:-1], repr(f'{spelling}"')[1:-2]


def relativize_project_paths(text: str, spellings: Iterable[str]) -> str:
    """Rewrite absolute project-directory paths in a message into project-relative paths; the project directory itself
    becomes ``.``.

    Only full paths starting at the project directory are rewritten: if the preceding character is an ASCII path
    character (e.g. ``/mnt`` followed by ``/data/projects/demo``), it's a different path and is kept as-is; when
    Chinese body text directly abuts the path, it is rewritten as usual. One separator right after the prefix
    (forward slash, backslash, or a doubled backslash in escaped text) is removed too; the remaining relative path
    keeps its original spelling.

    把消息里的项目目录绝对路径改写为项目内相对路径；项目目录本身写作 ``.``。

    只改写从项目目录起始的完整路径：前一个字符是 ASCII 路径字符（如 ``/mnt`` 接 ``/data/projects/demo``）的是另一条路径，
    原样保留；中文正文紧贴路径时照常改写。紧跟前缀的一个分隔符（正斜杠、反斜杠，或转义文本里成对的反斜杠）一并去掉，余下的相对路径保持原写法。
    """
    for root in spellings:
        text = re.sub(
            r"(?<![A-Za-z0-9_.\-/\\])" + re.escape(root) + r"(?:(\\\\|[\\/])|(?![\w.\-]))",
            lambda m: "" if m.group(1) else ".",
            text,
        )
    return text


__all__ = ["project_path_spellings", "relativize_project_paths"]
