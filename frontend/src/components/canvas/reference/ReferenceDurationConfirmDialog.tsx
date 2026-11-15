import { useTranslation } from "react-i18next";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogBody,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import type { ReferenceDurationPrecheck } from "@/types";
import { EpisodeItemRef } from "@/components/shared/episode-item/EpisodeItemRef";
import { advisoryProblems } from "./advisory-problems";

/** Units needing confirmation and their option resolution results. / 需确认的单元及其取档结果。 */
export interface DurationConfirmItem {
  unitId: string;
  precheck: ReferenceDurationPrecheck;
}

interface Props {
  open: boolean;
  items: DurationConfirmItem[];
  onConfirm: () => void;
  onCancel: () => void;
}

/** One "script option -> requested option" comparison row; seconds are monospaced so multiple rows align vertically. / 一行「剧本档位 → 申请档位」对照，秒数等宽以便多行纵向对齐。 */
function DurationRow({
  showUnit,
  item,
  diffText,
}: {
  /** Identify the unit on each row in batch confirmation; not repeated for a single confirmation. / 批量确认时逐行指认单元；单个确认不重复。 */
  showUnit: boolean;
  item: DurationConfirmItem;
  diffText: string;
}) {
  const { t } = useTranslation("dashboard");
  const seconds = (value: number) => t("reference_duration_seconds", { value });
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
      {showUnit && <EpisodeItemRef itemId={item.unitId} />}
      <span className="tabular-nums text-subtle-foreground">{seconds(item.precheck.script_duration)}</span>
      <span aria-hidden className="text-muted-foreground">
        →
      </span>
      <span className="font-medium tabular-nums text-foreground">{seconds(item.precheck.request_duration)}</span>
      <span className="text-muted-foreground">{diffText}</span>
    </li>
  );
}

/**
 * Confirmation before a video is enqueued: the model only accepts discrete duration options; once the requested option deviates from the script option, explain the
 * options before enqueueing and let the user decide whether to continue; the precheck's
 * non-blocking issues (only the first N reference images are used) trigger this dialog just like an option deviation, listed below the duration rows: they change
 * the final cut's content without blocking the enqueue, and nothing in the UI shows them afterwards.
 *
 * When the whole dialog covers only one unit, state the two second values and the consequence directly; when it covers several, list them row by row with unit labels, each row
 * carrying its own longer/shorter delta, so mixed directions are visible at once. The list scrolls when it overflows instead of collapsing into a count: collapsed rows
 * may include a unit whose final cut gets shorter, and the user would be deciding for it without seeing it.
 *
 * 视频入队前的确认：模型只接受离散时长档位，申请档位一旦偏离剧本档位，就在入队前讲清楚
 * 档位，由用户决定是否继续；预检的
 * 非阻断问题（参考图取前 N 张）与取档偏离同等触发本弹窗，列在时长对照行之下——它们改变
 * 成片内容却不拦截入队，事后界面上没有任何展示面。
 *
 * 整个弹窗只覆盖一个单元时直接陈述两个秒数与后果；覆盖多个时逐行列出并带上单元标签，每行
 * 自带更长/更短的差值，混合方向也能一次看清。列表超出高度即滚动而不折叠计数：被折叠的行里
 * 可能有成片更短的单元，而用户是在看不到它的情况下为它拍板。
 */
export function ReferenceDurationConfirmDialog({ open, items, onConfirm, onCancel }: Props) {
  const { t } = useTranslation("dashboard");
  const durationItems = items.filter((item) => item.precheck.needs_confirmation);
  const advisoryItems = items
    .map((item) => ({ unitId: item.unitId, problems: advisoryProblems(item.precheck) }))
    .filter((entry) => entry.problems.length > 0);
  if (durationItems.length === 0 && advisoryItems.length === 0) return null;

  const seconds = (value: number) => t("reference_duration_seconds", { value });
  const baseline = (item: DurationConfirmItem) => item.precheck.script_duration;
  const diffText = (item: DurationConfirmItem) => {
    const { request_duration } = item.precheck;
    const previous = baseline(item);
    const diff = Math.abs(request_duration - previous);
    return request_duration < previous
      ? t("reference_duration_diff_shorter", { value: seconds(diff) })
      : t("reference_duration_diff_longer", { value: seconds(diff) });
  };

  // Whether unit labels appear is decided by the number of units the whole dialog covers, not each section's own row count: if duration rows had no labels while
  // notice rows did, the reader could not tell whether both refer to the same unit.
  // 单元标签的有无按整个弹窗覆盖的单元数决定，不按分区各自的条数：时长行不带标签而
  // 告知行带标签，读者无从判断两处说的是不是同一个单元。
  const single = items.length === 1 && durationItems.length === 1 ? durationItems[0] : null;

  const durationSection = single ? (
    <div className="space-y-2">
      <ul className="space-y-1">
        <DurationRow showUnit={false} item={single} diffText={diffText(single)} />
      </ul>
      <p>
        {single.precheck.request_duration < baseline(single)
          ? t("reference_duration_note_shorter", {
              duration: seconds(single.precheck.request_duration),
            })
          : t("reference_duration_note_longer", {
              duration: seconds(single.precheck.request_duration),
            })}
      </p>
    </div>
  ) : durationItems.length > 0 ? (
    <div className="space-y-2">
      <p>{t("reference_duration_batch_summary", { count: durationItems.length })}</p>
      <ul className="space-y-1">
        {durationItems.map((item) => (
          <DurationRow key={item.unitId} showUnit item={item} diffText={diffText(item)} />
        ))}
      </ul>
      <p>{t("reference_duration_note_no_trim")}</p>
    </div>
  ) : null;

  const advisorySection =
    advisoryItems.length > 0 ? (
      <div className="space-y-2">
        <p>{t("reference_advisory_heading")}</p>
        <ul className="space-y-1">
          {advisoryItems.flatMap(({ unitId, problems }) =>
            problems.map((problem, index) => (
              // react-doctor-disable-next-line react-doctor/no-array-index-as-key -- Hints within a unit are computed by the server and have no ID; read-only and unchanged while the dialog is open, rows have no state / 单元内的提示由服务端算出、没有 ID，确认框打开期间只读不变、行内无状态
              <li
                key={`${unitId}-${index}`}
                className="flex flex-wrap items-center gap-x-2 gap-y-0.5"
              >
                {items.length > 1 && <EpisodeItemRef itemId={unitId} />}
                <span className="text-subtle-foreground">{problem.message}</span>
              </li>
            )),
          )}
        </ul>
      </div>
    ) : null;

  // Without a notice section, don't wrap in a spacing container: a single section has its own internal spacing, an extra wrapper only adds blank space
  // 没有告知分区时不套间距容器：单一分区自带内部间距，多包一层只会多出一处空白
  const description =
    advisorySection === null ? (
      durationSection
    ) : (
      <div className="space-y-3">
        {durationSection}
        {advisorySection}
      </div>
    );
  const durationChanges = durationItems.length > 0;

  const title = t(
    durationChanges ? "reference_duration_confirm_title" : "reference_advisory_confirm_title",
  );
  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onCancel();
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
        </AlertDialogHeader>
        {/* Multi-unit comparison rows may be long; the body is keyboard-scrollable / 多单元的对照行可能很长，正文可以用键盘滚动 */}
        <AlertDialogBody tabIndex={0} role="region" aria-label={title}>
          <AlertDialogDescription render={<div />}>{description}</AlertDialogDescription>
        </AlertDialogBody>
        <AlertDialogFooter>
          <AlertDialogCancel>{t("common:cancel")}</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>
            {t(durationChanges ? "reference_duration_confirm_cta" : "reference_advisory_confirm_cta")}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
