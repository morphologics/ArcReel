import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useStudioWakingStore } from "@/api/studio-waking";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

/**
 * YouReel: while requests wait for the studio's backend to be woken (`api/studio-waking.ts`), a blocking dialog
 * "Studio is restarting… retrying" covers the page; it closes as soon as the backend answers and the page carries on.
 * It can't be dismissed (Escape, a click outside): the page behind it has nothing to work with until then.
 */
export function StudioWakingOverlay() {
  const waking = useStudioWakingStore((s) => s.waiting > 0);
  const { t } = useTranslation("common");
  return (
    <Dialog open={waking} onOpenChange={() => {}}>
      <DialogContent size="sm" showCloseButton={false}>
        <div role="status" aria-live="polite" className="flex items-center justify-center gap-2 p-6 text-muted-foreground">
          <Loader2 aria-hidden className="size-4 animate-spin" />
          <DialogTitle>{t("backend_unreachable_retrying")}</DialogTitle>
        </div>
      </DialogContent>
    </Dialog>
  );
}
