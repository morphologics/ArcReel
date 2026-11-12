import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Maximize } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

interface PreviewableImageFrameProps {
  src: string | null;
  alt: string;
  children: ReactNode;
  /** Always shown in the top-right corner, above the full-size button (e.g. the shot's scene break switch). */
  corner?: ReactNode;
}

/**
 * 给缩略图加「查看大图」：右上角的放大按钮在悬停或聚焦时出现（触屏上常显），点开后用占满视口的
 * 查看器显示原图。
 */
export function PreviewableImageFrame({ src, alt, children, corner }: PreviewableImageFrameProps) {
  const { t } = useTranslation("dashboard");
  const [open, setOpen] = useState(false);
  const label = t("image_fullscreen_preview", { name: alt });

  return (
    <>
      <div className="group relative">
        {children}
        {(src || corner) && (
          <div className="absolute top-1.5 right-1.5 flex flex-col items-end gap-1.5">
            {corner}
            {src && (
              <div className="opacity-0 transition-opacity duration-fast group-focus-within:opacity-100 group-hover:opacity-100 pointer-coarse:opacity-100">
                <Button
                  variant="ghost"
                  size="icon-lg"
                  onClick={(event) => {
                    event.stopPropagation();
                    setOpen(true);
                  }}
                  aria-label={label}
                >
                  {/* no backdrop: a half-transparent white icon with a shadow reads over light and dark pictures */}
                  <Maximize aria-hidden className="size-7 text-white/50 drop-shadow-md" />
                </Button>
              </div>
            )}
          </div>
        )}
      </div>

      {src && (
        <Dialog open={open} onOpenChange={setOpen}>
          {/* 看图区域固定深色：浅底会干扰对画面明暗的判断 */}
          <DialogContent size="viewer" className="dark">
            <DialogHeader>
              <DialogTitle>{alt}</DialogTitle>
            </DialogHeader>
            <DialogBody>
              <img src={src} alt={alt} className="size-full object-contain" />
            </DialogBody>
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}
