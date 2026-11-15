"""Template primitives for the ``auth`` section, and the semantic checks shared by both ``kind`` values.

The ``auth`` section of declarative definitions and ComfyUI definitions has the same schema and the same rendering semantics: two tables,
``headers`` and ``query``, whose values are templates, and credentials appear only as the ``api_key`` placeholder. The two kinds differ in one respect only:
which variables are accepted besides ``api_key`` (in a declarative auth section base variables such as ``base_url`` are also usable, while ComfyUI's accepts only ``api_key``).
So the caller supplies the variable scope via ``variable_issues``; the other three checks (malformed placeholder, literal credential, duplicate header name)
are independent of kind and live here, read by both sides.

This module is at the package top level rather than inside ``endpoint_definition``: the comfyui subpackage is bound by the import contract of
"no dependency on the declarative runtime" and cannot reach any implementation in that package; like ``definition_diagnostics`` and
``definition_schema_errors``, it is a leaf that both kinds depend on downward.

Rendering lives here for the same reason: the ComfyUI client must render the same ``auth`` section into real request headers and query parameters,
and the declarative template engine is on the side it cannot reach. This way the credential syntax and its validation do not each hold
their own understanding of what a legal placeholder is.

``auth`` 节的模板原语与两种 ``kind`` 共用的语义检查。

声明式定义与 ComfyUI 定义的 ``auth`` 节是同一份 schema、同一套渲染语义：``headers`` 与 ``query``
两张表，值是模板，凭证只以 ``api_key`` 占位符的形态出现。两种 kind 的差别只有一条——除 ``api_key``
外还认哪些变量（声明式的 auth 节里 ``base_url`` 之类的基础变量同样可用，ComfyUI 的只认 ``api_key``）
——故变量作用域由调用方以 ``variable_issues`` 给出，其余三条检查（占位符写坏、字面凭证、头名重复）
与 kind 无关，落在这里由两侧共读。

本模块在包顶层而不在 ``endpoint_definition`` 内：comfyui 子包受「不依赖
声明式运行时」的 import 契约约束，够不到那个包里的任何实现；同 ``definition_diagnostics`` 与
``definition_schema_errors``，这是两种 kind 都向下依赖的叶子。

渲染同住这里的理由一样：ComfyUI 客户端要把同一份 ``auth`` 节渲染成真实请求头与查询参数，而声明式
的模板引擎在它够不到的那一侧。凭证的写法与它的校验因此不会各持一份对「什么是合法占位符」的理解。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from arcreel_market_core.definition_diagnostics import DefinitionErrorCode, DefinitionIssue, join_path

#: The only variable in ``auth`` that carries a credential. / ``auth`` 里唯一承载凭证的变量。
API_KEY_VARIABLE = "api_key"

# Legal placeholder: ``{{ variable }}``, where the variable name may be dot-separated (``inputs.first_frame``).
#: 合法占位符：``{{ 变量名 }}``，变量名允许点号分段（``inputs.first_frame``）。
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}")

# Every ``{{`` in a template. One that does not land at the start of :data:`PLACEHOLDER` is malformed: the format accepts only bare variables;
# filters, subscripts, expressions and unclosed opening braces are not placeholders, and would be sent to the provider as-is at render time.
#: 模板里每一处 ``{{``。不落在 :data:`PLACEHOLDER` 起点上的即写法不合法：格式只认裸变量，
#: 过滤器、下标、表达式与未闭合的开括号都不是占位符，渲染时会原样发给供应商。
_PLACEHOLDER_OPEN = re.compile(r"\{\{")

# What a credential looks like: a 20+ character token string containing digits (the common shape of API keys / JWTs / base64). Version numbers and fixed
# fields, being short static values, do not match; a false positive costs only a warning that does not block saving.
#: 凭证长相：20+ 位含数字的 token 串（API key / JWT / base64 的公共形态）。版本号、固定
#: 字段这类短静态值不命中；误报的代价只是一条不拦保存的 warning。
_CREDENTIAL_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{20,}")

#: The two tables in the ``auth`` section. / ``auth`` 节里的两张表。
_AUTH_GROUPS = ("headers", "query")


def placeholder_names(text: str) -> list[str]:
    """Variable names of all legal placeholders in the template, in order of appearance.

    模板里全部合法占位符的变量名，按出现次序。
    """
    return PLACEHOLDER.findall(text)


def malformed_placeholders(text: str) -> list[str]:
    """Every ``{{`` fragment that is not a legal placeholder, taken up to the nearest ``}}`` (or to the end of the string if none).

    所有不构成合法占位符的 ``{{`` 片段，取到最近的 ``}}``（没有就到串尾）。
    """
    valid_starts = {match.start() for match in PLACEHOLDER.finditer(text)}
    fragments: list[str] = []
    for match in _PLACEHOLDER_OPEN.finditer(text):
        if match.start() in valid_starts:
            continue
        closing = text.find("}}", match.start())
        fragments.append(text[match.start() : closing + 2] if closing != -1 else text[match.start() :])
    return fragments


def looks_like_literal_credential(template: str) -> bool:
    """Whether the literal part left after stripping placeholders from an auth value looks like a directly written credential.

    Credentials appearing as the api_key placeholder is the precondition for stripping credentials on export; a literal credential would leak
    as-is through export and "copy as mine", and can only be flagged by shape recognition.

    auth 值剔除占位符后，剩余字面部分是否形似直接写入的凭证。

    凭证以 api_key 占位符形态出现是导出剥凭证的前提；字面凭证会随导出与「复制为我的」
    原样外流，只能靠形态识别提示。
    """
    literal = PLACEHOLDER.sub(" ", template)
    return any(any(ch.isdigit() for ch in token) for token in _CREDENTIAL_TOKEN.findall(literal))


def auth_templates(auth: Mapping[str, Any]) -> Iterator[tuple[str, str, str, str]]:
    """Every template value in the ``auth`` section: ``(table, key, locator path, template)``.

    The key and the locator path are both given: header names may contain dots, and deriving the key from the last path segment would truncate ``X-Trace.Id`` to
    ``Id``.

    ``auth`` 节里每个模板值：``(所在表, 键名, 定位路径, 模板)``。

    键名与定位路径一并给出：头名里允许出现点号，从路径末段反推键名会把 ``X-Trace.Id`` 截成
    ``Id``。
    """
    for group in _AUTH_GROUPS:
        values: Mapping[str, Any] = auth.get(group) or {}
        for name, template in values.items():
            yield group, str(name), join_path(join_path("auth", group), str(name)), str(template)


def declares_credentials(auth: Mapping[str, Any]) -> bool:
    """Whether this section will actually send credentials.

    Counted by whether the two tables have entries, not by the ``auth`` object itself: ``{"headers": {}}`` sends not a single header,
    equivalent to the whole section being absent. :func:`check_auth_section` and :func:`render_auth` both use this measure, so
    "does this definition need an api_key" must too.

    这一节到底会不会发出凭据。

    按两张表里有没有条目算，不按 ``auth`` 这个对象本身算：``{"headers": {}}`` 一个头都不会发
    出去，与整节缺席等效——:func:`check_auth_section` 与 :func:`render_auth` 都是这个口径，
    「这份定义要不要 api_key」自然也得是。
    """
    return any(True for _ in auth_templates(auth))


def duplicate_header_issues(path: str, headers: Mapping[str, Any]) -> Iterator[DefinitionIssue]:
    """Keys in the same header table that differ only in case: HTTP header names are case-insensitive, so both would be sent.

    同一张头表里大小写不同的同名键：HTTP 头名不区分大小写，两条会一起发出去。
    """
    seen: dict[str, str] = {}
    for name in headers:
        first = seen.setdefault(name.lower(), name)
        if first != name:
            yield DefinitionIssue(
                join_path(path, name),
                DefinitionErrorCode.HEADER_NAME_DUPLICATE,
                {"header": name, "first": first},
            )


@dataclass(frozen=True)
class AuthIssues:
    """Output of one ``auth`` section check, in two columns by severity: a literal credential only warns and does not block saving.

    一次 ``auth`` 节检查的产出，按严重度分两列——字面凭证只提示，不拦保存。
    """

    errors: tuple[DefinitionIssue, ...] = ()
    warnings: tuple[DefinitionIssue, ...] = ()


def check_auth_section(
    auth: Mapping[str, Any],
    *,
    variable_issues: Callable[[str, str], Iterable[DefinitionIssue]],
) -> AuthIssues:
    """Run all semantic rules for the ``auth`` section.

    ``variable_issues(path, name)`` is the caller's variable scope check for its kind, called only on variables other than ``api_key``:
    ``api_key`` is always legal in an auth section, and whether it appears decides the last rule, "a non-empty auth must reference a
    credential"; keeping that rule in each kind would let the two sides drift apart on what counts as the credential being written.

    "Non-empty" is counted by whether the two tables have entries, not by the ``auth`` object itself: ``{"headers": {}}`` sends not a single
    header, equivalent to the whole section being absent.

    跑 ``auth`` 节的全部语义规则。

    ``variable_issues(path, name)`` 是调用方那种 kind 的变量作用域判定，只在 ``api_key`` 以外的
    变量上调用：``api_key`` 在 auth 节里恒合法，且它是否出现过决定最后那条「非空 auth 必须引用
    凭证」——把这一条留在各 kind 里，两侧就会各自漂移出一份「凭证算不算写到了」的理解。

    「非空」按两张表里有没有条目算，不按 ``auth`` 这个对象本身算：``{"headers": {}}`` 一个头都
    不会发出去，与整节缺席等效。
    """
    errors: list[DefinitionIssue] = []
    warnings: list[DefinitionIssue] = []
    references_api_key = False
    for _group, _name, path, template in auth_templates(auth):
        errors.extend(
            DefinitionIssue(path, DefinitionErrorCode.MALFORMED_PLACEHOLDER, {"fragment": fragment})
            for fragment in malformed_placeholders(template)
        )
        for name in placeholder_names(template):
            if name == API_KEY_VARIABLE:
                references_api_key = True
                continue
            errors.extend(variable_issues(path, name))
        if looks_like_literal_credential(template):
            warnings.append(DefinitionIssue(path, DefinitionErrorCode.AUTH_LITERAL_CREDENTIAL))
    errors.extend(duplicate_header_issues(join_path("auth", "headers"), auth.get("headers") or {}))
    if declares_credentials(auth) and not references_api_key:
        errors.append(DefinitionIssue("auth", DefinitionErrorCode.AUTH_WITHOUT_API_KEY))
    return AuthIssues(tuple(errors), tuple(warnings))


def render_auth(auth: Mapping[str, Any], *, api_key: str) -> tuple[dict[str, str], dict[str, str]]:
    """Render the ``auth`` section into the ``(headers, query)`` that will actually be sent.

    The section is not rendered when ``api_key`` is empty: ComfyUI has no native auth, credentials are needed only in deployments behind a reverse proxy, and sending
    an empty credential such as ``Authorization: Bearer `` would make the proxy reject with 401 a request that should have been let through.

    Variables other than ``api_key`` are rejected at save time by the validator's rules (a ComfyUI auth section accepts only this one), so no
    general template evaluation is done here; if a placeholder does slip through, leaving it in the value as-is makes it easier to see which spot was written wrong than replacing it with an empty string.

    把 ``auth`` 节渲染成实际要发出的 ``(headers, query)``。

    ``api_key`` 为空时整节不渲染：ComfyUI 原生无鉴权，凭证只在套了反向代理的部署里才需要，而把
    ``Authorization: Bearer `` 这样的空凭证发出去，代理会以 401 拒绝一个本该放行的请求。

    ``api_key`` 以外的变量按校验器的规则在保存期就被拒（ComfyUI 的 auth 节只认这一个），故这里
    不做通用模板求值——真有漏网的占位符，原样留在值里比替换成空串更容易看出是哪一处写错了。
    """
    if not api_key:
        return {}, {}
    rendered: dict[str, dict[str, str]] = {"headers": {}, "query": {}}
    for group, name, _path, template in auth_templates(auth):
        rendered[group][name] = PLACEHOLDER.sub(
            lambda match: api_key if match.group(1) == API_KEY_VARIABLE else match.group(0), template
        )
    return rendered["headers"], rendered["query"]
