"""A fixed-size model's ``sizes`` override never shows up as an editable dimension (and doesn't break the view)."""

from __future__ import annotations

from lib.backends.image_backends.base import ImageCapability
from lib.custom_provider.capability_view import _editable_image_keys, overridable_keys


def test_sizes_is_overridable_but_not_offered_for_editing():
    keys = set(overridable_keys("image"))
    assert "sizes" in keys
    implemented = frozenset({ImageCapability.TEXT_TO_IMAGE, ImageCapability.IMAGE_TO_IMAGE})
    assert _editable_image_keys(keys, implemented) == {"text_to_image", "image_to_image"}
    assert _editable_image_keys(keys, frozenset({ImageCapability.TEXT_TO_IMAGE})) == {"text_to_image"}
