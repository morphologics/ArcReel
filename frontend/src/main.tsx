import { createRoot } from "react-dom/client";
import { AppRoutes } from "./router";
import { useAuthStore } from "@/stores/auth-store";
import { i18nReady } from "@/i18n";
import { BRAND, BRAND_DOCUMENT_TITLE } from "@/branding";
import { TooltipProvider } from "@/components/ui/tooltip";
import { initTheme } from "@/theme";
import { exposeStudioNavigate } from "@/components/canvas/edit/open-in-openreel";
import { StudioWakingOverlay } from "@/components/shared/StudioWakingOverlay";
import { startActivitySignal } from "@/api/activity-signal";
import { startClientErrorReporter } from "@/diagnostics/client-error-reporter";

import "@fontsource-variable/inter";
import "@fontsource-variable/jetbrains-mono";
import "@fontsource/instrument-serif/400.css";
import "@fontsource/instrument-serif/400-italic.css";
import "driver.js/dist/driver.css";

import "./index.css";
// Loaded after the driver default skin so the overrides take effect / driver 默认皮肤之后加载，覆盖生效
import "./css/onboarding.css";

// At startup, set the document title and meta description from BRAND (the <title> and
// <meta name="description"> in index.html are only placeholders during loading).
// 启动时按 BRAND 设置文档标题与 meta description（index.html 中的
// <title> 与 <meta name="description"> 仅作为加载阶段的占位）。
document.title = BRAND_DOCUMENT_TITLE;
const metaDescription = document.querySelector<HTMLMetaElement>('meta[name="description"]');
if (metaDescription) {
  metaDescription.content = BRAND.description;
}

initTheme();

// Restore login state from localStorage / 从 localStorage 恢复登录状态
useAuthStore.getState().initialize();

// YouReel: OpenReel's links back ("Open shot … in YouReel Studio") move this tab in-app instead of opening another
exposeStudioNavigate();
// YouReel: tell the host the member is using the studio (its idle stop), on real use, at most once a minute
startActivitySignal();
// YouReel: errors the member hits (error toasts, uncaught exceptions, failed requests) go to the studio's error log
// (Settings → Options → Diagnostics)
startClientErrorReporter();

const root = document.getElementById("app-root");
if (root) {
  // Wait until the namespaces for the current i18n language + fallback are all loaded before rendering, to avoid keys flashing on first paint.
  // The chunks are all local lazy imports; even on a weak network it is only a few tens of ms delay (cold start).
  // An i18n load failure must not block app startup (still render, letting t() fall back to key strings),
  // but the failure must be observable, so log an error explicitly instead of using finally to merge success/failure silently.
  // 等 i18n 当前语言 + fallback 的 namespace 全部加载完再渲染，避免首屏闪 key。
  // chunk 都是本地 lazy import，弱网下也只是几十 ms 延迟（cold start）。
  // i18n 加载失败时不能阻塞应用启动（仍 render，让 t() 退回 key 字符串），
  // 但失败必须可观测——所以显式记 error 而不是用 finally 把成功/失败合流静默。
  const render = () =>
    createRoot(root).render(
      <TooltipProvider>
        <AppRoutes />
        <StudioWakingOverlay />
      </TooltipProvider>,
    );
  i18nReady.then(render, (err) => {
    console.error("i18n initialization failed", err);
    render();
  });
}
