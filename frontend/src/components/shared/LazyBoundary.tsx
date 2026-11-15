import { Component, Suspense, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useLocation } from "wouter";
import { CircleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/shared/LoadingState";

type LazyBoundaryVariant = "screen" | "pane" | "none";

/**
 * Outer boundary for lazy-loaded regions (`lazyNamed` components): shows a loading placeholder while the chunk hasn't arrived, and when loading finally fails
 * shows a failure notice and "Reload", so the error doesn't unmount the whole app root.
 *
 * - `screen`: fills the viewport, for whole-page routes.
 * - `pane`: fills the parent container, for local regions such as the workspace canvas; header and view switching stay usable.
 * - `none`: renders nothing for either loading or failure, for components that produce no DOM (such as onboarding).
 *
 * React.lazy caches the failure, and retrying in place can't get a new chunk, so after failure only a full page reload is offered;
 * the failure state is cleared on route change, so other pages show normally after the user leaves the failed page.
 *
 * 懒加载区域（`lazyNamed` 组件）的外层边界：chunk 未到达时显示加载占位，最终加载失败时
 * 显示失败提示与「重新加载」，不让错误卸载整个应用根节点。
 *
 * - `screen`：占满视口，用于整页路由。
 * - `pane`：占满父容器，用于工作区画布等局部区域，页头与视图切换保持可用。
 * - `none`：加载与失败都不渲染内容，用于不产出 DOM 的组件（如引导）。
 *
 * React.lazy 会缓存失败结果，原地重试拿不到新 chunk，因此失败后只提供整页刷新；
 * 路由变化时清除失败状态，用户离开失败页面后其他页面照常显示。
 */
export function LazyBoundary({ variant, children }: { variant: LazyBoundaryVariant; children: ReactNode }) {
  const [location] = useLocation();
  return (
    <LoadErrorBoundary variant={variant} resetKey={location}>
      <Suspense fallback={variant === "none" ? null : <LoadingState variant={variant} />}>{children}</Suspense>
    </LoadErrorBoundary>
  );
}

interface LoadErrorBoundaryProps {
  variant: LazyBoundaryVariant;
  resetKey: string;
  children: ReactNode;
}

interface LoadErrorBoundaryState {
  failed: boolean;
  resetKey: string;
}

class LoadErrorBoundary extends Component<LoadErrorBoundaryProps, LoadErrorBoundaryState> {
  state: LoadErrorBoundaryState = { failed: false, resetKey: this.props.resetKey };

  static getDerivedStateFromError(): Partial<LoadErrorBoundaryState> {
    return { failed: true };
  }

  static getDerivedStateFromProps(
    props: LoadErrorBoundaryProps,
    state: LoadErrorBoundaryState,
  ): Partial<LoadErrorBoundaryState> | null {
    return props.resetKey === state.resetKey ? null : { failed: false, resetKey: props.resetKey };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    if (this.props.variant === "none") return null;
    return <LoadFailedState variant={this.props.variant} />;
  }
}

function LoadFailedState({ variant }: { variant: "screen" | "pane" }) {
  const { t } = useTranslation("common");
  return (
    <div
      role="alert"
      className={
        variant === "screen"
          ? "flex h-dvh flex-col items-center justify-center gap-3 bg-background text-sm text-muted-foreground"
          : "flex h-full flex-col items-center justify-center gap-3 text-sm text-muted-foreground"
      }
    >
      <p className="flex items-center gap-2">
        <CircleAlert aria-hidden className="size-4" />
        {t("page_load_failed")}
      </p>
      <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
        {t("reload_page")}
      </Button>
    </div>
  );
}
