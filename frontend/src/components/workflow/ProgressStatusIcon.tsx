import { useTranslation } from "react-i18next";
import {
  CheckCheck,
  CircleArrowRight,
  Circle,
  CloudOff,
  LoaderCircle,
  MessageSquareWarning,
  OctagonAlert,
  RefreshCw,
  type LucideIcon,
} from "lucide-react";
import { cn } from "cn";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverDescription, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";

import type { ProgressHeadlineKind } from "./step-list";

/** The header status: the episode's headline kinds plus the panel's own "loading" and "unavailable". */
export type ProgressStatusKind = ProgressHeadlineKind | "loading" | "unavailable";

const STATUS: Record<ProgressStatusKind, { icon: LucideIcon; className: string; spin?: boolean }> = {
  complete: { icon: CheckCheck, className: "text-good" },
  next: { icon: CircleArrowRight, className: "text-primary" },
  running: { icon: RefreshCw, className: "text-primary", spin: true },
  warn: { icon: MessageSquareWarning, className: "text-warn" },
  danger: { icon: OctagonAlert, className: "text-destructive" },
  none: { icon: Circle, className: "text-muted-foreground" },
  loading: { icon: LoaderCircle, className: "text-muted-foreground", spin: true },
  unavailable: { icon: CloudOff, className: "text-muted-foreground" },
};

/**
 * The status icon beside the Production progress button: one icon and colour per status; hovering, clicking or
 * tapping it opens a small popup with the status title and, where there is one, a detail line. Clicking the button
 * itself still opens the full panel.
 */
export function ProgressStatusIcon({
  kind,
  title,
  detail,
}: {
  kind: ProgressStatusKind;
  title: string;
  detail: string | null;
}) {
  const { t } = useTranslation("workflow");
  const { icon: Icon, className, spin } = STATUS[kind];
  return (
    <Popover>
      <PopoverTrigger
        openOnHover
        delay={150}
        closeDelay={150}
        data-testid="workflow-status"
        data-status={kind}
        render={<Button variant="ghost" size="icon-xs" aria-label={t("status_icon_aria", { title })} />}
      >
        <Icon aria-hidden className={cn(className, spin && "animate-spin")} />
      </PopoverTrigger>
      <PopoverContent side="bottom" align="end" className="w-auto max-w-xs">
        <PopoverTitle>{title}</PopoverTitle>
        {detail ? <PopoverDescription>{detail}</PopoverDescription> : null}
      </PopoverContent>
    </Popover>
  );
}
