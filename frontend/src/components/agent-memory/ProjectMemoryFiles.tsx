import { useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";

import { projectSettingsPath } from "@/app-routes";
import { SecondaryRailList } from "@/components/shared/master-detail/SecondaryRail";
import type { AgentMemoryScope } from "@/types/agent-memory";

import { MemoryDetail, MemoryLoadState, useMemoryWorkspace } from "./MemoryWorkspace";
import { MEMORY_FILE_PARAM } from "./memory-files";

/**
 * File area of the "Project memory" tab in project settings: the width-limited page has no room for a secondary column; on the left is a file list without its own scrolling, on the right the same editor,
 * scrolled by the page body, with the editor sticky at the top. The editor is a standalone editing unit and does not use the shell's bottom save bar.
 *
 * 项目设置「项目记忆」分页的文件区：限宽页里放不下二级栏，左侧是不自带滚动的文件列表，右侧是同一个编辑器，
 * 由页面主体滚动，编辑器吸顶。编辑器是独立的编辑单元，不使用外壳底行的保存栏。
 */
export function ProjectMemoryFiles({ projectName }: { projectName: string }) {
  const { t } = useTranslation("dashboard");
  const scope = useMemo<AgentMemoryScope>(() => ({ level: "project", projectName }), [projectName]);
  const hrefFor = useCallback(
    (file: string) => projectSettingsPath(projectName, "memory", { [MEMORY_FILE_PARAM]: file }),
    [projectName],
  );
  const workspace = useMemoryWorkspace(scope, hrefFor);
  const loadState = <MemoryLoadState workspace={workspace} />;
  if (workspace.error !== null || workspace.overview === null) return loadState;

  return (
    <div className="flex items-start gap-6">
      <nav aria-label={t("agent_memory_files")} className="w-52 shrink-0">
        <SecondaryRailList group={workspace.group} activeId={workspace.activeId} replace />
      </nav>
      {/* With many files the list grows with the page; the editor sticks to the top of the body, so it stays in view when picking a file at the end of the list / 文件多时列表随页面变长，编辑器吸在主体顶部，滚到列表末尾选文件时编辑器仍在视野里 */}
      <div className="sticky top-6 min-w-0 flex-1">
        <MemoryDetail workspace={workspace} layout="flow" />
      </div>
    </div>
  );
}
