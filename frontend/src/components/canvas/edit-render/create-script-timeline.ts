import type { TFunction } from "i18next";
import { API } from "@/api";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import type { RefreshProjectResult } from "@/stores/projects-store";
import type { EditTimelineReadout } from "@/types/edit-timeline";

/** Display name of an edit timeline created from the script: "Full version", with sequence numbers added when the name already exists in the episode. / 按脚本新建的剪辑时间线显示名：「完整版」，集内已有同名时依次加序号。 */
function nextEditTimelineName(base: string, numbered: (n: number) => string, taken: string[]): string {
  const names = new Set(taken.map((name) => name.toLocaleLowerCase()));
  if (!names.has(base.toLocaleLowerCase())) return base;
  for (let n = 2; ; n += 1) {
    const candidate = numbered(n);
    if (!names.has(candidate.toLocaleLowerCase())) return candidate;
  }
}

/**
 * Mechanically create an edit timeline from the current script (whole clips, all hard cuts), bypassing the Agent; then refresh the project,
 * so workflow progress and the edit view reread. Both the WorkflowPanel "Edit" row and the edit view empty state go through here.
 * A refresh failure has already been reported; `refreshed` lets the caller decide whether to still report success.
 *
 * 按当前脚本机械新建一条剪辑时间线（整段使用、全部硬切），不经过 Agent；随后刷新项目，
 * 制作进度与剪辑视图据此重新读取。WorkflowPanel「剪辑」行与剪辑视图空状态都走这里。
 * 刷新失败时已提示，`refreshed` 供调用方决定是否还报告成功。
 */
export async function createScriptEditTimeline(
  projectName: string,
  episode: number,
  t: TFunction,
): Promise<{ created: EditTimelineReadout; refreshed: RefreshProjectResult }> {
  const { timelines } = await API.listEditTimelines(projectName, episode);
  const name = nextEditTimelineName(
    t("workflow:edit_timeline_default_name"),
    (number) => t("workflow:edit_timeline_default_name_numbered", { number }),
    timelines.map((timeline) => timeline.name),
  );
  const created = await API.createEditTimeline(projectName, episode, name);
  return { created, refreshed: await refreshAfterWrite(projectName, t) };
}
