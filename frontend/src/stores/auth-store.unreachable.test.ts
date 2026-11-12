import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "./auth-store";

// The backend doesn't answer /auth/status: what the app does after the quick retries depends on the backend's
// last answer in this browser (login off → keep retrying, shown as "restarting"; otherwise → login page).
describe("auth store when the backend is unreachable", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    useAuthStore.setState({ token: null, isAuthenticated: false, isLoading: true, unreachable: false });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("keeps retrying, without the login page, when the login was off last time", async () => {
    localStorage.setItem("arcreel:auth-enabled", "false");
    useAuthStore.getState().initialize();
    await vi.advanceTimersByTimeAsync(8000);
    expect(useAuthStore.getState()).toMatchObject({ isLoading: true, unreachable: true });

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ enabled: false }))));
    await vi.advanceTimersByTimeAsync(5000);
    expect(useAuthStore.getState()).toMatchObject({ isLoading: false, unreachable: false, isAuthenticated: true });
  });

  it("falls back to the login page when the login was on or never heard of", async () => {
    useAuthStore.getState().initialize();
    await vi.advanceTimersByTimeAsync(8000);
    expect(useAuthStore.getState()).toMatchObject({ isLoading: false, unreachable: false, isAuthenticated: false });
  });
});
