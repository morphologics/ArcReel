import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API, ApiRequestError } from "@/api";
import type { ScriptOverwrite, ScriptReviewState } from "@/types";
import { useAppStore } from "@/stores/app-store";

/** A draft the panel can edit: one variant of `ScriptReviewState.content`. / 面板可编辑的草稿：`ScriptReviewState.content` 的某个变体。 */
type ScriptReviewContent = NonNullable<ScriptReviewState["content"]>;

function scriptReviewErrorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "";
}

/** When confirmation was rejected because the episode already has a final script and overwrite wasn't approved, extract the overwrite consequences listed by the server. / 确认被拒是因为该集已有正式脚本、未认可覆盖时，取出服务端列出的覆盖后果。 */
function overwriteFromError(err: unknown): ScriptOverwrite | null {
  if (!(err instanceof ApiRequestError) || err.status !== 409) return null;
  const diagnostic = err.diagnostic;
  if (diagnostic == null || typeof diagnostic !== "object") return null;
  const overwrite = (diagnostic as { script_overwrite?: unknown }).script_overwrite;
  return overwrite != null && typeof overwrite === "object" ? (overwrite as ScriptOverwrite) : null;
}

/** Saving was rejected because the script plan is confirmed and read-only. / 保存被拒是因为脚本规划已确认、只读。 */
function isScriptPlanConfirmedError(err: unknown): boolean {
  if (!(err instanceof ApiRequestError) || err.status !== 409) return false;
  const diagnostic = err.diagnostic;
  return diagnostic != null && typeof diagnostic === "object" && (diagnostic as { code?: unknown }).code === "script_plan_confirmed";
}

/** A confirmed script plan without an invalid draft is read-only; unsaved local edits have nowhere to go, so external refreshes adopt the server content directly. / 已确认、无待修复草稿的脚本规划只读，本地未保存的编辑无处可存，外部刷新时直接采用服务端内容。 */
function isReadOnly(state: ScriptReviewState): boolean {
  return state.status === "confirmed" && state.quarantine == null;
}

/** Whether content has unsaved edits: compared by serialization; draft is cloned from server content, so key order is stable. / 内容是否有未保存编辑：以序列化比对，draft 由 server content 克隆而来，键序稳定。 */
function isDirty(draft: unknown, serverContent: unknown): boolean {
  if (draft == null) return false;
  return JSON.stringify(draft) !== JSON.stringify(serverContent);
}

function clone<TDraft>(content: TDraft | null): TDraft | null {
  return content ? structuredClone(content) : null;
}

interface ScriptReviewDraftOptions<TDraft extends ScriptReviewContent> {
  projectName: string;
  episode: number;
  /**
   * Extract from the server review state the content this panel can edit; returns null when the shape is not this panel's variant.
   * Must be a stable reference (module-level function or `useCallback`) — it is a dependency of the fetch effect.
   *
   * 从服务端审核态取出本面板可编辑的那份内容；形状不属于本面板的变体时返回 null。
   * 须是稳定引用（模块级函数或 `useCallback`）——它参与拉取 effect 的依赖。
   */
  selectContent: (state: ScriptReviewState) => TDraft | null;
  /** Panel-specific handling after a successful confirmation (including the success hint); failure hints are given uniformly by this hook. / 确认成功后的面板专属处理（含成功提示）；失败提示由本 hook 统一给出。 */
  onConfirmed: () => void;
}

interface ScriptReviewDraftHandle<TDraft extends ScriptReviewContent> {
  /** The most recently fetched server review state; null until any response arrives. / 最近一次拉取到的服务端审核态；尚未拿到任何响应时为 null。 */
  state: ScriptReviewState | null;
  draft: TDraft | null;
  setDraft: React.Dispatch<React.SetStateAction<TDraft | null>>;
  /** The draft differs from the server content it is based on, i.e. there are unsaved edits. / 草稿与其所基于的服务端内容不一致，即有未保存编辑。 */
  dirty: boolean;
  loading: boolean;
  loadError: { message: string } | null;
  saving: boolean;
  confirming: boolean;
  /** A save / confirm request is in flight: callers lock the edit controls on this, so the echo doesn't overwrite edits made after the request was sent. / 保存 / 确认请求在途：调用方据此锁住编辑控件，避免回显覆盖请求发出后的新编辑。 */
  busy: boolean;
  retry: () => void;
  /** Silently re-fetch the server review state (after the draft is adopted or discarded, both the final content and the draft info have changed). / 静默重新拉取服务端审核态（草稿被采用或丢弃后，正式内容与草稿信息都已变化）。 */
  refresh: () => void;
  save: () => Promise<void>;
  /**
   * Confirm and convert the whole thing into the final script. When the episode already has a final script, `overwriteRevision` approving the overwrite must be passed; when it's missing or stale and
   * the server rejects it, write the server-listed current overwrite consequences back into `state.script_overwrite`, and the panel switches to a danger confirmation.
   * Failures are handled in place (toast) and not rethrown; returns whether it succeeded, so the caller decides whether to close the confirmation.
   *
   * 确认并整份转为正式脚本。该集已有正式脚本时须传认可覆盖的 `overwriteRevision`；缺失或已过期而被
   * 服务端拒绝时，把服务端列出的当前覆盖后果写回 `state.script_overwrite`，面板据此改呈 danger 确认。
   * 失败已就地处置（toast）、不重抛；返回是否成功，供调用方决定是否收起确认框。
   */
  confirm: (options?: { overwriteRevision?: string }) => Promise<boolean>;
}

/**
 * Draft state machine of the script_plan -> prompt_authoring content review panel: fetch / external refresh / dirty flag / adopt / save / confirm.
 * Shared by `ScriptReviewGate` (drama, narration) and `ReferenceScriptPlanPreviewPanel` (reference_video);
 * panels keep only their own presentation and specific interactions.
 *
 * script_plan → prompt_authoring 内容确认面板的草稿状态机：拉取 / 外部刷新 / 脏标记 / 采纳 / 保存 / 确认。
 * `ScriptReviewGate`（drama、narration）与 `ReferenceScriptPlanPreviewPanel`（reference_video）
 * 共用，面板只保留各自的呈现与专属交互。
 */
export function useScriptReviewDraft<TDraft extends ScriptReviewContent>({
  projectName,
  episode,
  selectContent,
  onConfirmed,
}: ScriptReviewDraftOptions<TDraft>): ScriptReviewDraftHandle<TDraft> {
  const { t } = useTranslation("dashboard");
  const pushToast = useAppStore((s) => s.pushToast);
  const draftRevision = useAppStore((s) => s.getEntityRevision(`draft:episode_${episode}_script_plan`));

  const [state, setState] = useState<ScriptReviewState | null>(null);
  const [draft, setDraft] = useState<TDraft | null>(null);
  // Baseline fingerprint submitted on save: bound to **the server content the current draft is based on**, advanced only when the draft adopts server content.
  // state.fingerprint can't be used instead — an external refresh with unsaved edits updates state but keeps the old draft, and submitting
  // state.fingerprint then would take someone else's newly written content as baseline; OCC would let it through and silently overwrite their changes.
  // 保存时提交的基线指纹：绑定在**当前草稿所基于的那份服务端内容**上，只在草稿采用服务端内容时推进。
  // 不能改用 state.fingerprint——有未保存编辑时的外部刷新会更新 state 却保留旧草稿，届时提交
  // state.fingerprint 等于拿别人新写的内容当基线，OCC 会放行并静默覆盖对方的修改。
  const [baseFingerprint, setBaseFingerprint] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<{ message: string } | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [saving, setSaving] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const serverContent = state ? selectContent(state) : null;
  const dirty = useMemo(() => isDirty(draft, serverContent), [draft, serverContent]);
  const busy = saving || confirming;

  // Mirror dirty into a ref so the fetch effect below reads the latest value without listing dirty in deps (otherwise every edit would re-fetch).
  // 把 dirty 镜像进 ref，供下方拉取 effect 读取最新值，而无需把 dirty 列入 deps（否则每次编辑都会重新拉取）。
  const dirtyRef = useRef(false);
  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  // Controller and in-flight flag of the in-flight fetch, for adopt to cancel and to decide on a follow-up fetch.
  // 在途拉取的 controller 与在途标记，供 adopt 作废与补拉判定。
  const loadControllerRef = useRef<AbortController | null>(null);
  const loadInFlightRef = useRef(false);

  // Adopt server content as the new draft (deep clone, to avoid sharing references with server state). Called after user actions (save / confirm),
  // always overwriting the local draft; setDraft is given a value rather than an updater, staying pure without reading/writing refs inside an updater.
  // Any fetch issued before adopting is cancelled: the server state it reads may not include this write, and when it returns dirtyRef is already
  // false due to the adoption, so it would write back draft and baseFingerprint from old content — the draft regresses and the next save hits OCC with an old fingerprint.
  // But if the cancelled fetch was the one triggered by an external event (the Agent changing script_plan's revision), that revision is already consumed
  // and no request will make it up, so the external update would be lost; hence when an in-flight fetch really was cancelled, issue another round to fetch it back.
  // 采用服务端内容为新草稿（深克隆，避免与服务端态共享引用）。用户主动动作（保存 / 确认）后调用，
  // 总是覆盖本地草稿；setDraft 传值而非更新器，保持纯净、不在更新器内读写 ref。
  //
  // 采纳前发出的拉取一律作废：它读到的服务端态未必包含本次写入，回来时 dirtyRef 已因采纳而为
  // false，会按旧内容回写 draft 与 baseFingerprint——草稿回退，且下次保存拿旧指纹撞 OCC。
  // 但作废掉的若是外部事件（Agent 改 script_plan 的 revision）触发的那次拉取，该 revision 已被消费、
  // 不会再有请求补上，外部更新就此丢失；故确有在途拉取被作废时补发一轮，把它重新拉回来。
  const adopt = useCallback(
    (next: ScriptReviewState) => {
      const abandonedLoad = loadInFlightRef.current;
      loadControllerRef.current?.abort();
      loadInFlightRef.current = false;
      setState(next);
      setDraft(clone(selectContent(next)));
      setBaseFingerprint(next.fingerprint);
      if (abandonedLoad) setReloadNonce((n) => n + 1);
    },
    [selectContent],
  );

  const retry = useCallback(() => {
    setLoadError(null);
    setLoading(true);
    setReloadNonce((n) => n + 1);
  }, []);

  const refresh = useCallback(() => {
    setReloadNonce((n) => n + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    loadControllerRef.current = controller;
    loadInFlightRef.current = true;
    // Once any response (empty or content) has been received, revision-triggered re-fetches refresh silently without flashing the loading state;
    // 已拿到过任一响应（空态或内容态）后，revision 触发的重新拉取静默刷新、不闪加载态；
    const hadResponse = state != null;
    // When there is real content on screen to keep, refresh failures are silently ignored to preserve the user's view; only with no content (first screen, or empty state) does failure enter the error state.
    // 屏上有真实内容可保留时，刷新失败静默保留、不破坏用户视图；无内容（首屏，或空态）时失败才进错误态。
    const hasContent = draft != null;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- the loading state must be lit synchronously before the first-screen fetch; with an existing response refresh silently / 首屏拉取前须同步点亮加载态，已有响应时静默刷新
    if (!hadResponse) setLoading(true);
    API.getScriptReview(projectName, episode, { signal })
      .then((next) => {
        // The window where abort happens after the response has resolved: re-check before writing state so the departing round doesn't write back.
        // 响应已 resolve 之后才 abort 的窗口：写 state 前复核，避免离场的这轮回写。
        if (signal.aborted) return;
        setLoadError(null);
        setState(next);
        // External refresh (mount / revision triggered by the Agent changing script_plan): adopt server content when the user has no unsaved edits or the content is confirmed read-only,
        // otherwise only update server state and keep the user's draft. Reading dirtyRef inside the effect is safe (not during render).
        // 外部刷新（挂载 / Agent 改 script_plan 触发的 revision）：用户无未保存编辑、或内容已确认只读时采用
        // 服务端内容，否则仅更新服务端态、保留用户草稿。dirtyRef 读取在 effect 内安全（非 render 期）。
        if (!dirtyRef.current || isReadOnly(next)) {
          setDraft(clone(selectContent(next)));
          setBaseFingerprint(next.fingerprint);
        }
      })
      .catch((err: unknown) => {
        if (signal.aborted) return;
        // No real content on screen (first-screen failure, or revision refresh failing after the empty state) -> error state (distinct from empty) + retry;
        // a silent refresh failure with existing content keeps the current content without breaking the user's view.
        // 屏上无真实内容（首屏失败，或空态后 revision 刷新失败）→ 错误态（区别于空态）+ 重试；
        // 已有内容的静默刷新失败则保留现有内容，不破坏用户视图。
        if (!hasContent) setLoadError({ message: scriptReviewErrorMessage(err) });
      })
      .finally(() => {
        // Cleanup yields to the taker: once cancelled, don't touch the in-flight flag or loading, or it would interrupt the new round's loading state.
        // 收尾让位给接管方：已作废就不碰在途标记与 loading，否则会打断新一轮加载态。
        if (!signal.aborted) {
          loadInFlightRef.current = false;
          setLoading(false);
        }
      });
    return () => {
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- state/draft are only used to choose the loading and error branches; adding them to deps would re-fetch after every refresh in a loop / state/draft 仅用于决定加载态与错误态分支，加入 deps 会在每次刷新后重新拉取造成循环
  }, [projectName, episode, draftRevision, reloadNonce, selectContent]);

  const save = useCallback(async () => {
    if (!draft) return;
    setSaving(true);
    try {
      adopt(await API.saveScriptReviewContent(projectName, episode, draft, baseFingerprint));
      pushToast(t("dashboard:review_saved"), "success");
    } catch (err) {
      // The episode was confirmed during editing: re-fetch, and the panel becomes read-only. / 编辑期间该集已被确认：重新拉取，面板随之转为只读。
      if (isScriptPlanConfirmedError(err)) setReloadNonce((n) => n + 1);
      pushToast(scriptReviewErrorMessage(err) || t("dashboard:save_failed", { message: "" }), "error");
    } finally {
      setSaving(false);
    }
  }, [draft, baseFingerprint, projectName, episode, adopt, pushToast, t]);

  const confirm = useCallback(async (options: { overwriteRevision?: string } = {}): Promise<boolean> => {
    setConfirming(true);
    try {
      if (dirty && draft) {
        adopt(await API.saveScriptReviewContent(projectName, episode, draft, baseFingerprint));
      }
      adopt(await API.confirmScriptReview(projectName, episode, options));
      onConfirmed();
      return true;
    } catch (err) {
      const overwrite = overwriteFromError(err);
      if (overwrite) {
        setState((prev) => (prev ? { ...prev, script_overwrite: overwrite } : prev));
      }
      pushToast(scriptReviewErrorMessage(err) || t("dashboard:review_confirm_failed"), "error");
      return false;
    } finally {
      setConfirming(false);
    }
  }, [dirty, draft, baseFingerprint, projectName, episode, adopt, onConfirmed, pushToast, t]);

  return {
    state,
    draft,
    setDraft,
    dirty,
    loading,
    loadError,
    saving,
    confirming,
    busy,
    retry,
    refresh,
    save,
    confirm,
  };
}
