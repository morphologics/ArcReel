import { create } from "zustand";
import { API } from "@/api";
import { isDemoProject } from "@/onboarding/demo-project";
import { useProjectsStore } from "@/stores/projects-store";
import { errMsg } from "@/utils/async";
import type { CostEstimateResponse, SegmentCost, EpisodeCost } from "@/types";

interface CostState {
  costData: CostEstimateResponse | null;
  loading: boolean;
  error: string | null;

  /** Internal indexes — rebuilt on each fetchCost success */
  _segmentIndex: Map<string, SegmentCost>;
  _episodeIndex: Map<number, EpisodeCost>;

  fetchCost: (projectName: string) => Promise<void>;
  debouncedFetch: (projectName: string) => void;
  clear: () => void;

  getEpisodeCost: (episode: number) => EpisodeCost | undefined;
  getSegmentCost: (segmentId: string) => SegmentCost | undefined;
}

function buildIndexes(data: CostEstimateResponse): {
  _segmentIndex: Map<string, SegmentCost>;
  _episodeIndex: Map<number, EpisodeCost>;
} {
  const segmentIndex = new Map<string, SegmentCost>();
  const episodeIndex = new Map<number, EpisodeCost>();
  for (const ep of data.episodes) {
    episodeIndex.set(ep.episode, ep);
    for (const seg of ep.segments) {
      segmentIndex.set(seg.segment_id, seg);
    }
  }
  return { _segmentIndex: segmentIndex, _episodeIndex: episodeIndex };
}

let _debounceTimer: ReturnType<typeof setTimeout> | null = null;
let _abortController: AbortController | null = null;

/** Whether the user has switched to another project: the success and failure paths share one check, so they cannot drift apart. / 用户是否已切到别的项目——成功/失败两条路径共用同一份判断，避免各自维护出偏差。 */
function isStaleProject(projectName: string): boolean {
  return useProjectsStore.getState().currentProjectName !== projectName;
}

export const useCostStore = create<CostState>((set, get) => ({
  costData: null,
  loading: false,
  error: null,
  _segmentIndex: new Map(),
  _episodeIndex: new Map(),

  fetchCost: async (projectName: string) => {
    // A stale call must not abort the current project's in-flight request: debouncedFetch's timer does not re-check
    // staleness when it fires (only once at scheduling), and the public method may also be called directly; by the time it runs late the project
    // may have been switched away; aborting unconditionally then would wrongly kill an unrelated current-project request
    // that really needs to finish. Return directly and do not touch loading (it did not start it, so it should not end it).
    // 过期调用不得中止当前项目的在途请求：debouncedFetch 的定时器在触发时不会重新
    // 校验过期（只在排期时查过一次），public 方法也可能被直接调用，晚到执行时项目
    // 可能已经切走——此时若仍无条件 abort，会错误中止一个不相干、真正需要跑完的
    // 当前项目请求。直接返回，不动 loading（不是它开的，也不该由它收）。
    if (isStaleProject(projectName)) return;
    // Void the request still in flight from before switching in: whichever branch follows, this call supersedes it
    // 使切入前尚在途的请求作废：无论接下来走哪条分支，这次调用都取代它
    _abortController?.abort();
    // The guided demo project does not exist on the backend, so the cost estimate cannot be computed and the UI shows "not estimated"
    // 引导演示项目在后端不存在，费用估算无从计算，界面按「未估算」显示
    if (isDemoProject(projectName)) {
      _abortController = null;
      get().clear();
      return;
    }
    const controller = new AbortController();
    _abortController = controller;
    set({ loading: true, error: null });
    try {
      const data = await API.getCostEstimate(projectName, { signal: controller.signal });
      // A new request started during this one, so this response is void: finishing loading is left to the call that superseded it
      // 请求期间又发起了新请求，这份响应已作废：loading 收尾交给取代它的那次调用
      if (controller.signal.aborted) return;
      // The user has switched to another project: this data must not overwrite the current project's display (the singleton costData is not stored per project,
      // and segment_ids of two projects may collide), but loading must be finished: no request with the same key
      // will reset it instead, and leaving it would stay stuck in the loading state
      // 用户已切到别的项目：不能用这份数据覆盖当前项目的显示（单例 costData 不分项目
      // 存储，两个项目的 segment_id 还可能撞名），但 loading 要收尾——不会再有同 key
      // 请求替它复位，放着不收会一直卡在加载态
      if (isStaleProject(projectName)) {
        set({ loading: false });
        return;
      }
      set({ costData: data, loading: false, ...buildIndexes(data) });
    } catch (err) {
      if (controller.signal.aborted) return;
      if (isStaleProject(projectName)) {
        set({ loading: false });
        return;
      }
      set({ error: errMsg(err), loading: false });
    }
  },

  debouncedFetch: (projectName: string) => {
    // Clear demo projects immediately without waiting for the 500ms debounce window, otherwise during that window after switching into a demo project
    // the UI would still read cost data left in the store by the previous real project. This branch does not check staleness: the demo project
    // is itself the current project, so the "called after switching away" case does not exist.
    // 演示项目立即清空，不等 500ms 防抖窗口——否则切入演示项目后的这段窗口期，
    // UI 仍会读到上一个真实项目残留在 store 里的费用数据。这一支不判过期：演示项目
    // 本身就是当前项目，不存在「切走了还调它」的场景。
    if (isDemoProject(projectName)) {
      if (_debounceTimer) clearTimeout(_debounceTimer);
      _debounceTimer = null;
      _abortController?.abort();
      _abortController = null;
      get().clear();
      return;
    }
    // A call for what is no longer the current project does not touch the shared debounce timer: if debouncedFetch(A) lands after the user has switched to
    // B (e.g. in a slow request's .then callback), clearing/replacing the timer would also cancel the refresh B just scheduled
    // that really needs to run; the staleness check when the response lands can only drop A's own result and cannot rescue the B
    // it displaced.
    // This branch does not refetch after dropping; it relies on "after switching to a new project there is always a call that depends on
    // currentProjectName": AssetSidebar is mounted unconditionally in the workbench layout and its refresh effect depends on
    // currentProjectName, so the frame in which the project name lands always fires one more. The canvases' effects depend on
    // the route param projectName and fire before the router layer writes currentProjectName (React child
    // effects run before parent ones), so that call is bound to be dropped here. When adding new cost refresh entries keep this chain:
    // either depend on currentProjectName or make sure a later effect reruns.
    // 已不是当前项目的调用不动共享的防抖计时器：debouncedFetch(A) 若在用户已切到
    // B 之后才落地（如某个慢请求的 .then 回调），清掉/顶替计时器会连带取消 B 刚排的、
    // 真正需要跑的那次刷新——响应落地时的过期检查只能丢弃 A 自己的结果，救不回被它
    // 顶掉的 B。
    //
    // 这一支丢弃后不补拉，靠的是「切到新项目后必有一次以 currentProjectName 为
    // 依赖的调用」：AssetSidebar 无条件挂在工作台布局里，其刷新 effect 的依赖就是
    // currentProjectName，项目名落地的那一帧必然补发一次。各画布的 effect 依赖的是
    // 路由参数 projectName，会先于路由层写入 currentProjectName 触发（React 子组件
    // effect 先于父组件），那一次调用注定被这里丢掉。新增费用刷新入口时保持这条链：
    // 要么依赖 currentProjectName，要么确保有后续 effect 重跑。
    if (isStaleProject(projectName)) return;
    if (_debounceTimer) clearTimeout(_debounceTimer);
    _debounceTimer = setTimeout(() => {
      _debounceTimer = null;
      void get().fetchCost(projectName);
    }, 500);
  },

  clear: () => {
    if (_debounceTimer) clearTimeout(_debounceTimer);
    _debounceTimer = null;
    _abortController?.abort();
    _abortController = null;
    set({
      costData: null,
      loading: false,
      error: null,
      _segmentIndex: new Map(),
      _episodeIndex: new Map(),
    });
  },

  getEpisodeCost: (episode: number) => {
    return get()._episodeIndex.get(episode);
  },

  getSegmentCost: (segmentId: string) => {
    return get()._segmentIndex.get(segmentId);
  },
}));
