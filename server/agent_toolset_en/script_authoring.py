"""Declarations for the text generation, review gate and draft tools.

The two text generation tools are long tasks: the ArcReel Agent waits until the text task reaches a terminal state and gets
``text_generation``; an external Agent immediately gets a ``generation_batch`` handle and polls it. With ``dry_run``, both hosts get the prompt returned directly.
"""

from __future__ import annotations

from typing import Any

from lib.generation.generation_batch import GenerationBatchReadModel
from lib.script.draft_quarantine import OPEN_DRAFT_TOOL_NAME, PROMOTE_TOOL_NAME
from server.agent_toolset.declaration import BLOCKED, TEXT_OUTPUT_TRUNCATED_NOTE, ToolDeclaration
from server.agent_toolset.envelope import json_value
from server.tool_runtime import (
    ConfirmScriptReviewRequest,
    DiscardDraftRequest,
    DraftLocator,
    GenerateEpisodeScriptRequest,
    GenerateScriptPlanRequest,
    PatchDraftRequest,
    PromoteDraftRequest,
    confirm_script_review,
    discard_draft,
    generate_episode_script,
    generate_script_plan,
    open_draft,
    patch_draft,
    promote_draft,
)

_TEXT_GENERATION = "text_generation"
_GENERATION_BATCH = "generation_batch"
_DRAFT = "draft"


def _text_generation_structured(value: Any) -> dict[str, Any]:
    """The batch handle goes under ``generation_batch``; generation results and the dry_run prompt go under ``text_generation``."""
    if isinstance(value, GenerationBatchReadModel):
        return {_GENERATION_BATCH: json_value(value)}
    return {_TEXT_GENERATION: json_value(value)}


GENERATE_EPISODE_SCRIPT = ToolDeclaration(
    name="generate_episode_script",
    description=(
        "Prompt authoring: fill in the visual layer for the shots / video units pending authoring in the script; the input is the script's own content, and the script plan is not read. "
        "By default it fills gaps: the scope is all entries pending authoring, or the entries named in entry_ids; the image prompt and the video prompt are each judged as a whole, existing ones are kept, "
        "and only the missing one is filled in; for reference-to-video, the unit body text is expanded according to the pending-authoring markers. rewrite=true explicitly rewrites the entire visual layer of the entries in scope; "
        "when that would overwrite existing content it returns prompt_overwrite_required, the receipt body is the server-generated loss list, "
        "and params.prompt_overwrite.revision is the approval token; relay the list to the user first, and only after they agree call again with that revision as overwrite_revision. "
        "Content fields, notes, end frames and already-generated artifacts are kept as they are. "
        "When an ad project has no script yet it is generated whole, and the result directly becomes the script; new characters / scenes / props in the references are, according to the "
        "handling decisions in new_assets, registered along with it as assets pending generation, and the receipt lists these new assets. regenerate=true redoes an existing script whole: "
        "it first returns script_overwrite_required with the loss list, params.script_overwrite.revision is the approval token, and the confirmation flow is the same as above. "
        "If the output of a whole-script generation violates the rules the task fails (ad_script_rejected), and the script, assets and drafts are all unchanged; you can retry with targeted additional instructions. "
        "With dry_run=true the prompt is returned directly and no generation task is submitted." + TEXT_OUTPUT_TRUNCATED_NOTE
    ),
    request_model=GenerateEpisodeScriptRequest,
    migration=BLOCKED,
    domain_key=_TEXT_GENERATION,
    handler=generate_episode_script,
    long_task=True,
    projection=_text_generation_structured,
)

GENERATE_SCRIPT_PLAN = ToolDeclaration(
    name="generate_script_plan",
    description=(
        "Generate a structured script_plan according to the project's content mode: drama shots, narration shots or reference-to-video units, "
        "and at the same time bring out, in new_assets, the assets of this episode that are not yet registered, each with its handling decision. "
        "Ad projects have no script_plan. With dry_run=true the prompt is returned directly and no generation task is submitted." + TEXT_OUTPUT_TRUNCATED_NOTE
    ),
    request_model=GenerateScriptPlanRequest,
    migration=BLOCKED,
    domain_key=_TEXT_GENERATION,
    handler=generate_script_plan,
    long_task=True,
    projection=_text_generation_structured,
)

CONFIRM_SCRIPT_REVIEW = ToolDeclaration(
    name="confirm_script_review",
    description=(
        "Confirm this episode's script_plan: convert it whole into the script (all shots pending authoring), register this episode's new assets according to the handling decisions in new_assets, "
        "and release prompt_authoring visual generation. "
        "Call only when the user has explicitly approved moving on to visual generation. When the episode already has a script, confirming overwrites it whole; without approval it returns"
        " script_overwrite_required, the receipt body is the server-generated loss list (the same text as the Web confirmation dialog), "
        "and params.script_overwrite.revision is the approval token; relay the list to the user first, and only after they agree confirm again with that revision as overwrite_revision."
    ),
    request_model=ConfirmScriptReviewRequest,
    migration=BLOCKED,
    domain_key=_TEXT_GENERATION,
    handler=confirm_script_review,
)

OPEN_DRAFT = ToolDeclaration(
    name=OPEN_DRAFT_TOOL_NAME,
    description=(
        "Read the specified draft; when the draft does not exist, create an editing copy from the corresponding formal document. Returns the full body, the violations from the last output or promotion check, "
        "the canonical revision, and the formal document's formal_revision (written the same way as the revision given by get_script_plan_content "
        "and the workflow plan)."
    ),
    request_model=DraftLocator,
    migration=BLOCKED,
    domain_key=_DRAFT,
    handler=open_draft,
)

PATCH_DRAFT = ToolDeclaration(
    name="patch_draft",
    description=(
        "Atomically replace the draft body against the canonical revision; on a revision conflict it refuses and writes nothing. "
        "Giving accept_formal_revision accepts concurrent changes to the formal document, and giving source updates the re-check scope; explicitly passing null for either also takes effect. "
        "The receipt carries the new body and revision, without violations: this tool does not re-check, and violations after the change are determined by promote_draft's full re-check."
    ),
    request_model=PatchDraftRequest,
    migration=BLOCKED,
    domain_key=_DRAFT,
    handler=patch_draft,
)

PROMOTE_DRAFT = ToolDeclaration(
    name=PROMOTE_TOOL_NAME,
    description="Fully re-validate the specified draft; if it passes, promote it to the formal file and clear the draft, otherwise refresh the violation report. Can be called repeatedly.",
    request_model=PromoteDraftRequest,
    migration=BLOCKED,
    domain_key=_DRAFT,
    handler=promote_draft,
)

DISCARD_DRAFT = ToolDeclaration(
    name="discard_draft",
    description="Discard the specified draft against the canonical revision; the draft is moved to the project trash, and the formal document stays unchanged.",
    request_model=DiscardDraftRequest,
    migration=BLOCKED,
    domain_key=_DRAFT,
    handler=discard_draft,
)

SCRIPT_AUTHORING_TOOLS = (
    GENERATE_EPISODE_SCRIPT,
    GENERATE_SCRIPT_PLAN,
    CONFIRM_SCRIPT_REVIEW,
    OPEN_DRAFT,
    PATCH_DRAFT,
    PROMOTE_DRAFT,
    DISCARD_DRAFT,
)

__all__ = [
    "CONFIRM_SCRIPT_REVIEW",
    "DISCARD_DRAFT",
    "GENERATE_EPISODE_SCRIPT",
    "GENERATE_SCRIPT_PLAN",
    "OPEN_DRAFT",
    "PATCH_DRAFT",
    "PROMOTE_DRAFT",
    "SCRIPT_AUTHORING_TOOLS",
]
