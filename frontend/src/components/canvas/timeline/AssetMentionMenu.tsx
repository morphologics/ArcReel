import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ContextMenu } from "@base-ui/react/context-menu";
import { Box, ClipboardPaste, Copy, TextCursor, UserRound } from "lucide-react";
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu";
import { useProjectsStore } from "@/stores/projects-store";
import { isApplePlatform } from "@/preferences/hotkeys";

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
 *
 * YouReel: Copy, Paste and Select All on top, as the browser's own menu has them (this menu replaces it, so they were
 * only reachable with Shift + right-click or the keyboard). Paste reads the clipboard, which the browser may ask to
 * allow; refused, the text box gets the focus back so Ctrl / ⌘ + V still works.
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
  // YouReel: whether text was selected when the menu opened (Copy is offered only then); state, so the menu re-renders
  const [hasSelection, setHasSelection] = useState(false);
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

  const mod = isApplePlatform() ? "⌘" : "Ctrl+";
  const copySelection = () => {
    const [start, end] = selection.current;
    void navigator.clipboard?.writeText(value.slice(start, end)).catch(() => {});
    window.requestAnimationFrame(() => {
      const box = textarea();
      box?.focus();
      box?.setSelectionRange(start, end);
    });
  };
  const pasteClipboard = () => {
    const [start, end] = selection.current;
    const backToBox = (caret: number) => window.requestAnimationFrame(() => {
      const box = textarea();
      box?.focus();
      box?.setSelectionRange(caret, caret);
    });
    if (!navigator.clipboard?.readText) { backToBox(end); return; }
    navigator.clipboard.readText().then((text) => {
      const next = insertAtSelection(value, text, start, end);
      onChange(next.value);
      backToBox(next.caret);
    }).catch(() => backToBox(end));
  };
  const selectAll = () => window.requestAnimationFrame(() => {
    const box = textarea();
    box?.focus();
    box?.select();
  });

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
          setHasSelection(Boolean(box && box.selectionEnd > box.selectionStart));
        }}
      >
        {children}
      </ContextMenu.Trigger>
      <DropdownMenuContent className="w-auto min-w-44">
        <DropdownMenuItem disabled={!hasSelection} onClick={copySelection}>
          <Copy aria-hidden />
          {t("prompt_copy")}
          <DropdownMenuShortcut>{mod}C</DropdownMenuShortcut>
        </DropdownMenuItem>
        <DropdownMenuItem onClick={pasteClipboard}>
          <ClipboardPaste aria-hidden />
          {t("prompt_paste")}
          <DropdownMenuShortcut>{mod}V</DropdownMenuShortcut>
        </DropdownMenuItem>
        <DropdownMenuItem onClick={selectAll}>
          <TextCursor aria-hidden />
          {t("prompt_select_all")}
          <DropdownMenuShortcut>{mod}A</DropdownMenuShortcut>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
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
