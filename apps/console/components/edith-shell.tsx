"use client";

/**
 * Edith in the shell, so she is on every authenticated screen.
 *
 * Which product answers depends on where the person is. Inside a product, that product answers
 * and can act on its screens. Anywhere else, the product that describes Pixel itself answers, so
 * "take me to my products" works from the overview as readily as from inside something.
 *
 * The panel is keyed by the product it is for. Opening another product therefore unmounts this
 * one and mounts a new one: the in-flight request is aborted, audio stops, and no message,
 * pending question or execution key from the product just left can reach the new one.
 */

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { useConsole } from "./console-context";
import { EdithPanel } from "./edith";
import { productShape, storedSession, type ApiProductShape, type ApiSession } from "@pixel-console/lib/pixel-api";
import { viewForRoute } from "@pixel-console/lib/console-routes";

export function Edith() {
  const c = useConsole();
  const pathname = usePathname();
  const surface = c.productSurface;
  const consoleProductId = c.account?.console_product_id ?? null;
  const [session, setSession] = useState<ApiSession | null>(null);
  const [consoleShape, setConsoleShape] = useState<ApiProductShape | null>(null);

  useEffect(() => { setSession(storedSession()); }, [c.account]);

  useEffect(() => {
    setConsoleShape(null);
    if (!consoleProductId || !session) return;
    let cancelled = false;
    productShape(session, consoleProductId)
      .then((shape) => { if (!cancelled) setConsoleShape(shape); })
      // Without it she simply is not there, which is better than a panel that answers nothing.
      .catch(() => { if (!cancelled) setConsoleShape(null); });
    return () => { cancelled = true; };
  }, [consoleProductId, session]);

  if (!c.live || !session || !c.account) return null;
  if (surface) {
    return (
      <EdithPanel key={`product:${surface.productId}`} session={session} shape={surface.shape}
        productId={surface.productId} currentPage={surface.currentPage}
        selectedRecordId={surface.selectedRecordId} scope="product" onRecordsChanged={surface.reloadRecords}
        onUiAction={surface.showAction} />
    );
  }
  if (!consoleProductId || !consoleShape) return null;
  return <EdithPanel key="pixel" session={session} shape={consoleShape}
    productId={consoleProductId} currentPage={viewForRoute(pathname)} scope="platform" />;
}
