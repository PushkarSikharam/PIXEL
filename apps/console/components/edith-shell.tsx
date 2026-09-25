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

import { useCallback, useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { useConsole } from "./console-context";
import { EdithPanel } from "./edith";
import { productShape, storedSession, type ApiProductShape, type ApiSession } from "@pixel-console/lib/pixel-api";
import { viewForRoute } from "@pixel-console/lib/console-routes";

/** Visible placeholder when the assistant cannot reach the backend. */
function EdithUnavailable({ onRetry, loading }: { onRetry: () => void; loading: boolean }) {
  return (
    <div className="px-edith px-edith-unavailable" role="status">
      <div className="px-edith-topbar">
        <span className="px-edith-brand">
          <span className="px-edith-mark" aria-hidden>P</span>
          Pixel
        </span>
      </div>
      <div className="px-edith-unavailable-body">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <circle cx="12" cy="12" r="10" />
          <path d="M12 8v4M12 16h.01" />
        </svg>
        <p className="px-edith-unavailable-title">Edith is unavailable</p>
        <p className="px-edith-unavailable-detail">
          The assistant could not connect to the Pixel service. Chat, voice, and actions are temporarily disabled.
        </p>
        <button type="button" className="px-edith-chip" onClick={onRetry} disabled={loading}>
          {loading ? "Connecting\u2026" : "Retry"}
        </button>
      </div>
    </div>
  );
}

export function Edith() {
  const c = useConsole();
  const pathname = usePathname();
  const surface = c.productSurface;
  const consoleProductId = c.account?.console_product_id ?? null;
  const [session, setSession] = useState<ApiSession | null>(null);
  const [consoleShape, setConsoleShape] = useState<ApiProductShape | null>(null);
  const [shapeFailed, setShapeFailed] = useState(false);
  const [retrying, setRetrying] = useState(false);

  useEffect(() => { setSession(storedSession()); }, [c.account]);

  const loadConsoleShape = useCallback(() => {
    if (!consoleProductId || !session) return;
    setShapeFailed(false);
    setRetrying(true);
    productShape(session, consoleProductId)
      .then((shape) => { setConsoleShape(shape); setShapeFailed(false); })
      .catch(() => { setConsoleShape(null); setShapeFailed(true); })
      .finally(() => setRetrying(false));
  }, [consoleProductId, session]);

  useEffect(() => {
    setConsoleShape(null);
    setShapeFailed(false);
    if (!consoleProductId || !session) return;
    let cancelled = false;
    productShape(session, consoleProductId)
      .then((shape) => { if (!cancelled) { setConsoleShape(shape); setShapeFailed(false); } })
      .catch(() => { if (!cancelled) { setConsoleShape(null); setShapeFailed(true); } });
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

  // Show a visible placeholder when the assistant can't load, instead of blank space.
  if (!consoleProductId || shapeFailed) {
    return <EdithUnavailable onRetry={loadConsoleShape} loading={retrying} />;
  }

  if (!consoleShape) return null;
  return <EdithPanel key="pixel" session={session} shape={consoleShape}
    productId={consoleProductId} currentPage={viewForRoute(pathname)} scope="platform" />;
}
