"""ArcReel market source tools and the endpoint definition validator.

Market source side (``market``): address resolution, index schema and projection, entry validation, index
generation, icon checks, and directory validation. Endpoint definitions (``endpoint_definition`` and what it depends
on): two-layer structure and semantic validation. Diagnostics are always ``code + params``; this package carries no
translation catalog, and rendering is done by a translator passed in by the caller.

ArcReel 市场源工具与端点定义校验器。

市场源侧（``market``）：地址解析、索引 schema 与投影、条目校验、索引生成、图标检查与目录校验；
端点定义（``endpoint_definition`` 及其依赖）：结构与语义两层校验。诊断一律是 ``code + params``，
本包不携带翻译目录，渲染由调用方传入 translator。
"""
