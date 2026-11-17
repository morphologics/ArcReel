import { Tabs as TabsPrimitive } from "@base-ui/react/tabs"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "cn"

function Tabs({
  className,
  orientation = "horizontal",
  ...props
}: TabsPrimitive.Root.Props) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      // passed on as well: the arrow keys follow it (up / down in a vertical list)
      orientation={orientation}
      data-orientation={orientation}
      className={cn(
        "group/tabs flex gap-2 data-horizontal:flex-col",
        className
      )}
      {...props}
    />
  )
}

const tabsListVariants = cva(
  "group/tabs-list inline-flex w-fit items-center justify-center rounded-lg p-[3px] text-muted-foreground group-data-horizontal/tabs:h-8 group-data-vertical/tabs:h-fit group-data-vertical/tabs:flex-col data-[variant=line]:rounded-none",
  {
    variants: {
      variant: {
        default: "bg-muted",
        line: "gap-1 bg-transparent",
        // YouReel: tabs drawn as coloured buttons (TabsTrigger `tone`), Button size sm's height
        buttons: "gap-1.5 bg-transparent px-1",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function TabsList({
  className,
  variant = "default",
  ...props
}: TabsPrimitive.List.Props & VariantProps<typeof tabsListVariants>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      data-variant={variant}
      className={cn(tabsListVariants({ variant }), className)}
      {...props}
    />
  )
}

// Changes from the generated code: unselected item text uses muted-foreground, not foreground/60, reaching 4.5:1 on the muted background in light mode.
// Versus upstream: lower the input fill opacity to keep the light-layer hierarchy of the selected item.
// 相对生成代码的改动：未选中项文字用 muted-foreground，不用 foreground/60，浅色下在 muted 底上达到 4.5:1。
// 相对上游：降低 input 填充不透明度以保持选中项的浅底层级。
// YouReel: colour of a tab in a "buttons" list; the selected one is ringed, the others slightly dimmed until hovered.
// The trailing `!` beats the selected-state colours above, which differ per theme.
const tabsTriggerTones = cva(
  "group-data-[variant=buttons]/tabs-list:h-7 group-data-[variant=buttons]/tabs-list:flex-none group-data-[variant=buttons]/tabs-list:px-3 group-data-[variant=buttons]/tabs-list:opacity-75 group-data-[variant=buttons]/tabs-list:shadow-none! group-data-[variant=buttons]/tabs-list:hover:opacity-100 group-data-[variant=buttons]/tabs-list:data-active:opacity-100 group-data-[variant=buttons]/tabs-list:data-active:ring-2 group-data-[variant=buttons]/tabs-list:data-active:ring-foreground group-data-[variant=buttons]/tabs-list:data-active:ring-offset-1 group-data-[variant=buttons]/tabs-list:data-active:ring-offset-background",
  {
    variants: {
      tone: {
        neutral: "group-data-[variant=buttons]/tabs-list:bg-muted! group-data-[variant=buttons]/tabs-list:text-foreground!",
        plan: "bg-view-plan! text-view-plan-foreground!",
        board: "border-border! bg-view-board! text-view-board-foreground!",
        edit: "bg-view-edit! text-view-edit-foreground!",
      },
    },
    defaultVariants: { tone: "neutral" },
  }
)

function TabsTrigger({
  className,
  tone,
  ...props
}: TabsPrimitive.Tab.Props & VariantProps<typeof tabsTriggerTones>) {
  return (
    <TabsPrimitive.Tab
      data-slot="tabs-trigger"
      className={cn(
        "relative inline-flex h-[calc(100%-1px)] flex-1 items-center justify-center gap-1.5 rounded-md border border-transparent px-1.5 py-0.5 text-sm font-medium whitespace-nowrap text-muted-foreground transition-all group-data-vertical/tabs:w-full group-data-vertical/tabs:justify-start hover:text-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-1 focus-visible:outline-ring disabled:pointer-events-none disabled:opacity-50 has-data-[icon=inline-end]:pr-1 has-data-[icon=inline-start]:pl-1 aria-disabled:pointer-events-none aria-disabled:opacity-50 dark:hover:text-foreground group-data-[variant=default]/tabs-list:data-active:shadow-sm group-data-[variant=line]/tabs-list:data-active:shadow-none [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        "group-data-[variant=line]/tabs-list:bg-transparent group-data-[variant=line]/tabs-list:data-active:bg-transparent dark:group-data-[variant=line]/tabs-list:data-active:border-transparent dark:group-data-[variant=line]/tabs-list:data-active:bg-transparent",
        "data-active:bg-background data-active:text-foreground dark:data-active:border-input dark:data-active:bg-input/12 dark:data-active:text-foreground",
        tabsTriggerTones({ tone }),
        "after:absolute after:bg-foreground after:opacity-0 after:transition-opacity group-data-horizontal/tabs:after:inset-x-0 group-data-horizontal/tabs:after:bottom-[-5px] group-data-horizontal/tabs:after:h-0.5 group-data-vertical/tabs:after:inset-y-0 group-data-vertical/tabs:after:-right-1 group-data-vertical/tabs:after:w-0.5 group-data-[variant=line]/tabs-list:data-active:after:opacity-100",
        className
      )}
      {...props}
    />
  )
}

function TabsContent({ className, ...props }: TabsPrimitive.Panel.Props) {
  return (
    <TabsPrimitive.Panel
      data-slot="tabs-content"
      className={cn("flex-1 text-sm outline-none", className)}
      {...props}
    />
  )
}

export { Tabs, TabsList, TabsTrigger, TabsContent }
