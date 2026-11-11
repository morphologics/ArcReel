import { create } from "zustand";
import { getToken, setToken as saveToken, clearToken } from "@/utils/auth";

interface AuthState {
  token: string | null;
  username: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  initialize: () => void;
  login: (token: string, username: string) => void;
  logout: () => void;
  setLoading: (loading: boolean) => void;
}

/** Delays before each /auth/status attempt: the first at once, then retries over about 7 s. */
const AUTH_STATUS_RETRY_MS = [0, 1000, 2000, 4000];

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  username: null,
  isAuthenticated: false,
  isLoading: true,

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
        return !(payload as { enabled: boolean }).enabled;
      } finally {
        clearTimeout(timeoutId);
      }
    };
    void (async () => {
      for (const delay of AUTH_STATUS_RETRY_MS) {
        if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
        try {
          if (await attempt()) set({ isAuthenticated: true });
          set({ isLoading: false });
          return;
        } catch (err) {
          console.warn("[auth] /auth/status fetch failed", err);
        }
      }
      console.warn("[auth] /auth/status unavailable; defaulting to login");
      set({ isLoading: false });
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
