"""Declarations of the media generation tools.

Generation tools are long tasks: the ArcReel Agent waits until the batch reaches a terminal state and gets
``generation_result`` and ``batch_id``; external Agents
immediately get a ``generation_batch`` handle and poll. Both values are projected by the same set of result hooks; the
terminal ``generation_result`` has the same shape as the generation result attached to the batch terminal-state query.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, overload

from pydantic import BaseModel

from server.agent_toolset.declaration import BLOCKED, READ_CHECK, ScopedHandler, ToolDeclaration
from server.media_tools.assets import (
    GenerateAssetsRequest,
    ListPendingAssetsRequest,
    generate_assets,
    list_pending_assets,
)
from server.media_tools.context import (
    GenerationToolValue,
    generation_is_error,
    generation_structured,
    generation_summary,
)
from server.media_tools.image_edits import EditImagesRequest, edit_images
from server.media_tools.narration_audio import GenerateNarrationAudioRequest, generate_narration_audio
from server.media_tools.storyboards import GenerateStoryboardsRequest, generate_storyboards
from server.media_tools.videos import GenerateVideosRequest, generate_videos


@overload
def generation_tool[RequestT: BaseModel](
    *,
    name: str,
    description: str,
    request_model: type[RequestT],
    handler: ScopedHandler[RequestT, GenerationToolValue],
) -> ToolDeclaration[RequestT, GenerationToolValue]: ...


@overload
def generation_tool[RequestT: BaseModel, ResultT](
    *,
    name: str,
    description: str,
    request_model: type[RequestT],
    handler: ScopedHandler[RequestT, ResultT],
    summary: Callable[[ResultT], str | None],
    projection: Callable[[ResultT], dict[str, Any]],
    is_error: Callable[[ResultT], bool],
) -> ToolDeclaration[RequestT, ResultT]: ...


def generation_tool(
    *,
    name: str,
    description: str,
    request_model: type[BaseModel],
    handler: ScopedHandler[Any, Any],
    summary: Callable[[Any], str | None] = generation_summary,
    projection: Callable[[Any], dict[str, Any]] = generation_structured,
    is_error: Callable[[Any], bool] = generation_is_error,
) -> ToolDeclaration[Any, Any]:
    """Declaration for long-running generation tasks: the entry point blocks projects whose migration failed, and
    results are projected by the generation-result hooks by default.

    When the success value has other shapes besides a generation result (such as the grid plan preview), pass the
    three hooks that cover that shape.
    """
    return ToolDeclaration(
        name=name,
        description=description,
        request_model=request_model,
        migration=BLOCKED,
        domain_key="generation_result",
        handler=handler,
        long_task=True,
        summary=summary,
        projection=projection,
        is_error=is_error,
    )


LIST_PENDING_ASSETS = ToolDeclaration(
    name="list_pending_assets",
    description=(
        "List the characters, scenes, props and products in the project that have no sheet yet (names and "
        "description summaries); omit type to aggregate all types. Read-only, no side effects. "
        "When the project data upgrade failed, returns a project_migration_failed problem; after fixing per its "
        "details, call retry_project_migration."
    ),
    request_model=ListPendingAssetsRequest,
    migration=READ_CHECK,
    domain_key="list_pending_assets",
    handler=list_pending_assets,
)

GENERATE_ASSETS = generation_tool(
    name="generate_assets",
    description=(
        "Batch-generate sheets for characters / scenes / props / products (paid generation; images written to the "
        "project). "
        "Assets of several types go into the same batch; when only filling missing images, outdated but usable old "
        "images are reused and not regenerated automatically. "
        "Assets missing a description are each recorded as blocked. "
        "The terminal result's generation_result gives the outcome per ID as requested / succeeded / failed / "
        "blocked; "
        "IDs look like character/张三 (character/<name>), and each failed item carries a stable code and next action."
    ),
    request_model=GenerateAssetsRequest,
    handler=generate_assets,
)

GENERATE_STORYBOARDS = generation_tool(
    name="generate_storyboards",
    description=(
        "Generate storyboard images for narration or drama scripts (paid generation; images written to the project's "
        "storyboards/). "
        "When only filling missing images, outdated but usable old images are not regenerated automatically. "
        "Shots that reference unregistered names or subjects without a sheet yet, and shots missing image_prompt, are "
        "each recorded as blocked; "
        "shots that fail to generate are additionally recorded in storyboards/generation_failures.json. "
        "The terminal result's generation_result gives the outcome per ID as requested / succeeded / failed / "
        "blocked, "
        "and each failed item carries a stable code and next action."
    ),
    request_model=GenerateStoryboardsRequest,
    handler=generate_storyboards,
)

EDIT_IMAGES = generation_tool(
    name="edit_images",
    description=(
        "Instruction-based local edits of already generated sheets / storyboard images: keep the original image "
        "largely unchanged and modify only the unsatisfactory parts per the instruction "
        "(e.g. change hair colour, remove background clutter, adjust lighting mood); batches of the same type are "
        "supported (paid generation; edit results are written to the project). "
        "Difference from \"regenerate\": edit = fine-tune on top of the original image without changing the original "
        "image_prompt; regenerating later redraws from the original prompt "
        "and voids this edit; regenerate = redraw the whole image from the original prompt, overturning parts that "
        "were already satisfactory. "
        "Use edit when the user only wants local changes; use regenerate when the user wants to overturn the "
        "composition / content and start over, or when the original image_prompt itself must change. "
        "Editing always goes through image-to-image (i2i); when the project's current image provider does not "
        "support i2i, every ID "
        "is recorded as blocked (image_capability_missing_i2i) and no generation task is created. "
        "Editing is always an explicit choice (each edit carries its own instruction); the terminal result's "
        "generation_result gives the outcome per ID as "
        "requested / succeeded / failed / blocked."
    ),
    request_model=EditImagesRequest,
    handler=edit_images,
)

GENERATE_NARRATION_AUDIO = generation_tool(
    name="generate_narration_audio",
    description=(
        "Explicitly generate narration audio (TTS; paid generation, audio written to the project's audio/) for units "
        "whose voiced content is owned by the narrator, in any generation mode. "
        "When only filling missing audio, outdated but usable old audio is not regenerated automatically. "
        "Explicitly named units whose voicing is not narrator narration, that fail voicing admission, or that have "
        "no synthesizable narration text are each recorded as blocked. "
        "The terminal result's generation_result gives the outcome per ID as requested / succeeded / failed / "
        "blocked, "
        "and each failed item carries a stable code and next action. "
        "The synthesis text is read from the latest script's canonical narrator utterances when the worker starts; "
        "it does not depend on storyboard images or videos."
    ),
    request_model=GenerateNarrationAudioRequest,
    handler=generate_narration_audio,
)

GENERATE_VIDEOS = generation_tool(
    name="generate_videos",
    description=(
        "Generate video clips for storyboard-to-video or reference-to-video projects (paid generation; videos are "
        "written to the project). "
        "Existing usable videos (Manifest deems current / stale, or a selected manually uploaded version) are reused "
        "by default. "
        "The requested duration tier is taken from the script's arranged duration, independent of the project's "
        "narration delivery. "
        "Whole-batch admission: if any target fails admission (missing material, voicing conflict, prompt still to be "
        "written, unreadable artifact state, etc.), "
        "or some target needs the user to confirm a cross-tier duration cost, this call enqueues zero tasks and "
        "creates no batch; "
        "the result carries batch_admission per-unit details and request_projections; "
        "after the user confirms the tiers, resend the original target set in one go — do not split it into several "
        "calls by tier. "
        "preview=true only pre-checks: it runs the same admission but enqueues nothing and creates no batch, and "
        "returns a video_quote "
        "(per-unit outcome of generate / reuse / blocked, with arranged duration, requested tier, tier_changed, "
        "estimated_cost and problems, plus estimated_total and confirmed_request_durations); "
        "the quote matches the real submission; when the user must confirm the cost first, pre-check first. "
        "For units the price table cannot price, estimated_cost.amount is null with an unpriced_reason, and the "
        "unit's unpriced is true; "
        "estimated_total sums only priced units, the number of unpriced units is in unpriced_count, and it is null "
        "when no unit is priced at all. "
        "Unpriced is not zero: tell the user truthfully that the cost of these units is unknown, and do not present "
        "the total as the price of the whole batch. "
        "When submitting for real after the user confirms, pass the quote's confirmed_request_durations unchanged, "
        "and no tier confirmation will be returned again; if the tiers changed between the two calls, "
        "reconfirmation is still required. "
        "The terminal result's generation_result gives the outcome per ID as requested / succeeded / failed / "
        "blocked, "
        "and each failed item carries a stable code and next action."
    ),
    request_model=GenerateVideosRequest,
    handler=generate_videos,
)

MEDIA_GENERATION_TOOLS = (
    LIST_PENDING_ASSETS,
    GENERATE_ASSETS,
    GENERATE_STORYBOARDS,
    EDIT_IMAGES,
    GENERATE_NARRATION_AUDIO,
    GENERATE_VIDEOS,
)

__all__ = ["MEDIA_GENERATION_TOOLS", "generation_tool"]
