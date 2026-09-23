"use client";

import type { ReactNode } from "react";
import { Construction } from "lucide-react";
import { useConsole } from "./console-context";
import { PageHead, PermissionDenied } from "./ui";
import type { Permission } from "@/lib/contracts";

/** A section that exists in the navigation now and is built in a later SaaS phase. */
export function SectionPlaceholder({ title, description, permission, phase, children }: {
  title: string; description: string; permission: Permission; phase: string; children?: ReactNode;
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
        <h3>Planned for {phase}</h3>
        <p>This prototype shows where it lives in the console. It has no data yet and calls nothing.</p>
      </div>
    </>
  );
}
