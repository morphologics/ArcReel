import type { TFunction } from "i18next";
import type {
  AdmissionProblem,
  BatchAdmissionUnit,
  WorkflowActionType,
  WorkflowBlocker,
  WorkflowProblem,
} from "@/types/workflow";

/**
 * Unified presentation shape of every "where something went wrong" in the panel. Three kinds of source (per-unit gaps of the whole-batch admission verdict,
 * structured problems in the plan, data corruption blockers) are normalized here, and the UI only knows this one kind of row.
 *
 * The four slots each have their own job and don't substitute for each other: `unitId` / `field` say **where**, `summary` says
 * **what reason**, `nextStep` says **what to do next**, and `detail` is the raw text kept for troubleshooting.
 *
 * 面板里所有「哪里出了问题」的统一呈现形状。三类来源（整批准入判定的逐单元缺口、
 * 计划里的结构化问题、数据损坏 blocker）归一到这里，界面只认这一种行。
 *
 * 四个位置各司其职，不互相顶替：`unitId` / `field` 说的是**在哪**，`summary` 说的是
 * **什么原因**，`nextStep` 说的是**接下来做什么**，`detail` 是留给排障的原文。
 */
export interface ProblemView {
  key: string;
  /** The unit with the problem; empty for project-level problems. / 出问题的单元；项目级问题为空。 */
  unitId?: string | null;
  /** Field path of the problem, e.g. `generation_settings.audio_backend`. / 出问题的字段路径，如 `generation_settings.audio_backend`。 */
  field?: string | null;
  /** Localized one-sentence reason. / 已本地化的一句话原因。 */
  summary: string;
  /** Extra measures, such as a duration option comparison. / 附加度量，如档位对比。 */
  meta?: string | null;
  /** Localized statement of the next action; empty when there is no matching text, not made up. / 已本地化的下一步动作陈述；没有对应文案时为空，不编造。 */
  nextStep?: string | null;
  /** Server raw text, shown collapsed and not in the summary; hints in the plan have no raw text. / 服务端原文，折叠展示，不进摘要；计划里的提示没有原文。 */
  detail?: string | null;
}

type Translate = TFunction<"workflow">;

/**
 * One-sentence reason localized by problem code, with parameters taken from `params`. Unknown problem codes get a generic reason based on the action they hand back,
 * and when the action is also unknown, the most generic sentence; the summary never shows server raw text, problem codes or reason codes; raw text goes into collapsed details.
 *
 * 按问题码本地化的一句话原因，参数取自 `params`。不认识的问题码按它交回的动作给通用原因，
 * 动作也不认识时给最泛的一句——摘要不出现服务端原文、问题码或理由码，原文进折叠详情。
 */
function localizedSummary(
  t: Translate,
  code: string,
  action: string | null | undefined,
  params: Record<string, unknown> | undefined,
): string {
  const translated = t(`problem_${code}`, { ...params, defaultValue: "" });
  if (translated) return translated;
  return t(`problem_fallback_${action ?? "none"}`, { defaultValue: t("problem_fallback_unknown") });
}

/**
 * Restates the next action given by the backend. Action translations are bare imperative phrases, and the "Next step:" frame is added uniformly here,
 * so call sites don't each write it. Action types are the closed set {@link WorkflowActionType}, and each value has
 * text (coverage check in `action-language.test.ts`); `action_unknown` is only a runtime safety net, catching
 * new actions the backend ships before the frontend; not being able to say which action is still better than swallowing the whole step.
 *
 * 复述后端给的下一步动作。动作译文是裸的祈使短语，「下一步：」这层框架在这里统一加，
 * 各调用点不各写一遍。动作类型是 {@link WorkflowActionType} 那个闭集，每个取值都配了
 * 文案（覆盖检查见 `action-language.test.ts`）；`action_unknown` 只作运行时防线，接住
 * 后端先于前端上线的新动作——说不出是哪个动作，也好过把这一步整个吞掉。
 */
export function nextStepForAction(t: Translate, action: WorkflowActionType): string {
  return t("next_step", {
    step: t(`action_${action}`, { defaultValue: t("action_unknown") }),
  });
}

/** The next step in a problem row. Left empty when there is no action, or the action has no matching translation; not made up. / 问题行里的下一步。没有动作、或动作没有对应译文时留空，不编造。 */
function nextStepFor(t: Translate, action: string | null | undefined): string | null {
  if (!action || action === "none") return null;
  const phrase = t(`action_${action}`, { defaultValue: "" });
  return phrase ? t("next_step", { step: phrase }) : null;
}

function stringParam(params: Record<string, unknown> | undefined, key: string): string | null {
  const value = params?.[key];
  return typeof value === "string" && value ? value : null;
}

function problemField(problem: AdmissionProblem): string | null {
  const field = stringParam(problem.params, "field") ?? stringParam(problem.params, "path");
  if (field) return field;
  const path = problem.params?.["path"];
  if (Array.isArray(path)) {
    const parts = path.filter((part): part is string => typeof part === "string");
    if (parts.length > 0) return parts.join(".");
  }
  return null;
}

/** Hints in the plan: unit from `unit_id`, reason localized by problem code, no server raw text. / 计划里的提示：单元来自 `unit_id`，原因按问题码本地化，没有服务端原文。 */
export function problemViews(t: Translate, problems: WorkflowProblem[], keyPrefix = "problem"): ProblemView[] {
  return problems.map((problem, index) => ({
    key: `${keyPrefix}-${problem.code}-${index}`,
    unitId: problem.unit_id ?? null,
    summary: localizedSummary(t, problem.code, problem.action, problem.params),
    nextStep: nextStepFor(t, problem.action),
  }));
}

/**
 * Data corruption blocker. `path` is the concrete field the user needs to fix and goes into the summary row; `reason` is
 * server raw text and goes into the collapsed area: first a readable sentence, troubleshooting details only after expanding.
 *
 * 数据损坏的 blocker。`path` 是用户要去修的具体字段，进摘要行；`reason` 是
 * 服务端原文，进折叠区——先给能读懂的一句，排障细节在展开后才出现。
 */
export function blockerViews(t: Translate, blockers: WorkflowBlocker[]): ProblemView[] {
  return blockers.map((blocker, index) => ({
    key: `blocker-${blocker.code}-${index}`,
    field: blocker.path,
    summary: t(`problem_${blocker.code}`, { defaultValue: t("blocker_generic") }),
    nextStep: nextStepFor(t, "repair_project_data"),
    detail: blocker.reason,
  }));
}

/** Marker in the whole-batch admission verdict for "fine itself, left unsubmitted along with this batch". / 整批准入判定里「自身没问题、随本批一起未提交」的标记。 */
const WITHHELD_CODE = "generation_batch_admission_withheld";

export function isWithheld(unit: BatchAdmissionUnit): boolean {
  return unit.withheld === true || unit.problems.some((problem) => problem.code === WITHHELD_CODE);
}

/**
 * Per-unit admission gap. The requested duration option is shown on the same line as the reason: just saying "duration over the limit" doesn't show by how much,
 * and the user mainly relies on this number to judge what to change.
 *
 * 逐单元的准入缺口。申请档位与原因同行呈现：光说「时长超上限」看不出差多少，
 * 用户判断该去改什么主要靠这个数字。
 */
export function admissionUnitViews(
  t: Translate,
  units: BatchAdmissionUnit[],
  formatSeconds: (value: number) => string,
): ProblemView[] {
  const views: ProblemView[] = [];
  for (const unit of units) {
    const meta =
      unit.request_duration_seconds != null
        ? t("unit_request_tier", { request: formatSeconds(unit.request_duration_seconds) })
        : null;
    unit.problems.forEach((problem, index) => {
      views.push({
        key: `${unit.unit_id}-${problem.code}-${index}`,
        unitId: unit.unit_id,
        field: problemField(problem),
        // The batch endpoint already localized the text into message, and the raw text is kept as collapsed technical detail; the plan endpoint has no message,
        // so look up the translation table by problem code, and don't show raw text either.
        // 批量端点已经把文案本地化进 message，原文留作折叠的技术细节；计划端点没有 message，
        // 按问题码查译文表，也不展示原文。
        summary: problem.message ?? localizedSummary(t, problem.code, problem.action, problem.params),
        meta: index === 0 ? meta : null,
        nextStep: nextStepFor(t, problem.action),
        detail: problem.message ? (problem.detail ?? null) : null,
      });
    });
  }
  return views;
}

/**
 * Targets not reached when enqueueing was interrupted. Admission already passed and the gap isn't in the unit itself, so no duration option comparison;
 * what the user needs to know is which ones weren't queued and why; the duration option is just noise here.
 *
 * Parameters take a structure rather than a concrete type: this layer is where generic problem rows are normalized, and it doesn't depend back on one route's receipt type.
 * The server already localized the text into `message`, the same shape as admission gaps; `detail` is optional, and when absent
 * the problem row has no collapsed details.
 *
 * 入队中断时没轮到的目标。准入已经通过，缺口不在单元自身，所以不带档位对比——
 * 用户要知道的是哪几个没排上、各自为什么，档位在这里只是噪声。
 *
 * 参数取结构而非具体类型：这层是通用问题行的归一处，不反向依赖某条路线的回执类型。
 * 服务端已把文案本地化进 `message`，与准入缺口同一形状；`detail` 是可选字段，缺省时
 * 问题行不带折叠详情。
 */
export function enqueueFailureViews(
  t: Translate,
  failures: readonly { unit_id: string; problem: AdmissionProblem }[],
): ProblemView[] {
  return failures.map((failure, index) => ({
    key: `enqueue-${failure.unit_id}-${index}`,
    unitId: failure.unit_id,
    field: problemField(failure.problem),
    summary:
      failure.problem.message ??
      localizedSummary(t, failure.problem.code, failure.problem.action, failure.problem.params),
    nextStep: nextStepFor(t, failure.problem.action),
    detail: failure.problem.detail ?? null,
  }));
}
