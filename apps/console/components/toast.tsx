"use client";

import { AlertTriangle, CheckCircle2, CircleAlert, X } from "lucide-react";
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

type Tone = "ok" | "warn" | "danger";
interface Toast { id: number; tone: Tone; message: string }

const ToastContext = createContext<(tone: Tone, message: string) => void>(() => {});

/** Notifications are announced through a polite live region and dismiss themselves after 6 s. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);
  const dismiss = useCallback((id: number) => setToasts((all) => all.filter((t) => t.id !== id)), []);
  const push = useCallback((tone: Tone, message: string) => {
    const id = next.current++;
    setToasts((all) => [...all.slice(-3), { id, tone, message }]);
    window.setTimeout(() => dismiss(id), 6000);
  }, [dismiss]);
  const value = useMemo(() => push, [push]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="px-toasts" role="status" aria-live="polite">
        {toasts.map((t) => {
          const Icon = t.tone === "ok" ? CheckCircle2 : t.tone === "warn" ? AlertTriangle : CircleAlert;
          return (
            <div key={t.id} className="px-toast" data-tone={t.tone}>
              <Icon aria-hidden />
              <span style={{ flex: 1 }}>{t.message}</span>
              <button type="button" className="px-button" data-variant="ghost" data-size="sm" aria-label="Dismiss notification" onClick={() => dismiss(t.id)}><X aria-hidden /></button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);
