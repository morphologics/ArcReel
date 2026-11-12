import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

/**
 * 加载占位：转圈加「加载中」，`role="status"` 供读屏播报。
 *
 * - `screen`：占满视口，用于登录态未确定、整页懒加载的 chunk 未到达。
 * - `pane`：占满父容器，用于工作区画布等局部区域的懒加载。
 */
/** `label`: shown instead of "Loading…" (e.g. while the backend is restarting). */
export function LoadingState({ variant, label }: { variant: "screen" | "pane"; label?: string }) {
  const { t } = useTranslation("common");
  return (
    <div
      role="status"
      aria-live="polite"
      className={
        variant === "screen"
          ? "flex h-dvh items-center justify-center gap-2 bg-background text-sm text-muted-foreground"
          : "flex h-full items-center justify-center gap-2 text-sm text-muted-foreground"
      }
    >
      <Loader2 aria-hidden className="size-4 animate-spin" />
      <span>{label ?? t("loading")}</span>
    </div>
  );
}
