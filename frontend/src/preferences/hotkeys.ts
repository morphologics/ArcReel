import { useEffect, useRef, useSyncExternalStore, type RefObject } from "react";

/**
 * The hot key registry and its dispatcher: every rebindable keyboard action, the view it works in (scope), its
 * default key, and one hook (`useHotkeys`) a view mounts to handle its scope's actions.
 *
 * Custom bindings are saved per device (localStorage): keyboards and layouts differ between devices. Keys that
 * belong to a widget (Esc closing a panel, arrow keys on sliders and splitters, Enter / Space on buttons) are not
 * actions here; the Hot keys panel lists them read-only from `FIXED_KEYS`.
 */

export const HOTKEY_SCOPES = ["global", "storyboard", "edit"] as const;
export type HotkeyScope = (typeof HOTKEY_SCOPES)[number];

/** A key with its modifiers. `key` is `KeyboardEvent.key`, single characters lower-cased. `mod` is ⌘ or Ctrl. */
export interface HotkeyBinding {
  key: string;
  mod?: boolean;
  alt?: boolean;
  shift?: boolean;
}

export interface HotkeyActionDef {
  scope: HotkeyScope;
  labelKey: string;
  defaultBinding: HotkeyBinding;
  /** Also fires while typing in a text field, as long as the binding uses ⌘ / Ctrl (a plain key there is typed). */
  inTextFields?: boolean;
  /** Outside its scope (a popover, the agent panel) the key does nothing but still blocks the browser's own use of it. */
  claimOutsideScope?: boolean;
}

export const HOTKEY_ACTIONS = {
  // J is the left-hand key: the shot strip runs left to right
  "storyboard.prev_shot": { scope: "storyboard", labelKey: "dashboard:hotkeys_action_prev_shot", defaultBinding: { key: "j" } },
  "storyboard.next_shot": { scope: "storyboard", labelKey: "dashboard:hotkeys_action_next_shot", defaultBinding: { key: "k" } },
  "storyboard.save_shot": {
    scope: "storyboard",
    labelKey: "dashboard:hotkeys_action_save_shot",
    defaultBinding: { key: "s", mod: true },
    inTextFields: true,
    claimOutsideScope: true,
  },
} satisfies Record<string, HotkeyActionDef>;

export type HotkeyActionId = keyof typeof HOTKEY_ACTIONS;
export const HOTKEY_ACTION_IDS = Object.keys(HOTKEY_ACTIONS) as HotkeyActionId[];

/** Keys owned by widgets, not rebindable; shown read-only in the panel. */
export const FIXED_KEYS: readonly { keys: readonly HotkeyBinding[]; labelKey: string }[] = [
  { keys: [{ key: "Escape" }], labelKey: "dashboard:hotkeys_fixed_escape" },
  {
    keys: [{ key: "ArrowLeft" }, { key: "ArrowRight" }, { key: "ArrowUp" }, { key: "ArrowDown" }],
    labelKey: "dashboard:hotkeys_fixed_arrows",
  },
  { keys: [{ key: "Enter" }, { key: " " }], labelKey: "dashboard:hotkeys_fixed_enter_space" },
];

/** Keys a binding cannot use without ⌘ / Ctrl or Alt, because widgets already answer them. */
const RESERVED_PLAIN_KEYS = new Set(["Escape", "Tab", "Enter", " ", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"]);
const MODIFIER_KEYS = new Set(["Shift", "Control", "Alt", "Meta", "AltGraph", "CapsLock", "Fn", "OS", "Hyper", "Super"]);

export function isApplePlatform(): boolean {
  return typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.userAgent);
}

function normalizeKey(key: string): string {
  return key.length === 1 ? key.toLowerCase() : key;
}

export function sameBinding(a: HotkeyBinding, b: HotkeyBinding): boolean {
  return (
    normalizeKey(a.key) === normalizeKey(b.key) &&
    Boolean(a.mod) === Boolean(b.mod) &&
    Boolean(a.alt) === Boolean(b.alt) &&
    Boolean(a.shift) === Boolean(b.shift)
  );
}

/** ⌘ and Ctrl both count as `mod` on every platform, as the browser's own shortcuts are matched by users either way. */
export function matchesBinding(event: KeyboardEvent, binding: HotkeyBinding): boolean {
  return (
    normalizeKey(event.key) === normalizeKey(binding.key) &&
    (event.metaKey || event.ctrlKey) === Boolean(binding.mod) &&
    event.altKey === Boolean(binding.alt) &&
    event.shiftKey === Boolean(binding.shift)
  );
}

export type RecordedKey =
  | { kind: "binding"; binding: HotkeyBinding }
  /** Only a modifier so far: keep waiting. */
  | { kind: "pending" }
  /** Esc: stop recording without a change. */
  | { kind: "cancel" }
  /** A key widgets already answer; needs ⌘ / Ctrl or Alt. */
  | { kind: "reserved" };

/** Turn a key press during "press a key" recording into a binding, or say why it isn't one. */
export function recordKey(event: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey">): RecordedKey {
  if (MODIFIER_KEYS.has(event.key) || event.key === "Process" || event.key === "Dead" || event.key === "Unidentified") {
    return { kind: "pending" };
  }
  const mod = event.metaKey || event.ctrlKey;
  if (event.key === "Escape" && !mod && !event.altKey) return { kind: "cancel" };
  if (RESERVED_PLAIN_KEYS.has(event.key) && !mod && !event.altKey) return { kind: "reserved" };
  const binding: HotkeyBinding = { key: normalizeKey(event.key) };
  if (mod) binding.mod = true;
  if (event.altKey) binding.alt = true;
  if (event.shiftKey) binding.shift = true;
  return { kind: "binding", binding };
}

const KEY_NAMES: Record<string, string> = {
  " ": "Space",
  Escape: "Esc",
  ArrowLeft: "←",
  ArrowRight: "→",
  ArrowUp: "↑",
  ArrowDown: "↓",
};

/** The keys of a binding as shown on key caps, e.g. ["⌘", "S"] or ["Ctrl", "S"]. */
export function formatBinding(binding: HotkeyBinding, apple = isApplePlatform()): string[] {
  const parts: string[] = [];
  if (binding.mod) parts.push(apple ? "⌘" : "Ctrl");
  if (binding.alt) parts.push(apple ? "⌥" : "Alt");
  if (binding.shift) parts.push(apple ? "⇧" : "Shift");
  const key = KEY_NAMES[binding.key] ?? (binding.key.length === 1 ? binding.key.toUpperCase() : binding.key);
  parts.push(key);
  return parts;
}

// ---------- per-device bindings ----------

export const HOTKEYS_STORAGE_KEY = "youreel.hotkeys";
type Overrides = Partial<Record<HotkeyActionId, HotkeyBinding>>;

const listeners = new Set<() => void>();
let cachedRaw: string | null | undefined;
let cachedOverrides: Overrides = {};
let storageEventsBound = false;

function readRaw(): string | null {
  try {
    return window.localStorage.getItem(HOTKEYS_STORAGE_KEY);
  } catch {
    return null;
  }
}

function isBinding(value: unknown): value is HotkeyBinding {
  return typeof value === "object" && value !== null && typeof (value as HotkeyBinding).key === "string";
}

function parseOverrides(raw: string | null): Overrides {
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return {};
    const out: Overrides = {};
    for (const id of HOTKEY_ACTION_IDS) {
      const value = (parsed as Record<string, unknown>)[id];
      if (isBinding(value)) out[id] = value;
    }
    return out;
  } catch {
    return {};
  }
}

/** The bindings this device has changed (the same object until they change again). */
export function getHotkeyOverrides(): Overrides {
  const raw = readRaw();
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    cachedOverrides = parseOverrides(raw);
  }
  return cachedOverrides;
}

function writeOverrides(next: Overrides) {
  try {
    if (Object.keys(next).length === 0) window.localStorage.removeItem(HOTKEYS_STORAGE_KEY);
    else window.localStorage.setItem(HOTKEYS_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage blocked: the panel shows the change, but it is not kept
  }
  for (const listener of listeners) listener();
}

export function setHotkeyBinding(id: HotkeyActionId, binding: HotkeyBinding): void {
  const next = { ...getHotkeyOverrides() };
  if (sameBinding(binding, HOTKEY_ACTIONS[id].defaultBinding)) delete next[id];
  else next[id] = binding;
  writeOverrides(next);
}

export function resetHotkeyBinding(id: HotkeyActionId): void {
  const next = { ...getHotkeyOverrides() };
  delete next[id];
  writeOverrides(next);
}

export function resetAllHotkeyBindings(): void {
  writeOverrides({});
}

function subscribe(listener: () => void) {
  if (!storageEventsBound) {
    storageEventsBound = true;
    window.addEventListener("storage", (event) => {
      if (event.key === HOTKEYS_STORAGE_KEY || event.key === null) for (const l of listeners) l();
    });
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function bindingFor(id: HotkeyActionId, overrides: Overrides = getHotkeyOverrides()): HotkeyBinding {
  return overrides[id] ?? HOTKEY_ACTIONS[id].defaultBinding;
}

/** This device's changed bindings; re-renders when they change (also from another tab). */
export function useHotkeyOverrides(): Overrides {
  return useSyncExternalStore(subscribe, getHotkeyOverrides);
}

/** Current binding of one action. */
export function useHotkeyBinding(id: HotkeyActionId): HotkeyBinding {
  return bindingFor(id, useHotkeyOverrides());
}

/** Other actions bound to the same key where both can fire: the same scope, or either one global. */
export function findClashes(id: HotkeyActionId, overrides: Overrides = getHotkeyOverrides()): HotkeyActionId[] {
  const scope = HOTKEY_ACTIONS[id].scope;
  const binding = bindingFor(id, overrides);
  return HOTKEY_ACTION_IDS.filter((other) => {
    if (other === id) return false;
    const otherScope = HOTKEY_ACTIONS[other].scope;
    if (otherScope !== scope && otherScope !== "global" && scope !== "global") return false;
    return sameBinding(binding, bindingFor(other, overrides));
  });
}

// ---------- dispatcher ----------

/** Focus is in a control that takes typed text: plain keys there are characters, not hot keys. */
export function isTypingTarget(target: Element): boolean {
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) {
    return !["button", "checkbox", "radio", "range", "reset", "submit", "color", "file"].includes(target.type);
  }
  if (target instanceof HTMLElement && target.isContentEditable) return true;
  const role = target.getAttribute("role");
  return role === "textbox" || role === "combobox" || role === "searchbox";
}

export type HotkeyHandlers = Partial<Record<HotkeyActionId, (() => void) | undefined>>;

interface UseHotkeysOptions {
  /**
   * The scope's actions work only while focus is inside this element (or nothing has focus), so they don't take
   * keys from the agent panel, the header or popovers. Omitted for `global`: everywhere.
   */
  rootRef?: RefObject<HTMLElement | null>;
  /** Handlers by action. An action without a handler (undefined) is off: its key passes through untouched. */
  handlers: HotkeyHandlers;
}

/**
 * Handle a scope's hot keys with this device's bindings. Ignored: keys another handler already took
 * (`defaultPrevented`), keys during IME composition, and plain keys while typing in a text field. A matched key
 * outside the scope does nothing, unless its action claims it there (only the browser default is blocked).
 */
export function useHotkeys(scope: HotkeyScope, { rootRef, handlers }: UseHotkeysOptions): void {
  const overrides = useHotkeyOverrides();
  // Handlers are read at key time, so callers need not memoise the map
  const handlersRef = useRef(handlers);
  useEffect(() => {
    handlersRef.current = handlers;
  });

  useEffect(() => {
    const actions = HOTKEY_ACTION_IDS.filter((id) => HOTKEY_ACTIONS[id].scope === scope);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.key === "Process") return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const inScope = !rootRef || target === document.body || Boolean(rootRef.current?.contains(target));

      for (const id of actions) {
        const handler = handlersRef.current[id];
        if (!handler) continue;
        const binding = bindingFor(id, overrides);
        if (!matchesBinding(event, binding)) continue;
        const action: HotkeyActionDef = HOTKEY_ACTIONS[id];
        if (!inScope) {
          if (action.claimOutsideScope) event.preventDefault();
          return;
        }
        if (isTypingTarget(target) && !(action.inTextFields && binding.mod)) return;
        event.preventDefault();
        handler();
        return;
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [scope, rootRef, overrides]);
}
