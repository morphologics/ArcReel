"""Model name normalization (see docs/adr/0105).

模型名的归一化（见 docs/adr/0105）。
"""

from __future__ import annotations


def normalize_model_name(name: str) -> str:
    """Ignore case, strip the last `/` and the namespace before it, and treat dots as hyphens.

        Date suffixes are not stripped, nor are suffixes such as `:free`, `-thinking`, `-preview`: whether a dated snapshot
        belongs to the same model is expressed by the model aliases in the catalog data.

    忽略大小写，去掉最后一个 ``/`` 及之前的命名空间，点号视同连字符。

    不去掉日期后缀，也不去掉 ``:free``、``-thinking``、``-preview`` 一类后缀：带日期的快照是否
    属于同一个模型，由目录数据里的模型别名表达。
    """
    return name.rpartition("/")[2].lower().replace(".", "-")
