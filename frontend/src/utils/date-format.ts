const cache = new Map<string, Intl.DateTimeFormat>();

function getFormatter(lang: string | undefined, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${lang ?? ""}|${JSON.stringify(options)}`;
  let fmt = cache.get(key);
  if (!fmt) {
    try {
      fmt = new Intl.DateTimeFormat(lang, options);
    } catch {
      fmt = new Intl.DateTimeFormat(undefined, options);
    }
    cache.set(key, fmt);
  }
  return fmt;
}

// An ISO string without an explicit timezone suffix (Z / ±HH(:MM)) is treated as UTC to avoid browser ambiguity.
// The timestamp parsing rule is shared between time display and duration computation, hence exported.
// ISO 字符串若没有显式时区后缀（Z / ±HH(:MM)），按 UTC 处理避免浏览器歧义。
// 时间戳解析规则在时刻展示与时长计算之间共用，故导出。
export function parseIsoTimestamp(value: string): Date {
  const hasTz = /(?:Z|[+-]\d{2}(?::?\d{2})?)$/.test(value);
  return new Date(hasTz ? value : `${value}Z`);
}

export function formatDate(
  value: string | Date | null | undefined,
  lang: string,
  options: Intl.DateTimeFormatOptions,
  fallback = "—",
): string {
  if (value === null || value === undefined || value === "") return fallback;
  const date = typeof value === "string" ? parseIsoTimestamp(value) : value;
  if (Number.isNaN(date.getTime())) return fallback;
  return getFormatter(lang, options).format(date);
}

// Local time HH:mm, for same-day conversation flow; returns null on parse failure for the caller to handle
// 本地时刻 HH:mm，用于同一天内的对话流；解析失败返回 null 由调用方兜底
export function formatClockTime(value: string | null | undefined): string | null {
  if (!value) return null;
  const d = parseIsoTimestamp(value);
  if (Number.isNaN(d.getTime())) return null;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Compact local time MM/DD HH:mm, for dense lists/titles; returns null on parse failure for the caller to handle
// 紧凑本地时间 MM/DD HH:mm，用于信息密集的列表/标题；解析失败返回 null 由调用方兜底
export function formatShortDateTime(value: string | null | undefined): string | null {
  if (!value) return null;
  const d = parseIsoTimestamp(value);
  if (Number.isNaN(d.getTime())) return null;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["day", 86_400_000],
  ["hour", 3_600_000],
  ["minute", 60_000],
];

const JUST_NOW_MS = 60_000;

const relativeCache = new Map<string, Intl.RelativeTimeFormat>();

function getRelativeFormatter(lang: string): Intl.RelativeTimeFormat {
  let fmt = relativeCache.get(lang);
  if (!fmt) {
    try {
      fmt = new Intl.RelativeTimeFormat(lang, { numeric: "auto" });
    } catch {
      fmt = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    }
    relativeCache.set(lang, fmt);
  }
  return fmt;
}

// Less than a minute from now (including slightly later than now due to clock skew) counts as "just now"; returns false on parse failure.
// "Just now" is embedded in whole sentences with wording varying by sentence, so callers choose their own text rather than Intl's "now".
// 与当前相差不足一分钟（含时钟偏差造成的略晚于当前）算「刚刚」；解析失败返回 false。
// 「刚刚」要嵌进整句话，措辞因句子而异，由调用方各自选文案，不交给 Intl 的「现在」。
export function isJustNow(value: string | null | undefined, now: number = Date.now()): boolean {
  if (!value) return false;
  const time = parseIsoTimestamp(value).getTime();
  return !Number.isNaN(time) && Math.abs(time - now) < JUST_NOW_MS;
}

// Coarse time relative to now ("3 hours ago"), worded per UI language; under a minute is left to Intl ("now"), returns null on parse failure.
// Callers needing "just now" check isJustNow first.
// 相对当前的粗粒度时间（「3 小时前」），按界面语言成文；不足一分钟交给 Intl 成文（「现在」），解析失败返回 null。
// 需要「刚刚」的调用方先用 isJustNow 判断。
export function formatRelativeTime(
  value: string | null | undefined,
  lang: string,
  now: number = Date.now(),
): string | null {
  if (!value) return null;
  const time = parseIsoTimestamp(value).getTime();
  if (Number.isNaN(time)) return null;
  const diff = time - now;
  const fmt = getRelativeFormatter(lang);
  for (const [unit, ms] of RELATIVE_UNITS) {
    if (Math.abs(diff) >= ms) return fmt.format(Math.round(diff / ms), unit);
  }
  return fmt.format(0, "second");
}
