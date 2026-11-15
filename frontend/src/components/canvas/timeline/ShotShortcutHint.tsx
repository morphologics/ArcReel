import { useTranslation } from "react-i18next";
import { Kbd, KbdGroup } from "@/components/ui/kbd";
import { formatBinding, useHotkeyBinding, type HotkeyBinding } from "@/preferences/hotkeys";

function BindingKeys({ binding }: { binding: HotkeyBinding }) {
  return (
    <KbdGroup>
      {formatBinding(binding).map((part) => (
        <Kbd key={part}>{part}</Kbd>
      ))}
    </KbdGroup>
  );
}

/** Shortcut hint always shown at the bottom of the shot list, with this device's bindings. In read-only display (cannot save) saving is not mentioned. / 分镜列表底部常驻的快捷键提示。只读展示（不能保存）时不提保存。 */
export function ShotShortcutHint({ canSave }: { canSave: boolean }) {
  const { t } = useTranslation("dashboard");
  const prev = useHotkeyBinding("storyboard.prev_shot");
  const next = useHotkeyBinding("storyboard.next_shot");
  const save = useHotkeyBinding("storyboard.save_shot");
  return (
    <p className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-border/50 px-3 py-2 text-xs text-muted-foreground">
      <span className="inline-flex items-center gap-1.5">
        <BindingKeys binding={prev} />
        <BindingKeys binding={next} />
        {t("shot_shortcut_switch")}
      </span>
      {canSave ? (
        <span className="inline-flex items-center gap-1.5">
          <BindingKeys binding={save} />
          {t("shot_shortcut_save")}
        </span>
      ) : null}
    </p>
  );
}
