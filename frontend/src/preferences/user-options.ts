import { useCallback, useEffect, useSyncExternalStore } from "react";
import { create } from "zustand";

import { API } from "@/api";
import { EDITOR_BRAND } from "@/branding";
import { useOpenReelAvailable } from "@/components/canvas/edit/open-in-openreel";
import i18n, { LANGUAGE_DISPLAY_LABELS, SUPPORTED_LANGUAGES, type SupportedLanguage } from "@/i18n";
import { useAppStore } from "@/stores/app-store";
import { getThemePreference, setThemePreference, subscribeThemePreference, THEME_PREFERENCES } from "@/theme";
import type { StudioUserOptions } from "@/types";

/**
 * The user options registry: every option the Settings "Options" panel shows, with its labels, allowed values,
 * default and where it is saved.
 *
 * - `studio`: saved on the server (`/user-options`), shared by every device the member opens the studio on.
 * - `device`: saved in this browser (localStorage), for options tied to a device's screen, input or habits.
 *
 * Read and change an option with `useUserOption(id)`; the panel and every feature that reads an option go through
 * it, so the registry is the only place an option's values and default are defined (the server validates the
 * studio options' values against the same sets, see `server/routers/user_options.py`).
 */

export type UserOptionStorage = "studio" | "device";
/** Panel heading an option is listed under, in this order. */
export const USER_OPTION_GROUPS = ["display", "workflow", "openreel"] as const;
export type UserOptionGroup = (typeof USER_OPTION_GROUPS)[number];
/** Something the studio must serve for an option (or one of its values) to be offered. */
export type UserOptionRequirement = "openreel";

export interface UserOptionChoice<V extends string> {
  value: V;
  /** Translation key of the value's label; `label` instead for names that are never translated. */
  labelKey?: string;
  label?: string;
  requires?: UserOptionRequirement;
}

interface UserOptionCommon<V extends string> {
  group: UserOptionGroup;
  labelKey: string;
  descriptionKey?: string;
  /** `choice`: pick one value from a list. `toggle`: on / off, with the values `on` and `off`. */
  type: "choice" | "toggle";
  choices: readonly UserOptionChoice<V>[];
  default: V;
  /** The whole option is shown only where the studio serves this. */
  requires?: UserOptionRequirement;
}

/** How a device option is read, written and watched in this browser. */
export interface DeviceOptionStore<V extends string> {
  read(this: void): V;
  // Method syntax: a store of a narrower value set still fits the registry's generic `DeviceOptionStore<string>`
  write(this: void, value: V): void;
  subscribe(this: void, listener: () => void): () => void;
}

export interface StudioOptionDef<V extends string> extends UserOptionCommon<V> {
  storage: "studio";
  /** Key in the server's options object. */
  key: keyof StudioUserOptions;
}

export interface DeviceOptionDef<V extends string> extends UserOptionCommon<V> {
  storage: "device";
  device: DeviceOptionStore<V>;
}

export type UserOptionDef<V extends string = string> = StudioOptionDef<V> | DeviceOptionDef<V>;

function studioOption<const K extends keyof StudioUserOptions>(
  key: K,
  def: Omit<StudioOptionDef<NonNullable<StudioUserOptions[K]>>, "storage" | "key" | "type">,
): StudioOptionDef<NonNullable<StudioUserOptions[K]>> {
  return { ...def, storage: "studio", key, type: "choice" };
}

function deviceOption<const V extends string>(
  def: Omit<DeviceOptionDef<V>, "storage" | "type">,
): DeviceOptionDef<V> {
  return { ...def, storage: "device", type: "choice" };
}

export type ToggleValue = "on" | "off";

/** The OpenReel timeline's auto re-centre on zoom, per device (same origin: OpenReel reads it). */
export const TIMELINE_RECENTER_STORAGE_KEY = "youreel.timeline.autoRecenter";

/** An on / off option kept in this browser under `storageKey`; the default is stored as "key absent", the other as its value. */
function deviceToggle(
  storageKey: string,
  def: Omit<DeviceOptionDef<ToggleValue>, "storage" | "type" | "choices" | "device">,
): DeviceOptionDef<ToggleValue> {
  return {
    ...def,
    storage: "device",
    type: "toggle",
    choices: [{ value: "on" }, { value: "off" }],
    device: localStorageChoice(storageKey, ["on", "off"], def.default),
  };
}

// ---------- device storage ----------

const localListeners = new Map<string, Set<() => void>>();
let storageEventsBound = false;

function notifyLocal(key: string) {
  for (const listener of localListeners.get(key) ?? []) listener();
}

function bindStorageEvents() {
  if (storageEventsBound) return;
  storageEventsBound = true;
  // Another tab of the same studio changed a value
  window.addEventListener("storage", (event) => {
    if (event.key === null) for (const key of localListeners.keys()) notifyLocal(key);
    else notifyLocal(event.key);
  });
}

/**
 * A choice kept under a fixed localStorage key. The default is stored as "key absent", so other readers of the
 * same key (OpenReel, served from the same origin) see exactly the value strings below or nothing.
 */
export function localStorageChoice<const V extends string>(
  storageKey: string,
  values: readonly V[],
  fallback: V,
): DeviceOptionStore<V> {
  return {
    read: () => {
      let stored: string | null = null;
      try {
        stored = window.localStorage.getItem(storageKey);
      } catch {
        // Storage blocked (private mode, site data disabled): the default applies
      }
      return values.find((value) => value === stored) ?? fallback;
    },
    write: (value) => {
      try {
        if (value === fallback) window.localStorage.removeItem(storageKey);
        else window.localStorage.setItem(storageKey, value);
      } catch {
        // Storage blocked: the change lasts until the page is left
      }
      notifyLocal(storageKey);
    },
    subscribe: (listener) => {
      bindStorageEvents();
      let set = localListeners.get(storageKey);
      if (!set) {
        set = new Set();
        localListeners.set(storageKey, set);
      }
      set.add(listener);
      return () => {
        set.delete(listener);
      };
    },
  };
}

function isSupportedLanguage(value: unknown): value is SupportedLanguage {
  return SUPPORTED_LANGUAGES.includes(value as SupportedLanguage);
}

const languageStore: DeviceOptionStore<SupportedLanguage> = {
  read: () => {
    const current = i18n.language?.split("-")[0];
    return isSupportedLanguage(current) ? current : "zh";
  },
  write: (value) => {
    void i18n.changeLanguage(value);
  },
  subscribe: (listener) => {
    i18n.on("languageChanged", listener);
    return () => i18n.off("languageChanged", listener);
  },
};

const themeStore: DeviceOptionStore<(typeof THEME_PREFERENCES)[number]> = {
  read: getThemePreference,
  write: setThemePreference,
  subscribe: subscribeThemePreference,
};

/** OpenReel reads these keys too: keep the keys and the stored values exactly as they are. */
export const CLIP_DRAG_STORAGE_KEY = "youreel.timeline.clipDrag";
export const CONFIRM_DELETE_TRACK_STORAGE_KEY = "youreel.confirm.deleteTrack";
/** Off is stored as "off"; on (the default) as the key absent. */
export const EDITOR_OWN_TAB_STORAGE_KEY = "youreel.editor.ownTab";

// ---------- registry ----------

export const USER_OPTIONS = {
  language: deviceOption({
    group: "display",
    labelKey: "dashboard:settings_interface_language",
    choices: SUPPORTED_LANGUAGES.map((value) => ({ value, label: LANGUAGE_DISPLAY_LABELS[value] })),
    default: "zh",
    device: languageStore,
  }),
  theme: deviceOption({
    group: "display",
    labelKey: "dashboard:settings_theme",
    choices: THEME_PREFERENCES.map((value) => ({ value, labelKey: `dashboard:settings_theme_${value}` })),
    default: "system",
    device: themeStore,
  }),
  mode: studioOption("mode", {
    group: "workflow",
    labelKey: "dashboard:options_mode",
    descriptionKey: "dashboard:options_mode_desc",
    choices: [
      { value: "easy", labelKey: "dashboard:options_mode_easy" },
      { value: "advanced", labelKey: "dashboard:options_mode_advanced" },
    ],
    default: "advanced",
  }),
  default_editor: studioOption("default_editor", {
    group: "workflow",
    labelKey: "dashboard:options_default_editor",
    descriptionKey: "dashboard:options_default_editor_desc",
    choices: [
      { value: "arcreel", labelKey: "dashboard:options_default_editor_arcreel" },
      { value: "openreel", label: EDITOR_BRAND, requires: "openreel" },
    ],
    // YouReel (owner 2026-11-16): OpenReel is every member's editor; the built-in one is the fallback a member can
    // choose here, and what opens wherever OpenReel isn't served (useDefaultEditor)
    default: "openreel",
  }),
  editor_own_tab: deviceToggle(EDITOR_OWN_TAB_STORAGE_KEY, {
    group: "workflow",
    labelKey: "dashboard:options_editor_own_tab",
    descriptionKey: "dashboard:options_editor_own_tab_desc",
    default: "on",
  }),
  timeline_clip_drag: deviceOption({
    group: "openreel",
    labelKey: "dashboard:options_clip_drag",
    descriptionKey: "dashboard:options_clip_drag_desc",
    choices: [
      { value: "pan", labelKey: "dashboard:options_clip_drag_pan" },
      { value: "move", labelKey: "dashboard:options_clip_drag_move" },
    ],
    default: "pan",
    requires: "openreel",
    device: localStorageChoice(CLIP_DRAG_STORAGE_KEY, ["pan", "move"], "pan"),
  }),
  // YouReel (owner 2026-11-18): zooming the OpenReel timeline (− / +, slider, keys) re-centres it on the playhead;
  // off, the view stays where it is (Shift+C still centres). OpenReel reads the same key (Timeline.tsx).
  timeline_auto_recenter: deviceToggle(TIMELINE_RECENTER_STORAGE_KEY, {
    group: "openreel",
    labelKey: "dashboard:options_timeline_recenter",
    descriptionKey: "dashboard:options_timeline_recenter_desc",
    default: "on",
    requires: "openreel",
  }),
  confirm_delete_track: deviceOption({
    group: "openreel",
    labelKey: "dashboard:options_confirm_delete_track",
    choices: [
      { value: "ask", labelKey: "dashboard:options_confirm_delete_track_ask" },
      { value: "off", labelKey: "dashboard:options_confirm_delete_track_off" },
    ],
    default: "ask",
    requires: "openreel",
    device: localStorageChoice(CONFIRM_DELETE_TRACK_STORAGE_KEY, ["ask", "off"], "ask"),
  }),
} satisfies Record<string, UserOptionDef>;

export type UserOptionId = keyof typeof USER_OPTIONS;
export type UserOptionValue<I extends UserOptionId> = (typeof USER_OPTIONS)[I]["default"];
export const USER_OPTION_IDS = Object.keys(USER_OPTIONS) as UserOptionId[];

// ---------- studio storage ----------

interface StudioOptionsState {
  /** Options the member has set; a missing key means the registry default. */
  values: StudioUserOptions;
  /** `failed` keeps the defaults; the next consumer that mounts asks again. */
  status: "idle" | "loading" | "ready" | "failed";
  load: () => Promise<void>;
  save: <K extends keyof StudioUserOptions>(key: K, value: NonNullable<StudioUserOptions[K]>) => Promise<void>;
}

/**
 * YouReel (owner 2026-11-18): the Easy / Advanced mode mirrored into this browser's storage, which the OpenReel editor
 * (same origin) reads for its first paint and watches (the storage event), so an editor tab open beside the studio
 * follows a change at once; the studio's database stays the truth (the editor also asks it).
 */
export const STUDIO_MODE_MIRROR_KEY = "youreel.studioMode";
function mirrorStudioMode(values: StudioUserOptions): void {
  try {
    localStorage.setItem(STUDIO_MODE_MIRROR_KEY, values.mode === "easy" ? "easy" : "advanced");
  } catch {
    // storage blocked: the editor asks the studio instead
  }
}

export const useStudioOptionsStore = create<StudioOptionsState>((set, get) => ({
  values: {},
  status: "idle",

  load: async () => {
    const { status } = get();
    if (status === "loading" || status === "ready") return;
    set({ status: "loading" });
    try {
      const stored = await API.getUserOptions();
      // A change made while the request was in flight wins over the older stored value
      set((state) => ({ values: { ...stored, ...state.values }, status: "ready" }));
      mirrorStudioMode(get().values);
    } catch (err) {
      console.warn("[user-options] load failed; using defaults", err);
      set({ status: "failed" });
    }
  },

  save: async (key, value) => {
    const previous = get().values[key];
    set((state) => ({ values: { ...state.values, [key]: value } }));
    try {
      await API.updateUserOptions({ [key]: value });
      mirrorStudioMode(get().values);
    } catch (err) {
      console.warn("[user-options] save failed", err);
      // Roll back only if no newer change to the same option came in meanwhile
      if (get().values[key] === value) {
        set((state) => {
          const values = { ...state.values };
          if (previous === undefined) delete values[key];
          else values[key] = previous;
          return { values };
        });
      }
      useAppStore.getState().pushToast(i18n.t("dashboard:options_save_failed"), "error");
    }
  },
}));

// ---------- hooks ----------

const noSubscribe = () => () => {};
const noRead = () => undefined;

/** Current value of an option and its setter. Studio options show their default until the server has answered. */
export function useUserOption<I extends UserOptionId>(
  id: I,
): readonly [UserOptionValue<I>, (value: UserOptionValue<I>) => void] {
  const def: UserOptionDef = USER_OPTIONS[id];
  const device = def.storage === "device" ? def.device : null;
  const studioKey = def.storage === "studio" ? def.key : null;

  const deviceValue = useSyncExternalStore(device?.subscribe ?? noSubscribe, device?.read ?? noRead);
  const studioValue = useStudioOptionsStore((s) => (studioKey ? s.values[studioKey] : undefined));
  const load = useStudioOptionsStore((s) => s.load);
  const save = useStudioOptionsStore((s) => s.save);

  useEffect(() => {
    if (studioKey) void load();
  }, [studioKey, load]);

  const setValue = useCallback(
    (value: UserOptionValue<I>) => {
      if (device) device.write(value);
      // The registry types the value per studio key; the store's generic cannot narrow `studioKey`
      else if (studioKey) void save(studioKey, value as never);
    },
    [device, studioKey, save],
  );

  const raw = deviceValue ?? studioValue;
  const value = def.choices.some((choice) => choice.value === raw) ? raw : def.default;
  return [value as UserOptionValue<I>, setValue] as const;
}

/** What the studio serves for options that need it; null while it is still being found out. */
export function useUserOptionRequirements(): Record<UserOptionRequirement, boolean | null> {
  return { openreel: useOpenReelAvailable() };
}

/**
 * The editor the Edit route opens. Falls back to ArcReel's own while OpenReel isn't served; `ready` turns true once
 * both the studio options and the OpenReel probe have answered (the editor won't change after that on its own).
 */
export function useDefaultEditor(): { editor: UserOptionValue<"default_editor">; ready: boolean } {
  const [chosen] = useUserOption("default_editor");
  const openreel = useOpenReelAvailable();
  const status = useStudioOptionsStore((s) => s.status);
  const loaded = status === "ready" || status === "failed";
  const editor = chosen === "openreel" && openreel ? "openreel" : "arcreel";
  return { editor, ready: loaded && (chosen !== "openreel" || openreel !== null) };
}
