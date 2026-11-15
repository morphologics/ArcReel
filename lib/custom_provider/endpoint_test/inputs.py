"""Input value objects shared by the three endpoint test modes: call parameters, credentials, materials.

The parameter set has the same names as ``VideoGenerationRequest``, except there is no seed -- the product UI has
nowhere to set it, and always null in a trial run would only make the template delete the key, leaving a branch that
is never reached. Materials do not go into the asset library or the project: the caller reads them into memory and
hands them here, discarded after use.

端点测试三模式共用的输入值对象：调用参数、凭证、素材。

参数集与 ``VideoGenerationRequest`` 同名，唯独没有 seed——产品界面无处设置它，试跑里恒为 null
只会让模板把键删掉，留一个永远走不到的分支。素材不落资产库、不进项目：调用方读进内存交给这里，
用完即丢。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from arcreel_market_core.endpoint_definition import AssetData

#: Mapping from material source name (enum of ``inputs.*.source``) to "is list-type"; the multipart field name is the source name.
#: 素材来源名（``inputs.*.source`` 的枚举）到「是否列表型」的对应，multipart 字段名即来源名。
ASSET_SOURCES: Mapping[str, bool] = {
    "start_image": False,
    "end_image": False,
    "reference_images": True,
    "reference_audio_files": True,
}


@dataclass(frozen=True)
class EndpointTestParameters:
    """Call parameters of one endpoint test. / 一次端点测试的调用参数。"""

    model: str
    prompt: str
    duration_seconds: int = 5
    aspect_ratio: str = "9:16"
    resolution: str | None = None
    generate_audio: bool = True


@dataclass(frozen=True)
class EndpointTestCredentials:
    """Inline credentials. The entry point that reads from the DB by ``provider_id`` converts to the same value object at the service boundary.

    内联凭证。按 ``provider_id`` 读库的入口在服务边界换成同一个值对象。"""

    base_url: str
    api_key: str


@dataclass(frozen=True)
class EndpointTestAssets:
    """Material contents grouped by source name. Absent sources are ``None`` (list-type: empty list).

    按来源名归拢的素材内容。缺席的来源取 ``None``（列表型取空列表）。"""

    by_source: Mapping[str, AssetData | Sequence[AssetData] | None] = field(default_factory=dict)

    def get(self, source: str) -> AssetData | Sequence[AssetData] | None:
        return self.by_source.get(source)

    def items(self, source: str) -> list[AssetData]:
        """Materials of a list-type source; empty list when absent or for single-value sources. / 列表型来源的素材；缺席或单值来源取空列表。"""
        value = self.by_source.get(source)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return list(value)
        return []

    def single(self, source: str) -> AssetData | None:
        """Material of a single-value source; ``None`` when absent. / 单值型来源的素材；缺席即 ``None``。"""
        value = self.by_source.get(source)
        return value if isinstance(value, AssetData) else None
