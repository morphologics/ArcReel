import { useId, type ReactNode } from "react";

interface FieldRowProps {
  label: ReactNode;
  /** A single control: the label links to it via `<label htmlFor>`. / 单个控件：标签用 `<label htmlFor>` 关联它。 */
  htmlFor?: string;
  /** Multiple controls in one row (e.g. price): the row is a group named by the label, each control with its own `aria-label`. / 一行里有多个控件（如价格）：整行作为以标签命名的 group，控件各自带 `aria-label`。 */
  group?: boolean;
  hint?: ReactNode;
  children: ReactNode;
}

/** One property row of the custom provider form: a shared-width label column on the left, control and note on the right. / 自定义供应商表单的一行属性：左侧是共用宽度的标签列，右侧是控件与说明。 */
export function FieldRow({ label, htmlFor, group, hint, children }: FieldRowProps) {
  const labelId = useId();
  return (
    <div className="grid grid-cols-[7rem_minmax(0,1fr)] items-start gap-x-4">
      {htmlFor ? (
        <label htmlFor={htmlFor} className="pt-1.5 text-sm text-muted-foreground">
          {label}
        </label>
      ) : (
        <span id={labelId} className="pt-1.5 text-sm text-muted-foreground">
          {label}
        </span>
      )}
      <div
        role={group ? "group" : undefined}
        aria-labelledby={group ? labelId : undefined}
        className="flex min-w-0 flex-col gap-1.5"
      >
        {children}
        {hint}
      </div>
    </div>
  );
}

/** A note line under the control; `tone="warn"` for hints requiring user correction, such as format errors. / 控件下方的一行说明；`tone="warn"` 用于格式错误等需要用户修改的提示。 */
export function FieldHint({ children, tone }: { children: ReactNode; tone?: "warn" }) {
  return <p className={tone === "warn" ? "text-xs text-warn" : "text-xs text-muted-foreground"}>{children}</p>;
}
