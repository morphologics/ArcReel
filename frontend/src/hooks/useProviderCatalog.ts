import { useCallback, useEffect, useRef, useState } from "react";
import { API } from "@/api";
import { useConfigStatusStore } from "@/stores/config-status-store";
import type { CustomProviderInfo, ProviderInfo } from "@/types";
import { errMsg, voidCall } from "@/utils/async";

/**
 * Outcome of one silent re-fetch. Callers must tell "re-fetch failed" from "cancelled by a later request": the former leaves the catalog at old content
 * and the user must be told; the latter is the normal concurrent path where the taker will write the updated catalog, and shouldn't report an error.
 *
 * 一次静默重取的结局。调用方要区分「重取失败」与「被后续请求作废」：前者目录停在旧内容、
 * 需要告诉用户，后者是接管方会写下更新目录的正常并发路径，不该报错。
 */
export type CatalogRefreshResult =
  | { status: "ok"; customProviders: CustomProviderInfo[] }
  | { status: "aborted" }
  | { status: "failed" };

export interface ProviderCatalog {
  providers: ProviderInfo[];
  customProviders: CustomProviderInfo[];
  loading: boolean;
  error: string | null;
  /** Full re-fetch, returning to the loading / error panel behavior — the error panel's retry entry. / 全量重取并回到 loading / 错误面板口径——错误面板的重试入口。 */
  reload: () => void;
  /**
   * Silently re-fetch the whole catalog; on success it returns the latest custom provider list so the caller can adjust its selection.
   * Failure and "cancelled by a later request" resolve as failed / aborted respectively.
   *
   * 静默重取整份目录；成功时带出最新的自定义供应商列表，供调用方按它改选中项。
   * 失败与「被后续请求作废」分别 resolve 成 failed / aborted。
   */
  refresh: () => Promise<CatalogRefreshResult>;
}

/**
 * Reading and refreshing the provider catalog. The preset and custom tables are always fetched together, so the four write points (first fetch, language re-fetch, post-save
 * re-fetch, failure retry) have a single implementation and a single cancellation domain.
 *
 * The cancellation domain has one controller; any read that takes over first cancels the one in flight: the post-save re-fetch and the language-switch
 * re-fetch would otherwise overwrite each other, and the slower one would write an outdated catalog when it returns. The cancelled one doesn't write back shared state; the taker finishes up.
 *
 * A ``language`` change triggers a re-fetch: provider and model names are written by the backend per Accept-Language; without re-fetching the catalog stays in
 * the pre-switch language. This kind of re-fetch is silent — no loading, list and detail panels stay mounted, so unsaved input in the detail form
 * isn't unmounted and lost; failure also keeps the previous catalog, names stuck in the old language being far better than losing input. But when the first fetch has never succeeded
 * there is no catalog to keep, so that round still uses the normal loading / error panel, otherwise an empty list with no retry entry would be left.
 *
 * 供应商目录的读取与刷新。预置与自定义两张表恒一同取，四个写入点（首次拉取、语言重取、保存后
 * 重取、失败重试）因此只有一处实现，也只有一个取消域。
 *
 * 取消域只有一个 controller，任何一次读取接管时先作废在途的那次：保存后的重取与语言切换触发的
 * 重取会互相覆盖，慢的那次回来时写下的是过期目录。作废方不回写共享状态，收尾由接管方负责。
 *
 * ``language`` 变化即重取：供应商与模型名由后端按 Accept-Language 成文，不重取则目录停留在
 * 切换前的语言。这一类重取是静默的——不置 loading，列表与详情面板保持挂载，详情表单里未保存的
 * 输入不被卸载丢弃；失败也保留上一份目录，译名停留在旧语言远好过丢输入。但首次拉取尚未成功过
 * 时没有可留的目录，那一轮仍走常规的 loading / 错误面板，否则会留下一个空列表且没有重试入口。
 */
export function useProviderCatalog(language: string): ProviderCatalog {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [customProviders, setCustomProviders] = useState<CustomProviderInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  const hasLoadedRef = useRef(false);

  const load = useCallback(async (silent: boolean): Promise<CatalogRefreshResult> => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    try {
      const [presetRes, customRes] = await Promise.all([
        API.getProviders({ signal: controller.signal }),
        API.listCustomProviders({ signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return { status: "aborted" };
      setProviders(presetRes.providers);
      setCustomProviders(customRes.providers);
      hasLoadedRef.current = true;
      setError(null);
      void useConfigStatusStore.getState().refresh();
      return { status: "ok", customProviders: customRes.providers };
    } catch (err) {
      if (controller.signal.aborted) return { status: "aborted" };
      if (!silent) setError(errMsg(err));
      return { status: "failed" };
    } finally {
      // react-doctor-disable-next-line react-doctor/no-loading-flag-reset-outside-finally -- the reset is already in finally; a cancelled round hands the reset to the taker, otherwise it would switch off the taker's loading state / 复位已在 finally 中；被作废的一轮把复位交给接管方，否则会灭掉接管方的加载态
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  const refresh = useCallback(() => load(true), [load]);
  const reload = useCallback(() => setReloadKey((k) => k + 1), []);

  const loadedKeyRef = useRef<number | null>(null);
  useEffect(() => {
    // A rerun under the same reloadKey can only be triggered by a language change, so refresh silently.
    // 同一 reloadKey 下的重跑只可能由语言变化触发，此时静默刷新。
    const silent = hasLoadedRef.current && loadedKeyRef.current === reloadKey;
    loadedKeyRef.current = reloadKey;
    voidCall(load(silent));
    return () => abortRef.current?.abort();
  }, [reloadKey, language, load]);

  return { providers, customProviders, loading, error, reload, refresh };
}
