import type { ComponentProps, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, Loader2, Sparkles } from "lucide-react";
import { cn } from "cn";
import { Button } from "@/components/ui/button";
import { ButtonGroup, ButtonGroupSeparator } from "@/components/ui/button-group";
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { GenerateLabel, type GenerateDetail } from "./GenerateLabel";

/**
 * Every generate button: the action and, smaller, the model and its estimated cost; a spinner while generating.
 * With `menu` it is a split button: the ▾ at the end opens the model choices (the same items as the right-click
 * menu on the image: the configured model, Generate with ›).
 */
export function GenerateButton({
  onClick,
  loading = false,
  label,
  detail,
  menu,
  className,
  disabled = false,
  size,
  variant,
  title,
}: {
  onClick: () => void;
  /** Shows a spinner and "Generating…" (and disables). */
  loading?: boolean;
  label: string;
  /** Smaller text after the label: the model and its estimated cost. */
  detail?: GenerateDetail | null;
  /** The model choices (DropdownMenuItem elements), opened by the ▾ part. */
  menu?: ReactNode;
  className?: string;
  disabled?: boolean;
  size?: ComponentProps<typeof Button>["size"];
  /** 同一区域已有主操作时（如详情里每条衍生的生成）用次要变体。 */
  variant?: ComponentProps<typeof Button>["variant"];
  /** Why it is disabled, as a tooltip. */
  title?: string;
}) {
  const { t } = useTranslation(["assets", "dashboard"]);
  const main = (
    <Button
      onClick={onClick}
      disabled={disabled || loading}
      size={size}
      variant={variant}
      title={title}
      className={cn(menu ? "min-w-0 flex-1 shrink overflow-hidden" : className, !menu && "min-w-0 max-w-full overflow-hidden")}
    >
      {loading ? (
        <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
      ) : (
        <Sparkles aria-hidden data-icon="inline-start" />
      )}
      {loading ? t("assets:gallery_generating") : <GenerateLabel label={label} detail={detail} />}
    </Button>
  );
  if (!menu) return main;
  return (
    <ButtonGroup className={cn("w-full", className)}>
      {main}
      <ButtonGroupSeparator />
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              size={size}
              variant={variant}
              disabled={disabled || loading}
              aria-label={t("dashboard:generate_choose_model")}
              title={t("dashboard:generate_choose_model")}
            />
          }
        >
          <ChevronDown aria-hidden />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-auto min-w-56">
          {menu}
        </DropdownMenuContent>
      </DropdownMenu>
    </ButtonGroup>
  );
}
