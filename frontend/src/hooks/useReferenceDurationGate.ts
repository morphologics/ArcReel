import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API, ReferenceProjectionError, SpeechAdmissionError } from "@/api";
import { useAppStore } from "@/stores/app-store";
import { errMsg } from "@/utils/async";
import { advisoryProblems } from "@/components/canvas/reference/advisory-problems";
import type { DurationConfirmItem } from "@/components/canvas/reference/ReferenceDurationConfirmDialog";

interface Options {
  projectName: string;
  episode: number;
}

/**
 * Whether the unit should still be enqueued right now, provided by the caller per the current entry's semantics; it must be read live, not as a render-time snapshot:
 * both the precheck and the user's deliberation are await windows during which the same unit may be taken by another entry or the Agent.
 *
 * The judgment is per entry rather than unified in the gate — a batch entry targets "units without a final cut yet", so ones completed while the dialog
 * stays open must be dropped (once the task completes it's no longer busy, and queue dedup only looks at queued/running,
 * so letting it through means billing twice and overwriting the just-produced final cut); whereas a unit entry's "Regenerate" is meant to overwrite the existing final cut,
 * and the same judgment would block it too.
 *
 * 该 unit 此刻是否仍该入队，由调用方按当前入口的语义提供，须实时读取而非渲染期快照：
 * 预检与用户思考都是 await 窗口，其间同一 unit 可能已被其它入口或 Agent 占用。
 *
 * 判定按入口而非按闸门统一——批量入口的作用对象是「还没有成片的单元」，弹窗停留期间
 * 完成的要剔除（任务完成后它不再 busy，队列去重也只看 queued/running，
 * 放过去就是第二次计费并覆盖刚出的成片）；而单元入口的「重新生成」本就要覆盖已有成片，
 * 同一条判定会把它一并挡掉。
 */
type CanEnqueue = (unitId: string) => boolean;

interface PendingConfirm {
  /** Units needing the user's decision: duration option deviates or there are non-blocking issues / 需要用户拍板的单元：取档偏离或带非阻断问题 */
  items: DurationConfirmItem[];
  /** All units that passed confirmation (including those needing none), enqueued in original order after confirmation / 通过确认的全部单元（含无需确认的），确认后按原顺序入队 */
  unitIds: string[];
}

type ConfirmedDurations = ReadonlyMap<string, number>;
type Commit = (unitIds: string[], confirmedDurations: ConfirmedDurations) => Promise<void>;

/**
 * Pre-enqueue confirmation gate for reference-to-video entries: the precheck picks a duration option; when the requested seconds differ from the request duration basis, or the precheck reports
 * non-blocking issues (only the first N reference images used), ask the user first; cancelling enqueues nothing.
 *
 * Non-blocking issues trigger confirmation with the same weight as duration deviation: they also change the final cut content, and once the task is persisted the UI has no
 * surface showing them, so if not said before enqueueing they will never be said.
 *
 * Batch entries aggregate into one confirmation (a dialog per unit would make the user click N times for one action); single and batch entries share
 * the same gate — otherwise the batch button would become a bypass around confirmation.
 *
 * 参考生视频入口的入队前确认闸门：预检取档，申请秒数与请求时长基准不一致、或预检报出
 * 非阻断问题（参考图取前 N 张）时先让用户确认，取消则一个都不入队。
 *
 * 非阻断问题与取档偏离同权触发确认：它同样改变成片内容，而任务落库后界面上没有任何
 * 展示面，入队前不说就永远不会被说出来。
 *
 * 批量入口聚合成一次确认（逐个弹窗会让用户为一次操作点 N 遍），单入口与批量入口共用
 * 同一条闸门——否则批量按钮会成为绕过确认的旁路。
 */
export function useReferenceDurationGate({ projectName, episode }: Options) {
  const { t } = useTranslation("dashboard");
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  // The enqueue callback is captured with run: confirmation happens at any time after run, so it can't be re-read from the render-time closure
  // 入队回调随 run 一起捕获：确认发生在 run 之后的任意时刻，不能从渲染期闭包重取
  const commitRef = useRef<Commit | null>(null);
  // The re-check judgment is likewise captured with run: it depends on entry semantics, and at confirmation time it must be the one from the entry that started this round
  // 复核判定同理随 run 捕获：它按入口语义而定，确认时刻要用的是发起这一轮的那个入口的
  const canEnqueueRef = useRef<CanEnqueue | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // A project/episode switch cancels the in-flight precheck and pending dialog: both are bound to the pre-switch data; confirming after the switch
  // would enqueue the previous episode's units.
  // 项目/剧集切换即作废在途预检与未决弹窗：二者都绑定切换前那一份数据，留到切换后
  // 确认就会把上一个剧集的单元入队。
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
      setPending(null);
      commitRef.current = null;
      canEnqueueRef.current = null;
    };
  }, [projectName, episode]);

  const run = useCallback(
    async (
      unitIds: string[],
      commit: Commit,
      canEnqueue: CanEnqueue,
    ) => {
      if (unitIds.length === 0) return;
      // The taker rotates the controller: a new precheck round cancels its predecessor, and the pending confirmation dialog is replaced by the new round.
      // The dialog and commit must be cleared right now, not left for the new round's result to overwrite — if the new round needs no
      // confirmation at all (commit returns directly), the old dialog would stay on screen, and clicking confirm would enqueue the previous round's list.
      // 接管方轮换 controller：新一轮预检作废前任，未决的确认弹窗随之被新一轮替换。
      // 弹窗与 commit 必须在此刻一并清掉，不能等新一轮的结果来覆盖——新一轮若全部无需
      // 确认（直接 commit 返回），旧弹窗会留在屏幕上，用户点确认就按上一轮的清单入队。
      abortRef.current?.abort();
      setPending(null);
      commitRef.current = null;
      canEnqueueRef.current = null;
      const controller = new AbortController();
      abortRef.current = controller;
      const { signal } = controller;

      const results = await Promise.all(
        unitIds.map(async (unitId) => {
          try {
            const precheck = await API.precheckReferenceVideoDuration(
              projectName,
              episode,
              unitId,
              { signal },
            );
            return { unitId, precheck };
          } catch (e) {
            if (signal.aborted) return null;
            return { unitId, error: e };
          }
        }),
      );
      if (signal.aborted) return;

      const ok: DurationConfirmItem[] = [];
      let failed = 0;
      const admissionMessages = new Set<string>();
      for (const result of results) {
        if (!result) continue;
        if ("error" in result) {
          if (result.error instanceof SpeechAdmissionError || result.error instanceof ReferenceProjectionError) {
            admissionMessages.add(result.error.message);
          } else {
            failed += 1;
          }
          continue;
        }
        ok.push(result);
      }
      // Units whose precheck failed aren't enqueued: there's no way to tell whether the final cut duration matches the request basis, and silently generating by option would burn quota
      // 预检失败的单元不入队：无从判断成片时长是否与请求基准一致，静默按档位生成会烧掉配额
      if (admissionMessages.size > 0) {
        useAppStore.getState().pushToast(Array.from(admissionMessages).join("\n"), "error");
      }
      if (failed > 0) {
        useAppStore
          .getState()
          .pushToast(t("reference_duration_precheck_failed", { count: failed }), "error");
      }
      if (ok.length === 0) return;

      // Re-check when the dialog opens: during the precheck await window the same unit may have been enqueued elsewhere or already generated;
      // listing it for confirmation would ask the user to decide on something that can't (or shouldn't) be done again.
      // Skip silently rather than erroring — same rule as the batch entry's "queue everything that can still be generated".
      // 弹窗打开时刻复核：预检这段 await 窗口里同一 unit 可能已被别处入队或已生成完成，
      // 把它列进确认清单等于请用户为一件做不成（或不该再做）的事拍板。
      // 静默跳过而非报错——与批量入口「把还能生成的都排上」同口径。
      const available = ok.filter((item) => canEnqueue(item.unitId));
      if (available.length === 0) return;

      const needsConfirmation = available.filter(
        (item) => item.precheck.needs_confirmation || advisoryProblems(item.precheck).length > 0,
      );
      const passing = available.map((item) => item.unitId);
      if (needsConfirmation.length === 0) {
        await commit(passing, new Map());
        return;
      }
      commitRef.current = commit;
      canEnqueueRef.current = canEnqueue;
      setPending({ items: needsConfirmation, unitIds: passing });
    },
    [projectName, episode, t],
  );

  const confirm = useCallback(() => {
    const current = pending;
    const commit = commitRef.current;
    const canEnqueue = canEnqueueRef.current;
    setPending(null);
    commitRef.current = null;
    canEnqueueRef.current = null;
    if (!current || !commit) return;
    // Re-check once more at submit time: while the dialog stays open (the user's deliberation, possibly long) units in the list may have been
    // enqueued elsewhere, and in batch cases may already be generated — submitting the frozen list as-is would bill twice.
    // 提交时刻再复核一次：弹窗停留期间（用户思考时长，可以很长）清单里的单元可能已被
    // 别处入队，批量场景下还可能已经生成完成——按冻结清单原样提交会重复计费。
    const targets = canEnqueue ? current.unitIds.filter(canEnqueue) : current.unitIds;
    if (targets.length === 0) return;
    // commit itself already reports failures per entry; this only catches unexpected exceptions that slip through
    // Declare the confirmed duration option only for units whose option deviates: units listed only for non-blocking issues have no option to decide,
    // and declaring one for them would let the server skip its own duration-option check.
    // commit 自身已按入口口径提示失败，这里只兜住漏出的意外异常
    // 只为取档偏离的单元声明已确认档位：仅因非阻断问题进清单的单元没有档位要拍板，
    // 替它声明一个等于让服务端跳过自己那道取档校验。
    const confirmedDurations = new Map(
      current.items
        .filter((item) => item.precheck.needs_confirmation)
        .map((item) => [item.unitId, item.precheck.request_duration]),
    );
    void commit(targets, confirmedDurations).catch((e: unknown) => {
      useAppStore
        .getState()
        .pushToast(t("reference_generate_request_failed", { error: errMsg(e) }), "error");
    });
  }, [pending, t]);

  const cancel = useCallback(() => {
    setPending(null);
    commitRef.current = null;
    canEnqueueRef.current = null;
  }, []);

  return {
    run,
    /** Spread directly onto ReferenceDurationConfirmDialog / 直接摊给 ReferenceDurationConfirmDialog */
    dialogProps: {
      open: pending !== null,
      items: pending?.items ?? [],
      onConfirm: confirm,
      onCancel: cancel,
    },
  };
}
