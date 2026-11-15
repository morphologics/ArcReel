import type { ReactNode } from "react";

interface DetailPaneProps {
  /** Header: name, status and header actions; does not scroll with the body. / 页头：名称、状态与页头动作，不随正文滚动。 */
  header?: ReactNode;
  /** Footer: the persistent save bar (`SaveBar`); does not scroll with the body. / 页脚：常驻的保存栏（`SaveBar`），不随正文滚动。 */
  footer?: ReactNode;
  children: ReactNode;
}

/**
 * Detail pane of the full-bleed master-detail layout, placed beside `SecondaryRail` in the section root (`flex min-h-0 flex-1`).
 * Three parts, header, body and footer: only the body scrolls, and the save bar is fixed at the bottom of the detail pane, outside the scroll area.
 * The body has no padding or width cap; the caller decides by content (forms are usually `max-w-190 px-6 py-6`);
 * when the body is width-capped, the footer content is capped too (`max-w-178`) so the save bar aligns with the form column instead of spanning the pane.
 * When the selection changes, the caller gives the detail component a `key` to rebuild the whole pane, so the previous item's unsaved changes, in-flight requests and loading state do not carry over.
 *
 * 全出血主从布局的详情栏，与 `SecondaryRail` 并排放在区段根节点（`flex min-h-0 flex-1`）里。
 * 分页头、正文与页脚三段：只有正文滚动，保存栏固定在详情栏底部、滚动区之外。
 * 正文不加内边距与宽度上限，由调用方按内容决定（表单通常是 `max-w-190 px-6 py-6`）；
 * 正文限宽时，页脚内容同样限宽（`max-w-178`），保存栏与表单列对齐，不横跨整栏。
 * 选中项变化时，调用方给详情组件传 `key` 整栏重建，上一项的未保存修改、在途请求与加载状态不带到下一项。
 */
export function DetailPane({ header, footer, children }: DetailPaneProps) {
  return (
    <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
      {header && <div className="shrink-0 border-b border-border px-6 py-4">{header}</div>}
      <div className="relative min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">{children}</div>
      {footer && <div className="shrink-0 border-t border-border bg-card px-6 py-3">{footer}</div>}
    </div>
  );
}
