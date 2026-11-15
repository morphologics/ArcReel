import { cn } from "cn";

import { Button } from "@/components/ui/button";

export interface InlineWarningAction {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  /** Hover reason when greyed out. / 灰化时的 hover 原因。 */
  title?: string;
}

export interface InlineWarningProps {
  /** Warning body. / 警告正文。 */
  message: string;
  /** One-click fix entry; omitted in cases that offer no entry, such as read-only contexts. / 一键修正入口；只读上下文等不给入口的场景省略。 */
  action?: InlineWarningAction;
  /** Outer spacing, decided by the layout at the usage site; typography and colors are fixed by this component. / 外层间距，由使用处的版式决定；排版与配色由本组件固定。 */
  className?: string;
}

/**
 * Inline warning: amber text + an optional one-click fix button.
 *
 * Carries hints like "this setting will make later operations fail at execution time, but doesn't block now": the warning doesn't gate controls,
 * it only explains the consequence and gives one fix path. `role="alert"` makes it read out immediately when the condition is met, so it sits on
 * the conditionally rendered outer element: the whole block appears with the condition, instead of rendering an empty shell first and filling it in.
 *
 * The fix button lives in this component rather than a slot: both usage sites' buttons must look and disable the same way, and a slot would let them drift apart.
 *
 * 行内警告：琥珀文案 + 可选的一键修正按钮。
 *
 * 承载「这项设置会让后续操作在执行期失败，但现在不阻断」这类提示——警告不门控控件，
 * 只说明后果并给出一条修正路径。`role="alert"` 让它在条件满足时立刻被朗读，因此挂在
 * 条件渲染的外层元素上：整块随条件出现，而非先渲染空壳再填内容。
 *
 * 修正按钮走本组件而非插槽：两处使用点的按钮外观与禁用行为必须一致，插槽会让它们各自漂移。
 */
export function InlineWarning({ message, action, className }: InlineWarningProps) {
  return (
    <div role="alert" className={cn("flex flex-wrap items-center gap-2 text-xs text-warn", className)}>
      <span>{message}</span>
      {action && (
        <Button variant="outline" size="xs" onClick={action.onClick} disabled={action.disabled} title={action.title}>
          {action.label}
        </Button>
      )}
    </div>
  );
}
