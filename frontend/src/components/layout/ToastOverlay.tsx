import { useEffect } from "react";
import { Toast as ToastPrimitive } from "@base-ui/react/toast";
import { Toaster } from "@/components/ui/toast";
import { useAppStore, type Toast } from "@/stores/app-store";

// How long a toast stays; the timer pauses while the pointer hovers or keyboard focus is on the toast. / 提示的停留时长；指针悬停或键盘聚焦在提示上时暂停计时。
const AUTO_DISMISS_MS = 5000;

// The toast queue lives only here: other code sends toasts via useAppStore's pushToast / 提示队列只在这里：其余代码经 useAppStore 的 pushToast 发提示
const toastQueue = ToastPrimitive.createToastManager();

function enqueue({ id, text, tone, action }: Toast) {
  toastQueue.add({
    id,
    title: text,
    type: tone,
    priority: tone === "error" ? "high" : "low",
    actionProps: action
      ? {
          children: action.label,
          onClick: () => {
            action.onClick();
            toastQueue.close(id);
          },
        }
      : undefined,
  });
}

/**
 * Global toasts: hand each newly sent toast in app-store to the toast queue for display.
 * Subscribe to the store directly instead of reading during render: several sent in the same batch would merge into the last one before render, while the subscription receives each write.
 * The queue shows at most 3 at once and earlier toasts are pushed out; error toasts are announced immediately by screen readers.
 *
 * 全局提示：把 app-store 里每条新发出的 toast 转交提示队列显示。
 * 直接订阅 store 而不经渲染读取：同一批次里连发的几条在渲染前会合并成最后一条，订阅按写入逐条收到。
 * 队列同时最多显示 3 条，更早的提示被挤出；错误提示由读屏立即播报。
 */
export function ToastOverlay() {
  useEffect(() => {
    // The most recent toast sent before mount is shown as usual / 挂载前已发出的最近一条照常显示
    const current = useAppStore.getState().toast;
    if (current) enqueue(current);
    return useAppStore.subscribe((state, prev) => {
      if (state.toast && state.toast !== prev.toast) enqueue(state.toast);
    });
  }, []);

  return <Toaster toastManager={toastQueue} timeout={AUTO_DISMISS_MS} />;
}
