import { useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ContextMenu } from "@base-ui/react/context-menu";
import { Box, UserRound } from "lucide-react";
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu";
import { useProjectsStore } from "@/stores/projects-store";

/** The mention a prompt writes for an asset: `@[Name]`, a character's derivative `@[Character/Derivative]`. */
export function assetMention(name: string, derivative?: string): string {
  return derivative ? `@[${name}/${derivative}]` : `@[${name}]`;
}

/** `value` with `insert` put in place of the selection [start, end), and the caret just after it. */
export function insertAtSelection(
  value: string,
  insert: string,
  start: number,
  end: number,
): { value: string; caret: number } {
  const from = Math.max(0, Math.min(start, value.length));
  const to = Math.max(from, Math.min(end, value.length));
  return { value: value.slice(0, from) + insert + value.slice(to), caret: from + insert.length };
}

/**
 * Right-click menu on a prompt text box: Insert character › (a character with derivatives opens a submenu of
 * them) and Insert prop ›; the chosen asset's `@[name]` goes in at the cursor (replacing a selection). Shift +
 * right-click keeps the browser's own menu (copy, paste, spelling). Names are listed in ascending order; a space
 * follows the inserted mention.
 */
export function AssetMentionMenu({
  value,
  onChange,
  disabled,
  children,
}: {
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
  /** The text box (a textarea inside). */
  children: ReactNode;
}) {
  const { t } = useTranslation("dashboard");
  const wrapRef = useRef<HTMLDivElement>(null);
  // where the cursor was when the menu opened (choosing an item moves the focus away from the text box)
  const selection = useRef<[number, number]>([value.length, value.length]);
  const characters = useProjectsStore((s) => s.currentProjectData?.characters);
  const props = useProjectsStore((s) => s.currentProjectData?.props);
  if (disabled) return <>{children}</>;

  const textarea = () => wrapRef.current?.querySelector("textarea") ?? null;
  const insert = (mention: string) => {
    const [start, end] = selection.current;
    // a space after the tag, ready for the next word
    const next = insertAtSelection(value, `${mention} `, start, end);
    onChange(next.value);
    // back in the text box with the cursor after the mention, once the new value has rendered
    window.requestAnimationFrame(() => {
      const box = textarea();
      if (!box) return;
      box.focus();
      box.setSelectionRange(next.caret, next.caret);
    });
  };

  // listed in ascending order (in the project's language)
  const byName = (a: string, b: string) => a.localeCompare(b);
  const characterNames = Object.keys(characters ?? {}).sort(byName);
  const propNames = Object.keys(props ?? {}).sort(byName);
  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger
        ref={wrapRef}
        render={<div />}
        onContextMenuCapture={(event) => {
          // Shift + right-click: the browser's menu, not ours
          if (event.shiftKey) {
            event.stopPropagation();
            return;
          }
          const box = textarea();
          if (box) selection.current = [box.selectionStart, box.selectionEnd];
        }}
      >
        {children}
      </ContextMenu.Trigger>
      <DropdownMenuContent className="w-auto min-w-44">
        <DropdownMenuSub>
          <DropdownMenuSubTrigger disabled={characterNames.length === 0}>
            <UserRound aria-hidden />
            {t("prompt_insert_character")}
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent className="relative max-h-[70vh] min-w-44 overflow-y-auto">
            {characterNames.map((name) => {
              const derivatives = Object.keys(characters?.[name]?.derivatives ?? {}).sort(byName);
              return derivatives.length === 0 ? (
                <DropdownMenuItem key={name} onClick={() => insert(assetMention(name))}>
                  {name}
                </DropdownMenuItem>
              ) : (
                <DropdownMenuSub key={name}>
                  <DropdownMenuSubTrigger>{name}</DropdownMenuSubTrigger>
                  <DropdownMenuSubContent className="relative max-h-[70vh] min-w-44 overflow-y-auto">
                    <DropdownMenuItem onClick={() => insert(assetMention(name))}>{name}</DropdownMenuItem>
                    <DropdownMenuSeparator />
                    {derivatives.map((derivative) => (
                      <DropdownMenuItem key={derivative} onClick={() => insert(assetMention(name, derivative))}>
                        {derivative}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuSubContent>
                </DropdownMenuSub>
              );
            })}
          </DropdownMenuSubContent>
        </DropdownMenuSub>
        <DropdownMenuSub>
          <DropdownMenuSubTrigger disabled={propNames.length === 0}>
            <Box aria-hidden />
            {t("prompt_insert_prop")}
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent className="relative max-h-[70vh] min-w-44 overflow-y-auto">
            {propNames.map((name) => (
              <DropdownMenuItem key={name} onClick={() => insert(assetMention(name))}>
                {name}
              </DropdownMenuItem>
            ))}
          </DropdownMenuSubContent>
        </DropdownMenuSub>
      </DropdownMenuContent>
    </ContextMenu.Root>
  );
}
