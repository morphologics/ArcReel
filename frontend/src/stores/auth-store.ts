import { create } from "zustand";
import { getToken, setToken as saveToken, clearToken } from "@/utils/auth";

interface AuthState {
  token: string | null;
  username: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  /** The backend didn't answer /auth/status and last time it said the login is off: the app shows "restarting,
   * retrying" and keeps asking, instead of falling back to the login page (a dead end there). */
  unreachable: boolean;
  initialize: () => void;
  login: (token: string, username: string) => void;
  logout: () => void;
  setLoading: (loading: boolean) => void;
}

/** Delays before each /auth/status attempt: the first at once, then retries over about 7 s. */
const AUTH_STATUS_RETRY_MS = [0, 1000, 2000, 4000];
/** After those, while the backend stays unreachable: one more attempt every this many ms. */
const AUTH_STATUS_UNREACHABLE_RETRY_MS = 5000;
/** The backend's last answer to "is the login on?" in this browser ("true" / "false"). */
const AUTH_ENABLED_KEY = "arcreel:auth-enabled";

function lastAuthEnabled(): string | null {
  try {
    return localStorage.getItem(AUTH_ENABLED_KEY);
  } catch {
    return null;
  }
}

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  username: null,
  isAuthenticated: false,
  isLoading: true,
  unreachable: false,

  initialize: () => {
    const token = getToken();
    if (token) {
      set({ token, isAuthenticated: true, isLoading: false });
      return;
    }
    // 无 token 时先问后端是否启用了鉴权。`AUTH_ENABLED=false` 时后端全链路
    // bypass，前端也应该跳过登录页直接进主界面。超时 / 网络异常 / 响应 shape
    // 异常时 fail-closed 退回到登录页，避免误把损坏响应当成"无需鉴权"放行。
    // A failed request is retried a few times first (the backend may be restarting, e.g. a dev auto-reload),
    // so a brief outage doesn't land on the login page.
    const attempt = async (): Promise<boolean> => {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000);
      try {
        const res = await fetch("/api/v1/auth/status", { signal: controller.signal });
        if (!res.ok) throw new Error(`status ${res.status}`);
        const payload: unknown = await res.json();
        if (
          typeof payload !== "object" ||
          payload === null ||
          typeof (payload as { enabled?: unknown }).enabled !== "boolean"
        ) {
          throw new Error("invalid /auth/status payload");
        }
        const enabled = (payload as { enabled: boolean }).enabled;
        try {
          localStorage.setItem(AUTH_ENABLED_KEY, String(enabled));
        } catch {
          // storage blocked: an outage then falls back to the login page, as before
        }
        return !enabled;
      } finally {
        clearTimeout(timeoutId);
      }
    };
    void (async () => {
      // The quick retries; then, if the backend said last time that the login is off, keep asking every
      // AUTH_STATUS_UNREACHABLE_RETRY_MS (shown as "restarting, retrying"); otherwise (login on, or never heard)
      // fall back to the login page as before. Nothing is unlocked while it's unreachable.
      for (let tries = 0; ; tries++) {
        const delay = AUTH_STATUS_RETRY_MS[tries] ?? AUTH_STATUS_UNREACHABLE_RETRY_MS;
        if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
        try {
          if (await attempt()) set({ isAuthenticated: true });
          set({ isLoading: false, unreachable: false });
          return;
        } catch (err) {
          console.warn("[auth] /auth/status fetch failed", err);
          if (tries < AUTH_STATUS_RETRY_MS.length - 1) continue;
          if (lastAuthEnabled() !== "false") {
            console.warn("[auth] /auth/status unavailable; defaulting to login");
            set({ isLoading: false });
            return;
          }
          set({ unreachable: true });
        }
      }
    })();
  },

  login: (token, username) => {
    saveToken(token);
    set({ token, username, isAuthenticated: true, isLoading: false });
  },

  logout: () => {
    clearToken();
    set({ token: null, username: null, isAuthenticated: false });
  },

  setLoading: (isLoading) => set({ isLoading }),
}));
