"use client";

import type { ReactNode } from "react";
import { ConsoleProvider } from "@pixel-console/components/console-context";
import { Shell } from "@pixel-console/components/shell";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  return (
    <ConsoleProvider>
      <Shell>{children}</Shell>
    </ConsoleProvider>
  );
}
