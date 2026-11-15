import { useTranslation } from "react-i18next";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

interface PlanDurationSelectProps {
  seconds: number;
  /** Selectable options (ascending); when null or empty only the seconds are shown. / 可选档位（升序）；为 null 或空时只显示秒数。 */
  options: number[] | null;
  onChange: (seconds: number) => void;
  label: string;
  disabled?: boolean;
  /** Duration fixed by the endpoint: attach a note; still selectable when there are options (options borrowed by the screenplay plan). / 时长由端点固定：附上说明；有档位（剧本规划借用的档位）时仍可选。 */
  endpointFixed?: boolean;
}

/** Duration dropdown for script plan entries, shared by the three plans on the content review page. / 脚本规划条目的时长下拉，内容确认页三种规划共用。 */
export function PlanDurationSelect({
  seconds,
  options,
  onChange,
  label,
  disabled = false,
  endpointFixed = false,
}: PlanDurationSelectProps) {
  const { t } = useTranslation("dashboard");
  const notice = endpointFixed ? t("duration_not_driven_notice") : undefined;
  if (!options?.length) {
    return (
      <span className="text-xs text-muted-foreground">
        {t("reference_script_plan_duration_option", { seconds })}
        {notice && ` · ${notice}`}
      </span>
    );
  }
  // Stored seconds may no longer be in the current options: add an option for the current value, otherwise the dropdown cannot show the on-disk seconds.
  // 存量秒数可能已不在当前档位内：补一个当前值选项，否则下拉显示不出盘上的秒数。
  const values = options.includes(seconds) ? options : [...options, seconds].sort((a, b) => a - b);
  const items = values.map((value) => ({ value, label: t("reference_script_plan_duration_option", { seconds: value }) }));
  const select = (
    <Select
      items={items}
      value={seconds}
      onValueChange={(next) => {
        if (next !== null) onChange(next);
      }}
      disabled={disabled}
    >
      <SelectTrigger size="sm" aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false} align="start" className="w-auto">
        {items.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
  if (!notice) return select;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
      {select}
      {notice}
    </span>
  );
}
