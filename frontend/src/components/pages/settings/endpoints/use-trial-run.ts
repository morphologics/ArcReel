import { useCallback, useEffect, useRef, useState } from "react";
import { API, ApiRequestError } from "@/api";
import { errMsg } from "@/utils/async";
import type {
  EndpointTestAssets,
  EndpointTestCredentials,
  EndpointTestParameters,
  TrialRunInfo,
  TrialRunModelRef,
} from "@/types";

const TRIAL_POLL_INTERVAL_MS = 2000;
const TRIAL_POLL_MAX_CONSECUTIVE_FAILURES = 5;

/** Payload for creating a test connection: either an inline definition or a model row reference. / 创建一次测试连接的载荷：内联定义或模型行引用二选一。 */
export interface TrialRunRequest {
  definition?: unknown;
  model_ref?: TrialRunModelRef;
  parameters: EndpointTestParameters;
  credentials?: EndpointTestCredentials;
}

export interface TrialRunHandle {
  run: TrialRunInfo | null;
  /** Terminal state: success or failure. / 终态：成功或失败。 */
  finished: boolean;
  starting: boolean;
  /** Error description from creating, reading back, or fetching the output. / 创建、回读或取产物时的出错说明。 */
  error: string | null;
  /** The previous run was abandoned by the user. / 上一次 run 是被用户放弃的。 */
  cancelled: boolean;
  /** Read-back failed consecutively up to the limit and polling stopped; the run still occupies a server slot, and the cancel entry still works. / 回读连续失败到上限，已停止轮询；run 仍占着服务端名额，取消入口照旧可用。 */
  pollStopped: boolean;
  /** Local object URL of the output; null when there is no output or it hasn't been fetched yet. / 产物的本地对象 URL；没有产物或还没取回为 null。 */
  artifactUrl: string | null;
  start: (request: TrialRunRequest, assets?: EndpointTestAssets) => Promise<void>;
  cancel: () => Promise<void>;
}

/**
 * Lifecycle of a test connection: create -> poll read-back -> fetch output -> cancel.
 *
 * The test connection cards of the two kinds each have their own parameters, materials and display, but this lifecycle is identical; the polling give-up counter,
 * in-place clearing on 404, and slot release after cancel were all pitfalls already hit, and copying a second version copies the pitfalls too.
 *
 * 一次测试连接的生命周期：创建 → 轮询回读 → 取回产物 → 取消。
 *
 * 两种 kind 的测试连接卡各有自己的参数、素材与展示，但这条生命周期完全一致——轮询的放弃计数、
 * 404 的就地清空、取消后的名额释放都是踩过的坑，抄第二份等于把坑也抄一遍。
 */
export function useTrialRun(): TrialRunHandle {
  const [starting, setStarting] = useState(false);
  const [run, setRun] = useState<TrialRunInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [artifact, setArtifact] = useState<{ runId: string; url: string } | null>(null);
  const [cancelled, setCancelled] = useState(false);
  // Polling give-up state: after consecutive failures reach the limit only read-back stops and the run is untouched; the server slot is still occupied,
  // so runId and the cancel entry must be kept, otherwise recreating hits trial_run_already_running.
  // 轮询放弃态：连续失败达上限后只停读回，不动 run——服务端名额仍被占用，
  // runId 与取消入口必须保留，否则重新创建会撞 trial_run_already_running。
  const [pollStopped, setPollStopped] = useState(false);
  // Incremented on each test start: during cancel and read-back the old run may have ended and the user started a new one, so late results must not overwrite the new run.
  // 每次开始测试加一：取消与回读期间旧 run 可能已终结、用户又开了新的一次，晚到的结果不能改写新 run。
  const generation = useRef(0);

  const runId = run?.id ?? null;
  const finished = run !== null && (run.status === "succeeded" || run.status === "failed");
  const artifactRunId = run?.has_artifact ? run.id : null;
  const artifactUrl = artifact?.runId === artifactRunId ? artifact.url : null;

  useEffect(() => {
    if (!artifactRunId) return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    void API.getTrialRunArtifact(artifactRunId, { signal: controller.signal })
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setArtifact({ runId: artifactRunId, url: objectUrl });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(errMsg(e));
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [artifactRunId]);

  // A trial run is an in-process async run: after creation it polls read-back and stops at a terminal state. Recursive setTimeout ensures the previous
  // read-back lands before the next is scheduled, so responses slower than the interval don't pile up concurrent requests.
  // 试跑是进程内异步 run：创建后轮询读回，终态即停。递归 setTimeout 保证上一次
  // 读回落地后才排下一次，响应慢于间隔时不会堆积并发请求。
  // react-doctor-disable-next-line react-doctor/effect-needs-cleanup -- each round's timer id is written to the same timer, and cleanup clears it with clearTimeout and aborts; callbacks after abort don't schedule another round / 每轮的定时器号写进同一个 timer，cleanup 统一 clearTimeout 并 abort；abort 后的回调不再排下一轮
  useEffect(() => {
    if (!runId || finished || pollStopped) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    // Stop once consecutive failures hit the limit: when the run is deleted or credentials expire, 401/404 won't heal by itself, and infinite retries only spam requests.
    // A single failure doesn't abandon polling; on a transient network blip the counter resets after the next round succeeds.
    // 连续失败达到上限即停：run 被删或凭证过期时 401/404 不会自愈，无限重试只会刷请求。
    // 单次失败不弃轮询，瞬时网络抖动在下一轮成功后计数归零。
    let consecutiveFailures = 0;
    const poll = () => {
      void API.getTrialRun(runId, { signal: controller.signal })
        .then((next) => {
          if (controller.signal.aborted) return;
          consecutiveFailures = 0;
          setError(null);
          setRun(next);
          timer = setTimeout(poll, TRIAL_POLL_INTERVAL_MS);
        })
        .catch((e) => {
          if (controller.signal.aborted) return;
          setError(errMsg(e));
          // An explicit 404 means the run is no longer on the server (TTL expired or lost on restart) and the slot is released,
          // so clear local state in place; only transient network/service errors go through retry and the give-up counter.
          // 明确的 404 表示 run 已不在服务端（TTL 过期或重启丢失），名额已释放，
          // 就地清空本地状态；只有瞬时网络/服务错误才走重试与放弃计数。
          if (e instanceof ApiRequestError && e.status === 404) {
            setRun(null);
            return;
          }
          consecutiveFailures += 1;
          if (consecutiveFailures < TRIAL_POLL_MAX_CONSECUTIVE_FAILURES) {
            timer = setTimeout(poll, TRIAL_POLL_INTERVAL_MS);
          } else {
            setPollStopped(true);
          }
        });
    };
    timer = setTimeout(poll, TRIAL_POLL_INTERVAL_MS);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [runId, finished, pollStopped]);

  const start = useCallback(async (request: TrialRunRequest, assets: EndpointTestAssets = {}) => {
    generation.current += 1;
    setError(null);
    setCancelled(false);
    setPollStopped(false);
    setStarting(true);
    try {
      setRun(await API.createTrialRun(request, assets));
    } catch (e) {
      setRun(null);
      setError(errMsg(e));
    } finally {
      setStarting(false);
    }
  }, []);

  // Cancel makes the server discard this run along with its result, and read-back only gets 404; clear local state in place
  // so "Start test" is usable again. The remote task is unaffected, and fees already incurred still count.
  // 取消会让服务端连同结果一起丢弃这次 run，回读只会拿到 404；就地清空本地状态，
  // 让「开始测试」重新可用。远端任务不受影响，已经发生的费用照算。
  const cancel = useCallback(async () => {
    if (!runId) return;
    const started = generation.current;
    const superseded = () => generation.current !== started;
    try {
      await API.cancelTrialRun(runId);
    } catch (e) {
      if (superseded()) return;
      if (!(e instanceof ApiRequestError && e.status === 404)) {
        setError(errMsg(e));
        return;
      }
      // 404 comes in two kinds: the provider already returned and the cancel came too late, with the server keeping the result; or the run expired and is gone.
      // Read back once first and show it if found (polling takes over as usual if not terminal); only if read-back also 404s treat it as cancelled and clear.
      // 404 有两种：供应商已经返回、取消来晚了，服务端保留着结果；或 run 已过期不在。
      // 先回读一次，读到就展示（未到终态时轮询照常接手），回读也 404 才当作已取消清空。
      try {
        const kept = await API.getTrialRun(runId);
        if (superseded()) return;
        setRun(kept);
        setPollStopped(false);
        setError(null);
        return;
      } catch (readError) {
        if (superseded()) return;
        if (!(readError instanceof ApiRequestError && readError.status === 404)) {
          setError(errMsg(readError));
          return;
        }
      }
    }
    if (superseded()) return;
    setRun(null);
    setError(null);
    setPollStopped(false);
    setCancelled(true);
  }, [runId]);

  return { run, finished, starting, error, cancelled, pollStopped, artifactUrl, start, cancel };
}
