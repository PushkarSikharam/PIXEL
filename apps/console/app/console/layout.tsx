"use client";

import type { ReactNode } from "react";
import { ConsoleProvider } from "@/components/console-context";
import { Shell } from "@/components/shell";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  return (
    <ConsoleProvider>
      <Shell>{children}</Shell>
    </ConsoleProvider>
  );
}
