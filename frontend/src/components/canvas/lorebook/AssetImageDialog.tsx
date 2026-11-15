import { useState } from "react";
import { Dialog, DialogBody, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";

/**
 * View the full image inside the details Sheet. Rendered within the Sheet's subtree: the Sheet is a modal, and an overlay mounted elsewhere would
 * count as an outside click and close the Sheet.
 *
 * 在详情 Sheet 里查看大图。放在 Sheet 的子树里渲染：Sheet 是模态弹层，挂到别处的浮层会被它
 * 当作外部点击而关闭 Sheet。
 */
export function AssetImageDialog({
  src,
  title,
  alt,
  onClose,
}: {
  /** Closed when null. / 为 null 时关闭。 */
  src: string | null;
  title: string;
  alt: string;
  onClose: () => void;
}) {
  // Keep the previous image during the close animation / 关闭动画期间沿用上一张图
  const [shown, setShown] = useState(src);
  if (src !== null && src !== shown) setShown(src);
  return (
    <Dialog
      open={src !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      {/* The viewing area is fixed dark: a light background would distort judgments of picture brightness / 看图区域固定深色：浅底会干扰对画面明暗的判断 */}
      <DialogContent size="xl" className="dark">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          {shown && <img src={shown} alt={alt} className="mx-auto max-h-[70dvh] w-auto max-w-full object-contain" />}
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
