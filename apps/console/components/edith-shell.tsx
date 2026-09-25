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

import { useEffect, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { useConsole } from "./console-context";
import { EdithPanel } from "./edith";
import { productShape, serverUnavailable, storedSession, SERVER_UNAVAILABLE, type ApiProductShape,
  type ApiSession } from "@pixel-console/lib/pixel-api";
import { viewForRoute } from "@pixel-console/lib/console-routes";

export function Edith() {
  const c = useConsole();
  const pathname = usePathname();
  const surface = c.productSurface;
  const consoleProductId = c.account?.console_product_id ?? null;
  const [session, setSession] = useState<ApiSession | null>(null);
  const [consoleShape, setConsoleShape] = useState<ApiProductShape | null>(null);
  // Why Pixel's own assistant could not be loaded, so the column can say so instead of vanishing.
  const [shapeProblem, setShapeProblem] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => { setSession(storedSession()); }, [c.account]);

  useEffect(() => {
    setConsoleShape(null);
    setShapeProblem(null);
    if (!consoleProductId || !session) return;
    let cancelled = false;
    productShape(session, consoleProductId)
      .then((shape) => { if (!cancelled) setConsoleShape(shape); })
      // A panel that answers nothing is worse than none, but so is a gap nobody can explain.
      .catch((caught) => {
        if (!cancelled) setShapeProblem(serverUnavailable(caught) ? SERVER_UNAVAILABLE : "The assistant could not be loaded.");
      });
    return () => { cancelled = true; };
  }, [consoleProductId, session, attempt]);

  if (!session || !c.account) return null;
  if (surface) {
    return (
      <EdithPanel key={`product:${surface.productId}`} session={session} shape={surface.shape}
        productId={surface.productId} currentPage={surface.currentPage}
        selectedRecordId={surface.selectedRecordId} scope="product" onRecordsChanged={surface.reloadRecords}
        onUiAction={surface.showAction} />
    );
  }
  if (!consoleProductId) {
    return <EdithUnavailable>
      The assistant is not set up for this deployment yet. Everything else in the console still works.
    </EdithUnavailable>;
  }
  if (shapeProblem) {
    return <EdithUnavailable onRetry={() => setAttempt((count) => count + 1)}>{shapeProblem}</EdithUnavailable>;
  }
  if (!consoleShape) return null;
  return <EdithPanel key="pixel" session={session} shape={consoleShape}
    productId={consoleProductId} currentPage={viewForRoute(pathname)} scope="platform" />;
}

/** Where the assistant would be, saying why it is not, in the same place and the same frame. */
function EdithUnavailable({ children, onRetry }: { children: ReactNode; onRetry?: () => void }) {
  return (
    <aside className="px-product-assistant px-edith px-edith-unavailable" aria-label="Assistant unavailable">
      <div className="px-edith-topbar">
        <span className="px-edith-brand"><span className="px-edith-mark" aria-hidden>P</span>Pixel</span>
        <span className="px-edith-state" role="status">Unavailable</span>
      </div>
      <div className="px-edith-intro">
        <p className="px-edith-kicker">Your guide to Pixel</p>
        <p className="px-edith-note">{children}</p>
        {onRetry ? <button type="button" className="px-edith-chip" style={{ marginTop: 10 }} onClick={onRetry}>
          Try again</button> : null}
      </div>
    </aside>
  );
}
