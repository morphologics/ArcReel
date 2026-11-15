import { useEffect, useId, useState, type ReactNode } from "react";
import { AlertTriangle, Keyboard, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Kbd, KbdGroup } from "@/components/ui/kbd";
import {
  FIXED_KEYS,
  findClashes,
  formatBinding,
  getHotkeyOverrides,
  HOTKEY_ACTION_IDS,
  HOTKEY_ACTIONS,
  HOTKEY_SCOPES,
  bindingFor,
  recordKey,
  resetAllHotkeyBindings,
  resetHotkeyBinding,
  setHotkeyBinding,
  useHotkeyOverrides,
  type HotkeyActionId,
  type HotkeyBinding,
  type HotkeyScope,
} from "@/preferences/hotkeys";
import { useAppStore } from "@/stores/app-store";

import { SettingRow } from "./SettingRow";

function BindingKeys({ binding }: { binding: HotkeyBinding }) {
  return (
    <KbdGroup>
      {formatBinding(binding).map((part) => (
        <Kbd key={part}>{part}</Kbd>
      ))}
    </KbdGroup>
  );
}

/**
 * Global settings "Hot keys": the hot key registry's actions by scope, each with its key on this device. "Change"
 * records the next key pressed (Esc or Cancel stops); bindings are saved per device. Widget keys are listed
 * read-only at the end.
 */
export function HotkeysSection() {
  const { t } = useTranslation(["dashboard", "common"]);
  const overrides = useHotkeyOverrides();
  const [recording, setRecording] = useState<HotkeyActionId | null>(null);
  const [reserved, setReserved] = useState(false);
  const scopes = HOTKEY_SCOPES.map((scope) => ({
    scope,
    ids: HOTKEY_ACTION_IDS.filter((id) => HOTKEY_ACTIONS[id].scope === scope),
  })).filter(({ ids }) => ids.length > 0);

  useEffect(() => {
    if (!recording) return;
    // Capture phase on window: the key is taken before any other handler, wherever focus is
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      event.preventDefault();
      event.stopPropagation();
      const result = recordKey(event);
      if (result.kind === "pending") return;
      if (result.kind === "reserved") {
        setReserved(true);
        return;
      }
      if (result.kind === "binding") setHotkeyBinding(recording, result.binding);
      setRecording(null);
      setReserved(false);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [recording]);

  const startRecording = (id: HotkeyActionId) => {
    setReserved(false);
    setRecording(id);
  };
  const stopRecording = () => {
    setReserved(false);
    setRecording(null);
  };

  const resetAll = () => {
    const previous = getHotkeyOverrides();
    resetAllHotkeyBindings();
    useAppStore.getState().pushToast(t("dashboard:hotkeys_reset_all_done"), "success", {
      action: {
        label: t("common:undo"),
        onClick: () => {
          for (const id of HOTKEY_ACTION_IDS) {
            const binding = previous[id];
            if (binding) setHotkeyBinding(id, binding);
          }
        },
      },
    });
  };

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-1 basis-72 flex-col gap-1">
          <h2 className="text-lg font-medium">{t("dashboard:settings_hotkeys")}</h2>
          <p className="text-sm text-muted-foreground">{t("dashboard:hotkeys_desc")}</p>
          <p className="flex items-start gap-1.5 text-sm text-muted-foreground">
            <Keyboard aria-hidden className="mt-0.5 size-4 shrink-0" />
            {t("dashboard:hotkeys_tablet_note")}
          </p>
        </div>
        <Button variant="outline" disabled={Object.keys(overrides).length === 0} onClick={resetAll}>
          <RotateCcw aria-hidden data-icon="inline-start" />
          {t("dashboard:hotkeys_reset_all")}
        </Button>
      </div>

      {scopes.map(({ scope, ids }) => (
        <ScopeGroup
          key={scope}
          scope={scope}
          ids={ids}
          recording={recording}
          reserved={reserved}
          onRecord={startRecording}
          onStop={stopRecording}
        />
      ))}

      <FixedKeys />
    </div>
  );
}

interface ScopeGroupProps {
  scope: HotkeyScope;
  ids: HotkeyActionId[];
  recording: HotkeyActionId | null;
  reserved: boolean;
  onRecord: (id: HotkeyActionId) => void;
  onStop: () => void;
}

function ScopeGroup({ scope, ids, ...rowProps }: ScopeGroupProps) {
  const { t } = useTranslation("dashboard");
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-2">
      <h3 id={headingId} className="text-sm font-medium">
        {t(`hotkeys_scope_${scope}`)}
      </h3>
      <div className="flex flex-col divide-y divide-border rounded-lg border border-border bg-card">
        {ids.map((id) => (
          <ActionRow key={id} id={id} {...rowProps} />
        ))}
      </div>
    </section>
  );
}

function ActionRow({
  id,
  recording,
  reserved,
  onRecord,
  onStop,
}: { id: HotkeyActionId } & Omit<ScopeGroupProps, "scope" | "ids">) {
  const { t } = useTranslation(["dashboard", "common"]);
  const overrides = useHotkeyOverrides();
  const labelId = useId();
  const action = t(HOTKEY_ACTIONS[id].labelKey);
  const binding = bindingFor(id, overrides);
  const custom = Boolean(overrides[id]);
  const clashes = findClashes(id, overrides);
  const isRecording = recording === id;

  let description: ReactNode;
  if (isRecording) {
    description = (
      <span role="status">{reserved ? t("dashboard:hotkeys_reserved") : t("dashboard:hotkeys_recording_hint")}</span>
    );
  } else if (clashes.length > 0) {
    description = (
      <span className="inline-flex items-start gap-1.5 text-warn">
        <AlertTriangle aria-hidden className="mt-0.5 size-3.5 shrink-0" />
        {t("dashboard:hotkeys_clash", { actions: clashes.map((other) => t(HOTKEY_ACTIONS[other].labelKey)).join(", ") })}
      </span>
    );
  }

  return (
    <SettingRow
      label={
        <span id={labelId} className="text-sm font-medium">
          {action}
        </span>
      }
      description={description}
      labelledBy={labelId}
    >
      <div className="flex flex-wrap items-center justify-end gap-2">
        {isRecording ? (
          <>
            <span className="text-sm text-muted-foreground">{t("dashboard:hotkeys_recording")}</span>
            <Button variant="outline" size="sm" onClick={onStop}>
              {t("common:cancel")}
            </Button>
          </>
        ) : (
          <>
            <BindingKeys binding={binding} />
            <Button
              variant="outline"
              size="sm"
              aria-label={t("dashboard:hotkeys_change_label", { action })}
              onClick={() => onRecord(id)}
            >
              {t("dashboard:hotkeys_change")}
            </Button>
            {custom && (
              <Button
                variant="ghost"
                size="sm"
                aria-label={t("dashboard:hotkeys_reset_label", { action })}
                onClick={() => resetHotkeyBinding(id)}
              >
                <RotateCcw aria-hidden data-icon="inline-start" />
                {t("dashboard:hotkeys_reset")}
              </Button>
            )}
          </>
        )}
      </div>
    </SettingRow>
  );
}

function FixedKeys() {
  const { t } = useTranslation("dashboard");
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-2">
      <div className="flex flex-col gap-0.5">
        <h3 id={headingId} className="text-sm font-medium">
          {t("hotkeys_fixed_title")}
        </h3>
        <p className="text-sm text-muted-foreground">{t("hotkeys_fixed_desc")}</p>
      </div>
      <div className="flex flex-col divide-y divide-border rounded-lg border border-border bg-card">
        {FIXED_KEYS.map((entry) => (
          <SettingRow key={entry.labelKey} label={<span className="text-sm">{t(entry.labelKey)}</span>}>
            <span className="flex flex-wrap items-center gap-1.5">
              {entry.keys.map((binding) => (
                <BindingKeys key={binding.key} binding={binding} />
              ))}
            </span>
          </SettingRow>
        ))}
      </div>
    </section>
  );
}
