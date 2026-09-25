import type { ReactNode } from "react";

export function AuthCard({ title, description, children, footer }: { title: string; description?: ReactNode; children?: ReactNode; footer?: ReactNode }) {
  return (
    <main className="px-auth">
      <div className="px-auth-card">
        <header>
          <div className="px-brand" style={{ padding: 0 }}>
            <span className="px-brand-mark" aria-hidden><span /><span /><span /><span /></span>Pixel
          </div>
          <h1 style={{ fontSize: "var(--px-text-xl)" }}>{title}</h1>
          {description ? <p className="px-muted">{description}</p> : null}
        </header>
        {children}
        {footer ? <div className="px-auth-foot">{footer}</div> : null}
      </div>
    </main>
  );
}
