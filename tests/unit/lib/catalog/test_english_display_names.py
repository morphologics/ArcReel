"""English display names: Chinese official display names are served in English; other names are untouched.

英文显示名：中文官方显示名以英文提供，其余名称不动。
"""

import re

import pytest

from lib.catalog import query as catalog
from lib.catalog.english_display_names import english_display_name, with_english_display_names

_CJK = re.compile(r"[　-〿一-鿿＀-￯]")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("可灵 v3", "Kling v3"),
        ("可灵图像 O1", "Kling Image O1"),
        ("可灵 V3-Omni（图像）", "Kling V3-Omni (Image)"),
        ("万相 2.7 图生视频", "Wan 2.7 Image-to-Video"),
        ("万相 2.7 参考生视频", "Wan 2.7 Reference-to-Video"),
        ("万相 2.7 图像 Pro", "Wan 2.7 Image Pro"),
        ("万相 3.0 视频", "Wan 3.0 Video"),
        ("豆包 Seed 2.0 Pro", "Doubao Seed 2.0 Pro"),
        ("HappyHorse 1.0 文生视频", "HappyHorse 1.0 Text-to-Video"),
        ("Seedance 2.0", "Seedance 2.0"),
    ],
)
def test_english_display_name(name: str, expected: str):
    assert english_display_name(name) == expected


def test_unchanged_data_is_returned_as_is():
    english_only = catalog.factory_snapshot()
    once = with_english_display_names(english_only)
    assert with_english_display_names(once) is once


def test_served_catalog_has_no_chinese_display_names():
    catalog.reset_for_tests()
    leftovers = [entry.display_name for entry in catalog.catalog_data().entries if _CJK.search(entry.display_name)]
    assert leftovers == []
    # The official entry offered for "restore official values" is the English one too.
    # 「恢复官方值」所给的官方条目同样是英文。
    entry = next(entry for entry in catalog.factory_snapshot().entries if entry.display_name == "可灵 v3")
    official = catalog.official_entry(entry.key)
    assert official is not None
    assert official.display_name == "Kling v3"
