"""Drift guard: project field names must appear verbatim in the corresponding subagent .md.

漂移防御：项目字段名必须逐字出现在对应 subagent .md 中。"""

from pathlib import Path

import pytest

from lib.episode.episode_target_duration import EPISODE_TARGET_DURATION_FIELD
from lib.episode.episode_target_volume import EPISODE_TARGET_UNITS_FIELD

REPO = Path(__file__).resolve().parents[5]


#: The three script plan subagents that read the per-episode target duration from ``get_video_capabilities`` in Step 0,
#: plus the skill that lists the field in the ``patch_project`` allowlist, and the two video-workflow skills that convert
#: it into per-episode volume in the episode planning step.
#: 会在 Step 0 读 ``get_video_capabilities`` 里的单集目标时长的三条脚本规划子智能体，
#: 加上把该字段列进 ``patch_project`` 白名单的 skill，以及分集规划步骤里据它折算每集体量的
#: 两条 video-workflow skill。
EPISODE_TARGET_DURATION_READERS = (
    "agent_runtime_profile/.claude/agents/normalize-drama-script.md",
    "agent_runtime_profile/.claude/agents/split-narration-segments.md",
    "agent_runtime_profile/.claude/agents/split-reference-video-units.md",
    "agent_runtime_profile/.claude/skills/manage-project/SKILL.md",
    "agent_runtime_profile/.claude/skills/video-workflow/SKILL.narration.md",
    "agent_runtime_profile/.claude/skills/video-workflow/SKILL.drama.md",
)

#: The skill that checks the per-episode target volume before episode planning and writes the field via ``patch_project``.
#: 会在分集规划前核对每集目标体量、并经 ``patch_project`` 写入该字段的 skill。
EPISODE_TARGET_UNITS_READERS = (
    "agent_runtime_profile/.claude/skills/manage-project/SKILL.md",
    "agent_runtime_profile/.claude/skills/video-workflow/SKILL.narration.md",
    "agent_runtime_profile/.claude/skills/video-workflow/SKILL.drama.md",
)

#: The episode planning fallback note must mention both fields on the same line, so it does not pass just because the field names appear elsewhere in the file.
#: 分集规划回退说明必须在同一行同时说明两个字段，避免只因文件别处出现字段名而误通过。
EPISODE_TARGET_FALLBACK_READERS = (
    "agent_runtime_profile/.claude/references/generation-modes.md",
    "agent_runtime_profile/.claude/skills/manage-project/SKILL.md",
    "agent_runtime_profile/.claude/skills/video-workflow/SKILL.narration.md",
    "agent_runtime_profile/.claude/skills/video-workflow/SKILL.drama.md",
    "agent_runtime_profile/CLAUDE.narration.md",
    "agent_runtime_profile/CLAUDE.drama.md",
)


@pytest.mark.parametrize("relative_path", EPISODE_TARGET_DURATION_READERS)
def test_episode_target_duration_field_name_is_mirrored(relative_path: str) -> None:
    """Field names are a machine contract hard-coded in the .md: change the Python constant without syncing the .md and the subagent will read a key that does not exist.

    字段名是 .md 里写死的机器契约：改了 Python 常量而没同步 .md，子智能体会去读一个不存在的键。"""
    md = (REPO / relative_path).read_text(encoding="utf-8")

    assert EPISODE_TARGET_DURATION_FIELD in md, f"{EPISODE_TARGET_DURATION_FIELD} 未在 {relative_path} 中找到（漂移）"


@pytest.mark.parametrize("relative_path", EPISODE_TARGET_UNITS_READERS)
def test_episode_target_units_field_name_is_mirrored(relative_path: str) -> None:
    """Same as above: the field name is hard-coded in the skill's patch_project call example and check step; changing the constant requires syncing the .md.

    同上：字段名写死在 skill 的 patch_project 调用示例与核对步骤里，改常量须同步 .md。"""
    md = (REPO / relative_path).read_text(encoding="utf-8")

    assert EPISODE_TARGET_UNITS_FIELD in md, f"{EPISODE_TARGET_UNITS_FIELD} 未在 {relative_path} 中找到（漂移）"


@pytest.mark.parametrize("relative_path", EPISODE_TARGET_FALLBACK_READERS)
def test_episode_target_duration_fallback_is_mirrored(relative_path: str) -> None:
    """Every episode planning note states the relation of the two fields on the same line (explicit volume first, duration only as a fallback).

    Only co-occurrence of the field names on one line is checked: how the fallback relation is worded is out of scope; deleting the note or leaving only one field turns red.

    每份分集规划说明都在同一行写明两个字段的关系（显式体量优先、时长仅作回退）。

    只认字段名在同一行共现：怎么措辞回退关系不在断言范围内，删掉这条说明或只剩一个字段会红。
    """
    lines = (REPO / relative_path).read_text(encoding="utf-8").splitlines()

    assert any(EPISODE_TARGET_UNITS_FIELD in line and EPISODE_TARGET_DURATION_FIELD in line for line in lines), (
        f"{relative_path} 未在同一段说明 {EPISODE_TARGET_UNITS_FIELD} 与 {EPISODE_TARGET_DURATION_FIELD} 的回退关系"
    )
