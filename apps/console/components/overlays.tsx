"use client";

import * as RadixDialog from "@radix-ui/react-dialog";
import * as RadixMenu from "@radix-ui/react-dropdown-menu";
import type { ReactNode } from "react";

/** Accessible dialog: focus is trapped, Escape closes, and focus returns to the trigger. */
export function Dialog({ open, onOpenChange, title, description, children, actions }: {
  open: boolean; onOpenChange: (open: boolean) => void; title: string; description?: string;
  children?: ReactNode; actions: ReactNode;
}) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="px-overlay" />
        <RadixDialog.Content className="px-dialog" aria-describedby={description ? undefined : undefined}>
          <RadixDialog.Title asChild><h2 style={{ fontSize: "var(--px-text-lg)" }}>{title}</h2></RadixDialog.Title>
          {description ? <RadixDialog.Description className="px-muted">{description}</RadixDialog.Description> : <RadixDialog.Description className="px-sr-only">{title}</RadixDialog.Description>}
          {children}
          <div className="px-dialog-actions">{actions}</div>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}

export const DialogClose = RadixDialog.Close;

export interface MenuEntry { key: string; label: ReactNode; onSelect?: () => void; disabled?: boolean; hint?: ReactNode }

/** Accessible menu with arrow-key navigation and type-ahead. */
export function Menu({ trigger, label, sections, align = "start" }: {
  trigger: ReactNode; label?: string; sections: Array<{ label?: string; items: MenuEntry[] }>; align?: "start" | "end";
}) {
  return (
    <RadixMenu.Root>
      <RadixMenu.Trigger asChild>{trigger}</RadixMenu.Trigger>
      <RadixMenu.Portal>
        <RadixMenu.Content className="px-menu" align={align} sideOffset={4} aria-label={label}>
          {sections.map((section, index) => (
            <RadixMenu.Group key={section.label ?? index}>
              {index > 0 ? <RadixMenu.Separator className="px-menu-sep" /> : null}
              {section.label ? <RadixMenu.Label className="px-menu-label">{section.label}</RadixMenu.Label> : null}
              {section.items.map((item) => (
                <RadixMenu.Item key={item.key} className="px-menu-item" disabled={item.disabled}
                  onSelect={() => item.onSelect?.()}>
                  <span style={{ flex: 1 }}>{item.label}</span>{item.hint}
                </RadixMenu.Item>
              ))}
            </RadixMenu.Group>
          ))}
        </RadixMenu.Content>
      </RadixMenu.Portal>
    </RadixMenu.Root>
  );
}
