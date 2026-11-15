import type * as React from "react"
import { Popover as PopoverPrimitive } from "@base-ui/react/popover"
import { cn } from "cn"
import { Popover, PopoverContent, PopoverTrigger } from "./popover"

// Explanatory tip (infotip): the trigger exists only to open the explanation, so per Base UI's advice it uses a hover-openable Popover rather than a Tooltip;
// touch tap, keyboard Enter/Space and screen readers can all reach the explanation. If the trigger has another purpose and the explanation is incidental, still use Tooltip.
// 说明性提示（infotip）：触发元素只为打开说明而存在，按 Base UI 的建议用悬停可开的 Popover 而不是 Tooltip，
// 触屏点按、键盘 Enter/Space 与读屏都能取到说明。触发元素另有用途、说明只是附带的，仍用 Tooltip。
function InfoTip({ ...props }: PopoverPrimitive.Root.Props) {
  return <Popover data-slot="info-tip" {...props} />
}

// Renders a native <button type="button"> by default. Looks like a dashed underline or a badge are supplied by the caller via
// render={<button type="button" className="..." />} (precedent: ProjectStatusBar's PopoverTrigger);
// render's className is concatenated with the focus-ring here.
// 默认渲染原生 <button type="button">。虚线下划线、徽标这类外观由调用处经
// render={<button type="button" className="..." />} 给出（先例同 ProjectStatusBar 的 PopoverTrigger），
// render 的 className 与这里的 focus-ring 拼接。
function InfoTipTrigger({
  className,
  ...props
}: Omit<PopoverPrimitive.Trigger.Props, "openOnHover" | "delay" | "closeDelay">) {
  return (
    <PopoverTrigger
      data-slot="info-tip-trigger"
      openOnHover
      // Same as TooltipProvider's delay: opens on hover / 与 TooltipProvider 的 delay 一致：悬停即开
      delay={0}
      className={cn("focus-ring", className)}
      {...props}
    />
  )
}

function InfoTipContent({
  className,
  side = "top",
  title,
  children,
  ...props
}: Omit<React.ComponentProps<typeof PopoverContent>, "title"> & {
  /** Accessible name of the popover, usually the trigger's text; not shown, only announced by screen readers. / 弹层的可访问名称，通常就是触发元素上的文字；不显示，只供读屏播报。 */
  title: React.ReactNode
}) {
  return (
    <PopoverContent
      data-slot="info-tip-content"
      side={side}
      className={cn("w-fit max-w-xs px-3 py-1.5 text-xs", className)}
      {...props}
    >
      <PopoverPrimitive.Title className="sr-only">{title}</PopoverPrimitive.Title>
      {/* The explanation is attached as the popover's aria-describedby, announced together with the popover when it opens / 说明挂成弹层的 aria-describedby，读屏打开时连同弹层一起播报 */}
      <PopoverPrimitive.Description>{children}</PopoverPrimitive.Description>
    </PopoverContent>
  )
}

export { InfoTip, InfoTipContent, InfoTipTrigger }
