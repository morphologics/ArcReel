"""The three endpoint test modes, and which ones each definition ``kind`` supports.

The three modes answer three progressively deeper questions about the same definition, but not every kind can
answer all of them: whether "check response" works depends on whether the kind has user-configurable value paths to
check -- without configurable paths it would only check a piece of fixed code and give no information
(``docs/adr/0081``). Unsupported is not an invalid definition, so it does not go through definition diagnostics; the
caller turns it into a structured rejection.

This module only has the roster and one pure predicate, with no runtime imports -- the three HTTP entry points and
the service layer share one support matrix, so no entry point can let something through only to discover midway
that this kind has no implementation at all.

端点测试的三种模式，以及每种定义 ``kind`` 支持哪几种。

三模式对同一份定义回答三个层层递进的问题，但不是每种 kind 都答得上：能不能「验证响应」取决于
这种 kind 是否有用户可配的取值路径可验——没有可配路径时，验的只是一段固定代码，给不出任何信息
（``docs/adr/0081``）。不支持不是错误定义，故不走定义诊断，由调用方转成一条结构化的拒绝。

本模块只有名录与一个纯判定，不引运行时——三个 HTTP 入口与服务层共用同一份支持矩阵，才不会出现
某个入口放行、执行到一半才发现这种 kind 根本没有实现。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from arcreel_market_core.endpoint_definition import COMFYUI_KIND, DECLARATIVE_KIND


class EndpointTestMode(StrEnum):
    """端点测试的三种模式：预览请求、验证响应、测试连接。取值只作模式标识，不外发。"""

    PREVIEW_REQUEST = "preview-request"
    CHECK_RESPONSE = "check-response"
    TRIAL_RUN = "trial-run"


#: ``kind`` -> modes supported by that kind. Kinds outside the roster support no mode. / ``kind`` → 该 kind 支持的模式。名录外的 kind 一种模式都不支持。
_MODES_BY_KIND: Mapping[str, frozenset[EndpointTestMode]] = {
    DECLARATIVE_KIND: frozenset(EndpointTestMode),
    # ComfyUI endpoints have no "check response": artifact extraction reads three fixed keys of the node bound to
    # ``output``, there is no user-configurable value path, and it would only check a piece of fixed code (``docs/adr/0081``).
    # ComfyUI 端点没有「验证响应」：产物提取读的是 ``output`` 绑定那个节点的固定三个键，没有
    # 一处用户可配的取值路径，验的只会是一段固定代码（``docs/adr/0081``）。
    COMFYUI_KIND: frozenset({EndpointTestMode.PREVIEW_REQUEST, EndpointTestMode.TRIAL_RUN}),
}


#: All kinds endpoint testing knows. Must match :data:`.kinds.SUPPORTED_KINDS` -- a kind in the support matrix without
#: an implementation would let the entry point pass it through, only to discover midway there is nothing to run.
#: 端点测试认得的全部 kind。与 :data:`.kinds.SUPPORTED_KINDS` 必须一致——一种 kind 出现在支持
#: 矩阵里却没有实现，会让入口放行、执行到一半才发现没东西可跑。
TESTABLE_KINDS = frozenset(_MODES_BY_KIND)


def supports_test_mode(kind: str, mode: EndpointTestMode) -> bool:
    """Whether a definition of this ``kind`` can run this mode. / 这种 ``kind`` 的定义能不能跑这种模式。"""
    return mode in _MODES_BY_KIND.get(kind, frozenset())
