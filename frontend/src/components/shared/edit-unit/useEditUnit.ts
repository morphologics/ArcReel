import { useCallback, useContext, useEffect, useState } from "react";

import { errMsg } from "@/utils/async";

import { EditUnitRetentionContext, useRetainWhile } from "./edit-unit-retention";

import { useLeaveGuard } from "./LeaveGuard";

/** Save status: "Saved" shows briefly then returns to idle. / 保存状态：「已保存」短暂显示后回到 idle。 */
export type SaveStatus = "idle" | "saving" | "saved" | "error";

/** How long "Saved" stays on the save bar or notice bar. / 「已保存」在保存栏或提示条上停留的时长。 */
const SAVED_DISPLAY_MS = 2000;

export interface EditUnitOptions<T> {
  /**
   * Saved content: the load result, and later new values from SSE pushes and Agent rewrites.
   * With no unsaved changes new values take effect directly; with unsaved changes the changes are kept and the external update is flagged.
   *
   * 已保存的内容：加载结果，以及之后 SSE 推送、Agent 改写带来的新值。
   * 没有未保存修改时新值直接生效；有未保存修改时保留修改，并标出外部更新。
   */
  source: T;
  /**
   * Submit unsaved changes. `savedValue` is the saved content before submitting, for building a per-field incremental PATCH.
   * Returns the content after the server saves; if nothing is returned the submitted content is used. Throwing means save failed; the message shows on the save bar or notice bar.
   * Throw `PartialSaveError` when a save is submitted in several steps and earlier steps are already on disk. Must be a stable reference (`useCallback`).
   *
   * 提交未保存修改。`savedValue` 是提交前的已保存内容，供按字段生成增量 PATCH。
   * 返回服务端保存后的内容；不返回时以提交的内容为准。抛错即保存失败，错误信息显示在保存栏或提示条上。
   * 一次保存分几步提交、前几步已经落盘时抛 `PartialSaveError`。需传稳定引用（`useCallback`）。
   */
  save: (value: T, savedValue: T) => Promise<T | void>;
  /** Whether two contents are equal, by default compared via JSON serialization. Must be a stable reference (or module-level function). / 判断两份内容是否相同，默认按 JSON 序列化比较。需传稳定引用（或模块级函数）。 */
  isEqual?: (a: T, b: T) => boolean;
  /** Title of the leave guard dialog, e.g. "Shot 3 has unsaved changes"; defaults to a generic title. / 离开拦截对话框的标题，如「分镜 3 有未保存的修改」；缺省为通用标题。 */
  leaveTitle?: string;
  /**
   * In-app navigations for which this returns true are not intercepted; for navigations that do not unmount this edit unit (e.g. paging within the same edit unit).
   * The argument is the target path with query string. Must be a stable reference.
   *
   * 对返回 true 的应用内导航不拦截，用于不会卸载本编辑单元的跳转（如同一编辑单元的分页切换）。
   * 参数是带查询串的目标路径。需传稳定引用。
   */
  allowNavigation?: (to: string) => boolean;
}

export interface SaveAndGenerateOptions {
  /** Confirmation before saving (e.g. "regenerating invalidates downstream"); when it returns false nothing is saved or generated. / 保存前的确认（如「重新生成会让下游失效」），返回 false 时什么都不保存、不生成。 */
  confirm?: () => boolean | Promise<boolean>;
}

export interface EditUnit<T> {
  /** Current content, including unsaved changes. / 当前内容，包含未保存修改。 */
  value: T;
  /** Saved content. Compare with `value` when judging changes by a subset of fields (e.g. sidebar tab change markers). / 已保存的内容。按字段子集判断修改（如侧栏分页的修改标记）时与 `value` 比较。 */
  savedValue: T;
  setValue: (next: T | ((prev: T) => T)) => void;
  /** Has unsaved changes. / 有未保存修改。 */
  dirty: boolean;
  status: SaveStatus;
  /** Reason for the most recent save failure; cleared on further editing, discard or a successful save. / 最近一次保存失败的原因；再次编辑、放弃或保存成功时清除。 */
  error: string | null;
  /** Saved content was updated externally while there are unsaved changes. Discarding adopts the new content. / 有未保存修改期间，已保存内容被外部更新。放弃修改即采用新内容。 */
  externallyUpdated: boolean;
  /** Explanation when the current unit was removed or replaced externally and the visible content is retained. / 外部移除或替换了当前单元，保留可见内容时的说明。 */
  externalChangeMessage?: string;
  /** Submit unsaved changes, returns whether it succeeded; returns true directly when there are no changes. / 提交未保存修改，返回是否成功；没有修改时直接返回 true。 */
  save: () => Promise<boolean>;
  /** Discard unsaved changes and return to the latest saved content. / 丢弃未保存修改，回到最新的已保存内容。 */
  discard: () => void;
  /**
   * "Save and generate": confirm (if needed) -> save (if changed) -> generate.
   * Nothing is saved when the confirmation is cancelled, and nothing generated when saving fails; returns whether generation ran.
   *
   * 「保存并生成」：确认（如需要）→ 保存（有修改时）→ 生成。
   * 确认被取消时什么都不保存，保存失败时不生成；返回是否执行了生成。
   */
  saveAndGenerate: (generate: () => unknown, options?: SaveAndGenerateOptions) => Promise<boolean>;
}

interface UnitState<T> {
  value: T;
  saved: T;
  /** The `source` seen most recently, used to recognize external updates. / 最近一次见到的 `source`，用来识别外部更新。 */
  seenSource: T;
  status: SaveStatus;
  error: string | null;
  externallyUpdated: boolean;
}

/**
 * The save was only partly persisted (e.g. project PATCH succeeded, then the reference image upload failed): `saved` becomes the new saved content,
 * discarding returns to it and saving again uses it as the baseline; `value` is the content still unsaved, defaulting to the submitted content.
 * Throwing only a plain error would leave the saved content at its pre-save state; after discarding, the form shows old values and the next save writes them back.
 *
 * 保存只落盘了一部分（如项目 PATCH 成功、随后的参考图上传失败）：`saved` 成为新的已保存内容，
 * 放弃修改回到它，再次保存也以它为基准；`value` 是仍未保存的内容，缺省保留提交的内容。
 * 只抛普通错误会让已保存内容停在保存前，放弃修改后表单显示旧值，下次保存再把旧值写回。
 */
export class PartialSaveError<T> extends Error {
  readonly saved: T;
  readonly value: T | undefined;

  constructor(message: string, { saved, value, cause }: { saved: T; value?: T; cause?: unknown }) {
    super(message, { cause });
    this.name = "PartialSaveError";
    this.saved = saved;
    this.value = value;
  }
}

function jsonEqual<T>(a: T, b: T): boolean {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

/**
 * Edit unit: the UI scope submitted by one explicit save (a settings view, an asset, a shot detail, a memory file, etc.).
 * Fields in the unit go into unsaved changes first and are submitted together by `save`; registered with the leave guard automatically while mounted.
 *
 * 编辑单元：一次显式保存所提交的界面范围（一个设置视图、一张资产、一个分镜详情、一个记忆文件等）。
 * 单元内的字段先进入未保存修改，由 `save` 统一提交；挂载期间自动登记到离开拦截。
 */
export function useEditUnit<T>({
  source,
  save: submit,
  isEqual = jsonEqual,
  leaveTitle,
  allowNavigation,
}: EditUnitOptions<T>): EditUnit<T> {
  const [state, setState] = useState<UnitState<T>>(() => ({
    value: source,
    saved: source,
    seenSource: source,
    status: "idle",
    error: null,
    externallyUpdated: false,
  }));

  // External changes to saved content: synced during render (React's "adjust state from props" pattern),
  // to avoid rendering one frame with old content. Pushes equal to the current saved content (e.g. echo after our own save) are not external updates.
  // 已保存内容来自外部的变化：在渲染期同步（React 的「随 props 调整 state」写法），
  // 避免先用旧内容渲染一帧。与当前已保存内容相同的推送（如自己保存后的回显）不算外部更新。
  let current = state;
  if (!isEqual(source, state.seenSource)) {
    if (isEqual(source, state.saved)) {
      current = { ...state, seenSource: source };
    } else if (isEqual(state.value, state.saved)) {
      current = { ...state, seenSource: source, saved: source, value: source, externallyUpdated: false };
    } else {
      current = {
        ...state,
        seenSource: source,
        saved: source,
        externallyUpdated: !isEqual(state.value, source),
      };
    }
    setState(current);
  }

  const { value, saved, status, error } = current;
  const dirty = !isEqual(value, saved);
  const retention = useContext(EditUnitRetentionContext);

  useEffect(() => {
    if (status !== "saved") return;
    const timer = setTimeout(() => {
      setState((prev) => (prev.status === "saved" ? { ...prev, status: "idle" } : prev));
    }, SAVED_DISPLAY_MS);
    return () => clearTimeout(timer);
  }, [status]);

  const setValue = useCallback((next: T | ((prev: T) => T)) => {
    setState((prev) => ({
      ...prev,
      value: typeof next === "function" ? (next as (prev: T) => T)(prev.value) : next,
      status: prev.status === "saving" ? prev.status : "idle",
      error: null,
    }));
  }, []);

  const discard = useCallback(() => {
    setState((prev) => ({ ...prev, value: prev.saved, status: "idle", error: null, externallyUpdated: false }));
  }, []);

  const save = useCallback(async () => {
    if (!dirty) return true;
    const submitted = value;
    setState((prev) => ({ ...prev, status: "saving", error: null }));
    try {
      const result = await submit(submitted, saved);
      const next = result === undefined ? submitted : (result as T);
      setState((prev) => ({
        ...prev,
        saved: next,
        // If editing did not continue during the save, adopt the server-normalized content; if it did, keep the new changes
        // 保存期间没有继续编辑时，采用服务端规范化后的内容；继续编辑过的保留新修改
        value: isEqual(prev.value, submitted) ? next : prev.value,
        status: "saved",
        error: null,
        // When insisting on saving, last writer wins and the external-update flag is cleared along with it / 坚持保存时后写者生效，外部更新的标记随之清除
        externallyUpdated: false,
      }));
      return true;
    } catch (err) {
      if (err instanceof PartialSaveError) {
        const { saved: partial, value: unsaved } = err as PartialSaveError<T>;
        setState((prev) => ({
          ...prev,
          saved: partial,
          value: unsaved !== undefined && isEqual(prev.value, submitted) ? unsaved : prev.value,
          status: "error",
          error: errMsg(err),
          externallyUpdated: false,
        }));
        return false;
      }
      setState((prev) => ({ ...prev, status: "error", error: errMsg(err) }));
      return false;
    }
  }, [dirty, value, saved, submit, isEqual]);

  const saveAndGenerate = useCallback(
    async (generate: () => unknown, options?: SaveAndGenerateOptions) => {
      if (options?.confirm && !(await options.confirm())) return false;
      if (!(await save())) return false;
      await generate();
      return true;
    },
    [save],
  );

  // After external replacement the visible unit is detached from the real view, so the old view's allow rules can no longer apply to active navigation.
  // 外部替换后，可见单元已与真实视图分离，旧视图的放行规则不能继续用于主动跳转。
  const discarding = useLeaveGuard({ dirty, saving: status === "saving", save, discard, title: leaveTitle, allowNavigation: retention?.message ? undefined : allowNavigation });
  // A delete awaited after discarding removed this unit itself; do not treat it as external-removal retention
  // 放弃修改后等待落定的删除自己移除了本单元，不当作外部移除保留
  useRetainWhile((dirty || status === "saving") && !discarding);

  return {
    value,
    savedValue: saved,
    setValue,
    dirty,
    status,
    error,
    externallyUpdated: (current.externallyUpdated || Boolean(retention?.message)) && dirty,
    externalChangeMessage: retention?.message,
    save,
    discard,
    saveAndGenerate,
  };
}
