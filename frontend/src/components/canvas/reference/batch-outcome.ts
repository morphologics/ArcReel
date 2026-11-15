import type { ReferenceBatchAdmission } from "@/types";

/**
 * The five outcomes of a batch enqueue. The server's `decision` only has three: an interrupted enqueue does not roll back already-created tasks,
 * so that path's decision is still `admitted`, differing from "the whole batch was created" only by whether some units were not queued. When the interruption hits
 * the first target, no task is created at all; that is the difference between `interrupted` and `none_queued`: the former
 * still has tasks running, the latter does not; they mean different things to the user and cannot be stated with the same sentence.
 *
 * 一次批量入队的五种结局。服务端的 `decision` 只分到三种：入队中断不撤销已建的任务，
 * 那一路的 decision 仍是 `admitted`，与「整批都建上了」只差有没有单元没排上。中断落在
 * 第一个目标上时一个任务也没建成，`interrupted` 与 `none_queued` 的差别就在这里：前者
 * 还有任务在跑，后者没有，两者对用户的意味不同，不能用同一句话陈述。
 */
export type ReferenceBatchOutcome =
  | "queued"
  | "confirm"
  | "blocked"
  | "interrupted"
  | "none_queued";

/**
 * The decision lives in this one place: the canvas uses it to decide whether to keep this verdict, the dialog uses it to decide open/close and form. Both ask the
 * two sides of the same question; writing it twice would silently diverge when the rule changes.
 *
 * 判定收在这一处：画布据此决定要不要留下这份结论，弹窗据此决定开合与形态。两处问的是
 * 同一件事的正反面，各写一遍就会在改判时静默失配。
 */
export function referenceBatchOutcome(admission: ReferenceBatchAdmission): ReferenceBatchOutcome {
  if (admission.decision === "blocked") return "blocked";
  if (admission.decision === "confirmation_required") return "confirm";
  if (admission.enqueue_failures.length === 0) return "queued";
  return admission.task_ids.length > 0 ? "interrupted" : "none_queued";
}
