import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";
import { useId } from "react";
import { AlertTriangle, CheckCircle2, CircleAlert, Info, Loader2, Lock, SearchX } from "lucide-react";

type Tone = "neutral" | "ok" | "warn" | "danger" | "accent";

export function Button({ variant = "default", size = "md", loading = false, children, ...props }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "primary" | "danger" | "ghost"; size?: "sm" | "md"; loading?: boolean }) {
  return (
    <button type="button" className="px-button" data-variant={variant} data-size={size}
      aria-busy={loading || undefined} disabled={props.disabled || loading} {...props}>
      {loading ? <Loader2 aria-hidden className="px-spin" /> : null}
      {children}
    </button>
  );
}

export function Badge({ tone = "neutral", children, icon }: { tone?: Tone; children: ReactNode; icon?: ReactNode }) {
  return <span className="px-badge" data-tone={tone}>{icon}{children}</span>;
}

/** Status is never colour alone: every badge carries its word. */
const STATUS: Record<string, { tone: Tone; label: string }> = {
  active: { tone: "ok", label: "Active" }, published: { tone: "ok", label: "Published" },
  validated: { tone: "accent", label: "Validated" }, draft: { tone: "neutral", label: "Draft" },
  deploying: { tone: "accent", label: "Deploying" }, pending: { tone: "neutral", label: "Pending" },
  failed: { tone: "danger", label: "Failed" }, revoked: { tone: "danger", label: "Revoked" },
  retired: { tone: "neutral", label: "Retired" }, superseded: { tone: "neutral", label: "Superseded" },
  disabled: { tone: "warn", label: "Disabled" }, archived: { tone: "neutral", label: "Archived" },
  suspended: { tone: "warn", label: "Suspended" }, removed: { tone: "neutral", label: "Removed" },
  expired: { tone: "warn", label: "Expired" }, accepted: { tone: "ok", label: "Accepted" },
};

export function StatusBadge({ status }: { status: string }) {
  const s = STATUS[status] ?? { tone: "neutral" as Tone, label: status };
  return <Badge tone={s.tone}>{s.label}</Badge>;
}

export type RasterState = "done" | "active" | "failed" | "warn" | "idle";
/** Pixel's square status raster: one cell per real stage, labelled for assistive technology. */
export function StatusRaster({ cells, label }: { cells: Array<{ state: RasterState; name: string }>; label: string }) {
  const summary = cells.map((c) => `${c.name}: ${c.state === "idle" ? "not started" : c.state}`).join(", ");
  return (
    <span className="px-raster" role="img" aria-label={`${label}. ${summary}`} title={summary}>
      {cells.map((c) => <span key={c.name} data-state={c.state} />)}
    </span>
  );
}

export function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string | null; children: (props: { id: string; describedBy?: string; invalid: boolean }) => ReactNode }) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className="px-field">
      <label className="px-label" htmlFor={id}>{label}</label>
      {children({ id, describedBy, invalid: Boolean(error) })}
      {hint ? <span id={hintId} className="px-hint">{hint}</span> : null}
      {error ? <span id={errorId} className="px-error-text" role="alert"><CircleAlert aria-hidden size={14} />{error}</span> : null}
    </div>
  );
}

export function Input({ invalid, describedBy, ...props }: InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean; describedBy?: string }) {
  return <input className="px-input" aria-invalid={invalid || undefined} aria-describedby={describedBy} {...props} />;
}

export function Alert({ tone = "neutral", title, children }: { tone?: "neutral" | "warn" | "danger" | "ok"; title?: string; children: ReactNode }) {
  const Icon = tone === "danger" ? CircleAlert : tone === "warn" ? AlertTriangle : tone === "ok" ? CheckCircle2 : Info;
  return (
    <div className="px-alert" data-tone={tone} role={tone === "danger" ? "alert" : "status"}>
      <Icon aria-hidden />
      <div>{title ? <strong>{title} </strong> : null}{children}</div>
    </div>
  );
}

export function Panel({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="px-panel">
      {title || actions ? <header className="px-panel-header"><h3>{title}</h3>{actions}</header> : null}
      <div className="px-panel-body">{children}</div>
    </section>
  );
}

export function PageHead({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <header className="px-page-head">
      <div><h1>{title}</h1>{description ? <p>{description}</p> : null}</div>
      {actions ? <div className="px-row">{actions}</div> : null}
    </header>
  );
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return <div className="px-state"><SearchX aria-hidden /><h3>{title}</h3>{children ? <p>{children}</p> : null}{action}</div>;
}

export function ErrorState({ title = "Something went wrong", children, action }: { title?: string; children?: ReactNode; action?: ReactNode }) {
  return <div className="px-state" data-tone="danger" role="alert"><CircleAlert aria-hidden /><h3>{title}</h3>{children ? <p>{children}</p> : null}{action}</div>;
}

export function PermissionDenied({ what = "this page" }: { what?: string }) {
  return (
    <div className="px-state" role="status">
      <Lock aria-hidden />
      <h3>You don&apos;t have access to {what}</h3>
      <p>Ask an organization administrator if you need it. Pixel doesn&apos;t say whether something exists when you can&apos;t see it.</p>
    </div>
  );
}

export function LoadingRows({ rows = 4, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-live="polite" className="px-stack" style={{ gap: 10 }}>
      <span className="px-sr-only">{label}</span>
      {Array.from({ length: rows }, (_, i) => <div key={i} className="px-skeleton" style={{ width: `${90 - i * 12}%` }} />)}
    </div>
  );
}
