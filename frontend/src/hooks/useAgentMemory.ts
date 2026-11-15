import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { API } from "@/api";
import type { AgentMemoryOverview, AgentMemoryScope } from "@/types/agent-memory";
import { errMsg } from "@/utils/async";

export interface AgentMemoryState {
  /** Directory contents of the current scope; null until loaded or on load failure, and back to null immediately after a scope switch. / 当前 scope 的目录内容；未加载完成或加载失败时为 null，切换 scope 后立即回到 null。 */
  overview: AgentMemoryOverview | null;
  loading: boolean;
  /** Fetch failure message for the current scope; cleared on success. / 当前 scope 的拉取失败说明；成功后清空。 */
  error: string | null;
  /** Memory directory identifier; call sites pass it unchanged to the write APIs, so both levels share the same calls. / 记忆目录标识，调用点把它原样传给写类 API，两级因此共用同一批调用。 */
  scope: AgentMemoryScope;
  /** Re-fetch the list; the in-flight request is cancelled, and cancelled on unmount too. / 重新拉取列表；在途请求作废，卸载时一并取消。 */
  reload: () => Promise<void>;
}

/** One load's result and the scope that produced it; `target` is kept reference-stable by `useMemo`, so compare by reference. / 一次加载的结果与产出它的 scope；`target` 由 `useMemo` 保持引用稳定，故按引用比对。 */
interface LoadedMemory {
  target: AgentMemoryScope;
  data: AgentMemoryOverview | null;
  error: string | null;
}

/**
 * List loading state of one memory directory: fetched once on mount, re-fetched by the call site via `reload()` after writes.
 *
 * No polling or window-focus listening: memory is written separately by the creator and the Agent, and the server has no conflict detection; following external updates
 * would replace the editor content while the creator is editing.
 *
 * The argument is destructured by `level` and project name and the scope rebuilt: call sites usually pass an object literal in JSX, and listing it directly in
 * dependencies would re-fetch on every render.
 *
 * The result is stored paired with the scope that produced it and is visible only when they match: in the window after a project switch before the new directory's request returns,
 * the old directory's path and file list would hang under the new project's title, and opening one would send the old file name to the new directory.
 *
 * 一个记忆目录的列表加载态：挂载时拉一次，写操作后由调用点 `reload()` 重拉。
 *
 * 不轮询、不监听窗口聚焦：记忆由创作者与 Agent 各自写入，服务端无冲突检测，跟随外部更新
 * 会在创作者正在编辑时替换掉编辑器内容。
 *
 * 入参按 `level` 与项目名解构后再重建 scope：调用点惯常在 JSX 里传字面量对象，直接把它列进
 * 依赖会让每次渲染都重新拉取。
 *
 * 结果与产出它的 scope 成对存放，只在两者匹配时对外可见：切换项目后新目录的请求尚未返回的
 * 窗口里，旧目录的路径与文件列表会挂在新项目的标题下，点开还会把旧文件名发往新目录。
 */
export function useAgentMemory(scope: AgentMemoryScope): AgentMemoryState {
  const level = scope.level;
  const projectName = scope.level === "project" ? scope.projectName : null;
  const target = useMemo<AgentMemoryScope>(
    () => (level === "user" ? { level: "user" } : { level: "project", projectName: projectName ?? "" }),
    [level, projectName],
  );

  const [loaded, setLoaded] = useState<LoadedMemory | null>(null);
  const [loading, setLoading] = useState(true);
  const abortRef = useRef<AbortController | null>(null);

  const reload = useCallback(async () => {
    // The taker rotates the controller (see docs/standards/frontend.md). / 接管方轮换 controller（见 docs/standards/frontend.md）。
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    try {
      const next = await API.getAgentMemory(target, { signal: controller.signal });
      // Checkpoint before writing state after the network await: abort may happen after the response has already resolved.
      // 网络 await 之后的写 state 断点：abort 可能发生在响应已 resolve 之后。
      if (controller.signal.aborted) return;
      setLoaded({ target, data: next, error: null });
    } catch (err) {
      if (controller.signal.aborted) return;
      setLoaded({ target, data: null, error: errMsg(err) });
    } finally {
      // The superseded one yields: after cancellation don't reset shared state, or it would switch off the loading state the taker just lit.
      // 被接管方让位：作废后不复位共享状态，否则会灭掉接管方刚点亮的加载态。
      // react-doctor-disable-next-line react-doctor/no-loading-flag-reset-outside-finally -- the reset is already in finally; the condition only lets a cancelled round hand the reset to the taker / 复位已在 finally 中；条件只让被作废的一轮把复位交给接管方
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [target]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch once on mount; reload lights the loading state synchronously, a controlled initial load / 挂载时拉一次，reload 同步点亮加载态，属于受控的初始化加载
    void reload();
    return () => abortRef.current?.abort();
  }, [reload]);

  // A result belonging to the previous scope counts as not yet loaded: loading continues until the current scope's response lands.
  // 结果属于上一个 scope 时视同尚未加载：加载态一直持续到当前 scope 的响应落地。
  const current = loaded !== null && loaded.target === target ? loaded : null;
  return {
    overview: current?.data ?? null,
    loading: current === null || loading,
    error: current?.error ?? null,
    scope: target,
    reload,
  };
}
