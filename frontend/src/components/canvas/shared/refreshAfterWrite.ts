import type { TFunction } from "i18next";
import { useAppStore } from "@/stores/app-store";
import { useProjectsStore, type RefreshProjectResult } from "@/stores/projects-store";

/**
 * Refresh project data after a successful write (instant actions and edit unit saves).
 *
 * The write is already committed; refreshing is a separate follow-up step: `refreshProject` reports failure as a settled value rather than rejecting.
 * Without a notice the UI stays on stale data, looking as if the write did not take effect, and creators tend to repeat the action.
 * `cancelled` means the project was switched away; stay silent. The settled value is returned as is, so callers can decide whether follow-up actions are still reliable.
 *
 * Writes that already have a dedicated notice may skip this function and consume `refreshProject`'s settled value with their own notice:
 * shot saves (via `PartialSaveError`), and timeline shot add/remove, rename, merge and delete.
 *
 * 写入（即时动作与编辑单元的保存）成功后刷新项目数据。
 *
 * 写入已提交，刷新是独立的后续步骤：`refreshProject` 以结算值报告失败而不 reject。
 * 不提示的话界面停在旧数据上，看着像这次写入没生效，创作者容易重复操作。
 * `cancelled` 是项目已切走，静默。结算值原样返回，调用方据此决定后续动作是否仍可靠。
 *
 * 已有专属提示的写入可以不经本函数，直接消费 `refreshProject` 的结算值并自行提示：
 * 分镜保存（经 `PartialSaveError`），以及时间线的增删分镜、改名、合并、删除。
 */
export async function refreshAfterWrite(
  projectName: string,
  t: TFunction,
  options?: { invalidateKeys?: string[] },
): Promise<RefreshProjectResult> {
  const result = await useProjectsStore.getState().refreshProject(projectName, options);
  if (result === "failed") useAppStore.getState().pushToast(t("common:write_refresh_failed"), "warning");
  return result;
}
