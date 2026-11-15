"""Image viewing tool declaration: hand sheets or storyboard images to the Agent as image content blocks."""

from __future__ import annotations

from server.agent_toolset.declaration import BLOCKED, ToolDeclaration
from server.media_tools.image_review import (
    MAX_IMAGE_EDGE,
    MAX_IMAGES_PER_CALL,
    InspectImagesRequest,
    inspect_images,
    inspect_images_images,
    inspect_images_projection,
    inspect_images_summary,
)

INSPECT_IMAGES = ToolDeclaration(
    name="inspect_images",
    description=(
        "View sheets or storyboard images: fetch by ID the sheets of characters, scenes, props, products or character derivatives, "
        "or the storyboard images of shots, "
        "attached after the result as image content blocks. Read-only, free of charge, triggers no generation. "
        "Use it to judge whether a generation result is usable: whether it is broken, whether it matches the design or prompt, "
        "whether the same character stays consistent. "
        "One call views only one type (resource_type); version applies to every item in the list, and when omitted each item's "
        "current version is taken; "
        "to compare historical versions of the same item, call once per version. "
        "When the default current has no version record but a current image is registered, the image can still be viewed, with "
        "version null and available_versions empty; no version record is backfilled. "
        f"At most {MAX_IMAGES_PER_CALL} images per call; exceeding it returns image_limit_exceeded, so call in batches; "
        f"images are scaled proportionally to JPEG with the long edge no more than {MAX_IMAGE_EDGE} px. "
        "Result images are in request order; each item carries id, version, available_versions (all version numbers the item "
        "currently has), status, "
        "and when ok, image (attached image number, starting from 1) and prompt (the prompt registered when that version was "
        "generated; omitted when none was registered). "
        "status is ok; image_missing means the item has no image yet or that version's file is missing; image_unreadable means "
        "the image cannot be read or decoded, with detail giving the reason. "
        "A nonexistent ID returns resource_not_found, with params.ids listing the missing IDs; a nonexistent specified version "
        "returns version_not_found, "
        "with params.available_versions listing the item's existing versions."
    ),
    request_model=InspectImagesRequest,
    migration=BLOCKED,
    domain_key="inspect_images",
    handler=inspect_images,
    summary=inspect_images_summary,
    projection=inspect_images_projection,
    images=inspect_images_images,
)

IMAGE_REVIEW_TOOLS = (INSPECT_IMAGES,)

__all__ = ["IMAGE_REVIEW_TOOLS", "INSPECT_IMAGES"]
