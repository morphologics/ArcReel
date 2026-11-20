import { useId } from "react";
import { MonitorSmartphone } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  USER_OPTION_GROUPS,
  USER_OPTION_IDS,
  USER_OPTIONS,
  useUserOption,
  useUserOptionRequirements,
  type UserOptionChoice,
  type UserOptionDef,
  type UserOptionGroup,
  type UserOptionId,
  type UserOptionRequirement,
} from "@/preferences/user-options";

import { DiagnosticsSection } from "./DiagnosticsSection";
import { SettingRow } from "./SettingRow";

type Requirements = Record<UserOptionRequirement, boolean | null>;

function offered(requirement: UserOptionRequirement | undefined, requirements: Requirements): boolean {
  return !requirement || requirements[requirement] === true;
}

/**
 * Global settings "Options": the member's own preferences from the option registry, grouped under headings.
 * Every change applies at once (no save bar); options saved per device carry a "This device" note. Below them, the
 * studio's error log (Diagnostics).
 */
export function OptionsSection() {
  const { t } = useTranslation("dashboard");
  const requirements = useUserOptionRequirements();
  const groups = USER_OPTION_GROUPS.map((group) => ({
    group,
    ids: USER_OPTION_IDS.filter((id) => {
      const def: UserOptionDef = USER_OPTIONS[id];
      return def.group === group && offered(def.requires, requirements);
    }),
  })).filter(({ ids }) => ids.length > 0);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h2 className="text-lg font-medium">{t("settings_options")}</h2>
        <p className="text-sm text-muted-foreground">{t("options_desc")}</p>
      </div>
      {groups.map(({ group, ids }) => (
        <OptionGroup key={group} group={group} ids={ids} requirements={requirements} />
      ))}
      {/* YouReel: the errors this studio's UI reported, for us to trace problems */}
      <DiagnosticsSection />
    </div>
  );
}

function OptionGroup({ group, ids, requirements }: { group: UserOptionGroup; ids: UserOptionId[]; requirements: Requirements }) {
  const { t } = useTranslation("dashboard");
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-2">
      <h3 id={headingId} className="text-sm font-medium">
        {t(`options_group_${group}`)}
      </h3>
      <div className="flex flex-col divide-y divide-border rounded-lg border border-border bg-card">
        {ids.map((id) => (
          <OptionRow key={id} id={id} requirements={requirements} />
        ))}
      </div>
    </section>
  );
}

function OptionRow({ id, requirements }: { id: UserOptionId; requirements: Requirements }) {
  const { t } = useTranslation("dashboard");
  const labelId = useId();
  const def: UserOptionDef = USER_OPTIONS[id];
  const [value, setValue] = useUserOption(id);
  const choiceLabel = (choice: UserOptionChoice<string>) => choice.label ?? t(choice.labelKey ?? "");
  // Only what this studio offers is listed, and the select shows what actually applies here: a value whose requirement
  // is missing (e.g. OpenReel, the default editor, where OpenReel isn't served) shows as the first offered choice,
  // which is what opens (useDefaultEditor)
  const items = def.choices
    .filter((choice) => offered(choice.requires, requirements))
    .map((choice) => ({ value: choice.value, label: choiceLabel(choice) }));
  const shown = items.some((item) => item.value === value) ? value : (items[0]?.value ?? value);

  return (
    <SettingRow
      label={
        <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span id={labelId} className="text-sm font-medium">
            {t(def.labelKey)}
          </span>
          {def.storage === "device" && (
            <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
              <MonitorSmartphone aria-hidden className="size-3.5" />
              {t("options_this_device")}
            </span>
          )}
        </span>
      }
      description={def.descriptionKey ? t(def.descriptionKey) : undefined}
    >
      {def.type === "toggle" ? (
        <Switch
          aria-labelledby={labelId}
          checked={value === "on"}
          onCheckedChange={(checked) => setValue(checked ? "on" : "off")}
        />
      ) : (
        <Select
          items={items}
          value={shown}
          onValueChange={(next) => {
            const choice = def.choices.find((c) => c.value === next);
            if (choice) setValue(choice.value as never);
          }}
        >
          <SelectTrigger aria-labelledby={labelId} className="w-64 max-w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {items.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </SettingRow>
  );
}
