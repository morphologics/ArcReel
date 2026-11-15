import type { ReactNode } from "react";

/** One row of a settings list: label and description on the left, the control on the right (below it when narrow). */
export function SettingRow({
  label,
  description,
  children,
  labelledBy,
}: {
  label: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  /** Id of the label element: the row becomes a group named by it, so its controls are announced with their label. */
  labelledBy?: string;
}) {
  return (
    <div
      role={labelledBy ? "group" : undefined}
      aria-labelledby={labelledBy}
      className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2 px-4 py-3"
    >
      <div className="flex min-w-0 flex-1 basis-56 flex-col gap-0.5">
        {label}
        {description && <div className="text-sm text-muted-foreground">{description}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}
