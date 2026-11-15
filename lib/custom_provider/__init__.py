"""Custom provider module. / 自定义供应商模块。"""

import re

CUSTOM_PROVIDER_PREFIX = "custom-"

#: Key prefix of user-defined call endpoints (``ce-<id>``). Built-in endpoint keys must not use this prefix (``lib.custom_provider.
#: endpoints`` checks it at import time): both key sets share the model row's ``endpoint`` column and the endpoint lookup entry; the
#: prefix splits them into two never-overlapping namespaces, and it alone decides which side a lookup goes to.
#: 用户自定义调用端点的键前缀（``ce-<id>``）。内置端点键不得占用该前缀（``lib.custom_provider.
#: endpoints`` 在导入期校验）——两套键共享模型行的 ``endpoint`` 列与端点查表入口，前缀划分出两个
#: 永不重叠的命名空间，查表走哪一侧由它唯一决定。
CUSTOM_ENDPOINT_KEY_PREFIX = "ce-"


def make_provider_id(db_id: int) -> str:
    """Build a custom provider's provider_id string, e.g. 'custom-3'. / 构造自定义供应商的 provider_id 字符串，如 'custom-3'。"""
    return f"{CUSTOM_PROVIDER_PREFIX}{db_id}"


def parse_provider_id(provider_id: str) -> int:
    """Extract the database ID from a provider_id of the form 'custom-3'.

    Raises:
        ValueError: if the format is invalid

    从 'custom-3' 格式的 provider_id 提取数据库 ID。

    Raises:
        ValueError: 如果格式不正确
    """
    return int(provider_id.removeprefix(CUSTOM_PROVIDER_PREFIX))


def is_custom_provider(provider_id: str) -> bool:
    """Whether this is a custom provider's provider_id. / 判断是否为自定义供应商的 provider_id。"""
    return provider_id.startswith(CUSTOM_PROVIDER_PREFIX)


def make_endpoint_key(db_id: int) -> str:
    """Build the endpoint key of a custom call endpoint, e.g. 'ce-3'. Keys are assigned by the system; shared files carry no key.

    构造自定义调用端点的 endpoint 键，如 'ce-3'。键由系统分配，分享文件不带键。"""
    return f"{CUSTOM_ENDPOINT_KEY_PREFIX}{db_id}"


_CUSTOM_ENDPOINT_KEY = re.compile(rf"^{re.escape(CUSTOM_ENDPOINT_KEY_PREFIX)}([1-9][0-9]*)\Z")


def parse_endpoint_key(endpoint: str) -> int:
    """Extract the database ID from an endpoint key of the form 'ce-3'.

    Only the canonical form is accepted: prefix + a positive integer without leading zeros. The key's literal is its only
    canonical form; the delete path compares the model row's endpoint column by literal, so a looser parser would split the
    two paths' judgement of "the same endpoint".

    Raises:
        ValueError: non-canonical form (leading zeros, sign, whitespace, underscore separators, non-ASCII digits, etc.)

    从 'ce-3' 格式的 endpoint 键提取数据库 ID。

    只认规范形：前缀 + 无前导零的正整数。键的字面量即唯一规范形，删除路径按字面量比对
    模型行的 endpoint 列，解析侧放宽会让两条路径对「同一端点」的判定分裂。

    Raises:
        ValueError: 非规范形（前导零、正负号、空白、下划线分隔、非 ASCII 数字等）
    """
    match = _CUSTOM_ENDPOINT_KEY.match(endpoint)
    if match is None:
        raise ValueError(f"malformed custom endpoint key: {endpoint!r}")
    return int(match.group(1))


def is_custom_endpoint(endpoint: str) -> bool:
    """Whether this is a custom call endpoint key. / 判断是否为自定义调用端点的键。"""
    return endpoint.startswith(CUSTOM_ENDPOINT_KEY_PREFIX)
