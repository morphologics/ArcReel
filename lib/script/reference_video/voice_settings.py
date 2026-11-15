"""Voice capability profile of the reference-to-video render layer: a value object for model tier, episode switch,
clip limit and backend constraints.

The render layer (parse preview / three-part rendering / execution-time assembly) takes all its voice decisions
from the same set of inputs, which travel together layer by layer along the call chain and never change
individually. Gathered into a value object, adding a new voice-related capability flag only changes this module's
field definitions, without shotgun edits to every function signature and call site along the way.

The silent decision (:attr:`VoiceRenderSettings.is_silent`) also lives here: the two silent paths (model produces no
audio, audio turned off for this episode) are the same from the product's point of view, and writing the criterion
as a property keeps it from being written separately throughout the render layer and diverging later.

参考生视频渲染层的声音能力档：模型档位、本集开关、段数上限与 backend 约束的值对象。

渲染层（解析预览 / 三段论渲染 / 执行期组装）对声音的判定全部取自同一组入参，这些入参在
调用链上逐层结伴传递、无一处单独变化。收口成值对象后，新增一个声音相关的能力位只改本模块
的字段定义，不必 shotgun 修改沿途每一个函数签名与调用点。

无声判定（:attr:`VoiceRenderSettings.is_silent`）也落在这里：两条无声路径（模型不产音、
本集关闭音频）在产品口径上同形，判据写成属性才不会在渲染层各处各写一遍、日后分叉。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lib.generation.video_request_facts import VideoRequestFacts


@dataclass(frozen=True)
class VoiceRenderSettings:
    """The voice input profile of one render.

    ``voice_consistency`` is the server-derived three-level model tier identifier (``native`` / ``soft`` / ``none``):
    only ``native`` (class A, native audio reference) involves reference audio binding; ``none`` (class C, truly
    silent) produces no audio.

    ``requested_generate_audio`` is this episode's silent switch (user intent, not the pricing basis).

    ``max_reference_audio`` is the max number of reference audio clips per request (backend capability declaration);
    ``provider_id`` and ``model_id`` are only used to look up display names for degradation warnings.

    ``audio_ready`` is the set of character names whose "audio is actually usable": the parse preview doesn't touch the
    filesystem and leaves None, meaning decide by the character asset's ``reference_audio`` field being non-empty; the
    execution layer passes the names of characters whose files are resolved and actually exist.

    When ``requires_reference_image`` is True the target backend requires each audio clip to be attached to a specific
    reference item (e.g. wan2.7-r2v), so off-screen-only speakers get no audio binding.

    一次渲染的声音输入档。

    ``voice_consistency`` 是服务端派生的三级模型档标识（``native`` / ``soft`` / ``none``）：
    只有 ``native``（A 类·原生音频参考）谈得上参考音频绑定，``none``（C 类·真无声）不产音。

    ``requested_generate_audio`` 是本集的无声开关（用户意图，非计价口径）。

    ``max_reference_audio`` 是每请求可携带的参考音频段数上限（backend 能力声明），
    ``provider_id`` 与 ``model_id`` 只用于降级 warning 的显示名查找。

    ``audio_ready`` 是「音频确实可用」的角色名集合：解析预览不碰文件系统、留 None 表示按角色
    资产的 ``reference_audio`` 字段非空判定；执行层传入已解析且确实存在的文件对应的角色名。

    ``requires_reference_image`` 为 True 时目标 backend 要求音频逐段挂在具体参考素材项上
    （如 wan2.7-r2v），纯画外 speaker 因此不绑定音频。
    """

    voice_consistency: str = "soft"
    requested_generate_audio: bool = True
    max_reference_audio: int = 0
    provider_id: str = ""
    model_id: str = ""
    audio_ready: Collection[str] | None = None
    requires_reference_image: bool = False

    @classmethod
    def from_caps(cls, caps: Mapping[str, Any], *, audio_ready: Collection[str] | None = None) -> VoiceRenderSettings:
        """Construct from values in the resolved video capability dict.

        When capability resolution fails the caller passes an empty dict, and fields fall back to this class's defaults
        (``soft`` tier, no reference audio) -- only a few voice-related hints are not emitted; rendering is not blocked. The
        episode silent switch defaults to ``True``, i.e. "render as audible unless an off signal was received": a capability
        resolution failure should not decide for the user that this episode is silent; the real off intent is resolved
        independently by the caller and written into the capability dict (see ``server/services/tasks/video_caps.py``). The
        mapping between capability dict keys and this class's field names exists only here,
        so multiple call sites (parse preview route, SDK split tool) don't each miss a flag and reach different voice conclusions.

        从解析出的视频能力 dict 取值构造。

        能力解析失败时调用方传空 dict，各字段落到本类的字段默认（``soft`` 档、无参考音频）——
        只是少发几条声音相关提示，不阻断渲染。本集无声开关的缺省是 ``True``，即「未收到关闭
        信号就按有声渲染」：能力解析失败不该替用户把这一集判成无声，真实的关闭意图由调用方
        在能力 dict 里独立解析后写入（见 ``server/services/tasks/video_caps.py``）。能力 dict 的
        key 名与本类字段名的对应只在这一处，
        多个调用点（解析预览路由、SDK 拆分工具）因此不会各自漏取某一位而给出不同的声音结论。
        """
        return cls(
            voice_consistency=str(caps.get("voice_consistency") or "soft"),
            requested_generate_audio=bool(caps.get("requested_generate_audio", True)),
            max_reference_audio=int(caps.get("max_reference_audio_count") or 0),
            provider_id=str(caps.get("provider_id") or ""),
            model_id=str(caps.get("model") or ""),
            audio_ready=audio_ready,
            requires_reference_image=bool(caps.get("reference_audio_per_image") or False),
        )

    @classmethod
    def from_request_facts(
        cls, facts: VideoRequestFacts, *, audio_ready: Collection[str] | None = None
    ) -> VoiceRenderSettings:
        """Construct from one video request facts evaluation: the voice flags and the duration tiers come from the same evaluation.

        从一次视频请求事实取值构造：声音那几位与时长档位同源于同一次求值。"""
        return cls(
            voice_consistency=facts.voice_consistency,
            requested_generate_audio=facts.requested_generate_audio,
            max_reference_audio=facts.max_reference_audio_count,
            provider_id=facts.provider_id,
            model_id=facts.model_id,
            audio_ready=audio_ready,
            requires_reference_image=facts.reference_audio_per_image,
        )

    @property
    def is_silent(self) -> bool:
        """Whether this episode has no audible sound -- the model produces no audio (class C) or audio is turned off for this
        episode; both paths use the same convention.

        Voice trait descriptions are also not injected under this criterion: although they are prompt text rather than an
        audio payload, they describe an audible voice, and injecting them into a silent final cut only makes the model spend
        budget on constraints it can't use. Dialogue ignores this flag -- in silent video the dialogue text is still sent,
        and the provider can use it as a lip-sync reference.

        这一集是否听不到声音——模型不产音（C 类）或本集关闭了音频，两条路径同口径。

        声音特征描述随该判据一并不注入：它虽是提示词文本而非音频负载，但描述的是听得到的
        音色，无声成片里注入只会让模型把配额花在用不上的约束上。台词不看这一位——无声视频
        里台词文本照常下发，供应商可用作口型参考。
        """
        return self.voice_consistency == "none" or not self.requested_generate_audio
