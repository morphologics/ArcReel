import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import type { ModelCandidatesResponse } from "@/types/system";

/**
 * Loading state for task-type bucket candidates (docs/adr/0054): both global settings and project settings consume the same global data,
 * so loading and failure handling live here and call sites just take the result.
 *
 * The three states don't overlap, because the fork between "refinement area silently degrades" and "explicit error" depends entirely on them:
 *   candidates=null, error=false  -> still loading (or not triggered yet), keep silent degradation
 *   candidates!=null              -> success; an empty bucket also goes here, no error
 *   error=true                    -> fetch failed, the call site renders the error text and a retry entry
 *
 * 任务类型桶候选（docs/adr/0054）的加载态：全局设置与项目设置两处都消费同一份全局数据，
 * 加载与失败叙事收在这里，调用点只拿结果。
 *
 * 三态互不重叠，因为「细分区静默降级」与「显式报错」的分岔全靠它们区分：
 *   candidates=null, error=false  → 仍在加载（或尚未触发），沿用静默降级
 *   candidates!=null              → 成功；桶为空也走这支，不报错
 *   error=true                    → 拉取失败，调用点渲染错误文案与重试入口
 */
export interface ModelCandidatesState {
  candidates: ModelCandidatesResponse | null;
  error: boolean;
  /** A retry is in flight; the retry button grays out on this, otherwise clicking gives no feedback on slow responses. / 重试在途；重试按钮据此灰化，否则慢响应下点击没有任何反馈。 */
  retrying: boolean;
  /** Fetch once; component unmount and the next call both cancel the in-flight request. / 拉取一次；组件卸载与下一次调用都会作废在途请求。 */
  reload: () => Promise<void>;
}

/**
 * Not fetched automatically on mount: the two call sites trigger at different times — project settings fetches once on mount, global settings
 * folds it into the whole-page re-fetch after save. The call site decides when to `reload()`; the hook only guarantees no stale response writes back.
 *
 * Candidate responses carry provider / model names written per Accept-Language, so `reload`'s identity changes with language:
 * call sites usually put it in effect dependencies, so a language switch re-fetches automatically without also re-fetching the form itself (which would wipe
 * unsaved edits).
 *
 * 不在挂载时自动拉取：两处调用点的触发时机不同——项目设置只在挂载时取一次，全局设置把它
 * 并进保存后的整页重取里。由调用点决定何时 `reload()`，hook 只保证不会有过期响应回写。
 *
 * 候选响应带按 Accept-Language 成文的供应商名 / 模型名，故 `reload` 的标识随语言变化：
 * 调用点惯常把它放进 effect 依赖，语言切换即自动重取，而无需连带重取表单本体（那会清空
 * 未保存的编辑）。
 */
export function useModelCandidates(): ModelCandidatesState {
  const { i18n } = useTranslation();
  const language = i18n.language;
  const [candidates, setCandidates] = useState<ModelCandidatesResponse | null>(null);
  const [error, setError] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  const reload = useCallback(async () => {
    // The taker rotates the controller (see docs/standards/frontend.md): a retry may fire before the previous round's
    // request has returned, and the old response is already stale when it arrives.
    // 接管方轮换 controller（见 docs/standards/frontend.md）：重试可能在上一轮
    // 请求尚未回来时触发，旧响应回来时已经过期。
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setRetrying(true);
    try {
      const next = await API.getModelCandidates({ signal: controller.signal });
      // Checkpoint before writing state after the network await: abort may happen after the response has already resolved.
      // 网络 await 之后的写 state 断点：abort 可能发生在响应已 resolve 之后。
      if (controller.signal.aborted) return;
      setCandidates(next);
      setError(false);
    } catch {
      if (controller.signal.aborted) return;
      setCandidates(null);
      setError(true);
    } finally {
      // The superseded one yields: after cancellation don't reset shared state, or it would switch off the in-flight flag the taker just lit.
      // 被接管方让位：作废后不复位共享状态，否则会灭掉接管方刚点亮的在途标记。
      if (!controller.signal.aborted) setRetrying(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- language isn't used in the function body; it only swaps reload's identity on language switch, driving the call site's effect to re-fetch / language 不进函数体，只用来在语言切换时换掉 reload 的标识，驱动调用点的 effect 重取
  }, [language]);

  useEffect(() => () => abortRef.current?.abort(), []);

  return { candidates, error, retrying, reload };
}
