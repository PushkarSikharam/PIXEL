"use client";

import type { ReactNode } from "react";
import { Construction } from "lucide-react";
import { useConsole } from "./console-context";
import { PageHead, PermissionDenied } from "./ui";
import type { Permission } from "@pixel-console/lib/contracts";

/**
 * A part of the console that is designed but not built.
 *
 * It says so in the words somebody using Pixel would use. Which internal step of ours it is
 * waiting on is our business, not theirs: a customer reading "Planned for SaaS Phase 8" learns
 * nothing except that we talk about them in release numbers.
 */
export function SectionPlaceholder({ title, description, permission, children }: {
  title: string; description: string; permission: Permission; phase?: string; children?: ReactNode;
}) {
  const c = useConsole();
  const scoped = c.productId ? { productId: c.productId } : {};
  if (!c.can(permission, scoped)) return <><PageHead title={title} /><PermissionDenied what={title.toLowerCase()} /></>;
  return (
    <>
      <PageHead title={title} description={description} />
      {children}
      <div className="px-state">
        <Construction aria-hidden />
        <h3>Not built yet</h3>
        <p>This shows where {title.toLowerCase()} will live. Nothing here is connected to your
          workspace, so nothing you see is yours.</p>
      </div>
    </>
  );
}
