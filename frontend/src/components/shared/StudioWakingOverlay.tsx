import { useTranslation } from "react-i18next";
import { useStudioWakingStore } from "@/api/studio-waking";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

/**
 * YouReel: while requests wait for the studio's backend to be woken (`api/studio-waking.ts`), a blocking dialog
 * covers the page; it closes as soon as the backend answers and the page carries on. It can't be dismissed (Escape,
 * a click outside): the page behind it has nothing to work with until then.
 *
 * What it shows is the host's waiting card, the same one as its waking page (one file on the host, so a new logo or
 * wording needs no app release), sent along with the wake answer and shown in a sandboxed frame: no scripts, its
 * styles kept apart from the app's. Until a wake answer has carried it, a built-in card in the app's theme.
 */
export function StudioWakingOverlay() {
  const waking = useStudioWakingStore((s) => s.waiting > 0);
  const html = useStudioWakingStore((s) => s.html);
  const { t } = useTranslation("common");
  return (
    <Dialog open={waking} onOpenChange={() => {}}>
      <DialogContent size="sm" showCloseButton={false}>
        {html ? (
          <div role="status" aria-live="polite">
            <span className="sr-only">
              <DialogTitle>{t("studio_waking_title")}</DialogTitle>
            </span>
            <iframe sandbox="" srcDoc={html} title={t("studio_waking_title")} className="block h-80 w-full border-0" />
          </div>
        ) : (
          <div role="status" aria-live="polite" className="flex flex-col items-center gap-2 px-7 pt-8 pb-7 text-center">
            <YouReelMark />
            <DialogTitle>{t("studio_waking_title")}</DialogTitle>
            <DialogDescription>{t("studio_waking_hint")}</DialogDescription>
            <div aria-hidden className="relative mt-4 h-1 w-56 max-w-full overflow-hidden rounded-full bg-muted">
              <div className="absolute inset-y-0 left-0 w-2/5 rounded-full bg-primary animate-waking-slide" />
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** The YouReel mark (as public/favicon.svg), drawn in the current text colour (the built-in card). */
function YouReelMark() {
  return (
    <svg aria-hidden viewBox="300 302 420 420" className="mb-3 size-12 text-foreground animate-waking-breathe">
      <g fill="currentColor" stroke="currentColor" strokeWidth="116" strokeLinejoin="round">
        <path d="M384 370 L384 655 L637 512 Z" />
      </g>
      {/* the mark's star cut-out, in the dialog's own background colour */}
      <path className="fill-popover" d="M480 395 A117 117 0 0 0 597 512 A117 117 0 0 0 480 629 A117 117 0 0 0 363 512 A117 117 0 0 0 480 395 Z" />
    </svg>
  );
}
