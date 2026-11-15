import { useRef } from "react";
import { Loader2, Upload } from "lucide-react";
import { TooltipIconButton } from "@/components/canvas/timeline/TooltipIconButton";

/** Keep in sync with the extension allowlist in backend upload_finalize.py / 与后端 upload_finalize.py 的扩展名白名单保持一致 */
export const UPLOAD_IMAGE_ACCEPT = ".png,.jpg,.jpeg,.webp";
export const UPLOAD_VIDEO_ACCEPT = ".mp4,.mov,.m4v";

interface UploadIconButtonProps {
  accept: string;
  /** Accessible name of the button, shown in the tooltip on hover or focus / 按钮的无障碍名称，悬停或聚焦时显示在提示里 */
  label: string;
  /** Upload request in progress: show spinner and disable / 上传请求进行中：显示 spinner 并禁用 */
  busy?: boolean;
  disabled?: boolean;
  onSelect: (file: File) => void;
}

/** Icon-style upload entry in card headers: a hidden file input plus an icon button, styled like the adjacent "Local edit" and "Versions". / 卡片头部的图标式上传入口：隐藏的文件输入框加一个图标按钮，与旁边的「局部修改」「版本」同样式。 */
export function UploadIconButton({ accept, label, busy, disabled, onSelect }: UploadIconButtonProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  return (
    <>
      <input
        ref={fileInputRef}
        type="file"
        accept={accept}
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          // Allow selecting the same file again to re-upload / 允许重复选择同一文件再次上传
          e.target.value = "";
          if (f) onSelect(f);
        }}
      />
      <TooltipIconButton label={label} disabled={busy || disabled} onClick={() => fileInputRef.current?.click()}>
        {busy ? <Loader2 className="animate-spin" aria-hidden="true" /> : <Upload aria-hidden="true" />}
      </TooltipIconButton>
    </>
  );
}
