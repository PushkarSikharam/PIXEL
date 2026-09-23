import type { Metadata } from "next";
import Link from "next/link";
import styles from "./page.module.css";

export const metadata: Metadata = {
  title: "Pixel | Product Architecture",
  description:
    "A premium walkthrough of how Pixel turns visitor conversation into scoped workspace actions. Developed by PushkarSikharam."
};

const pillars = [
  {
    number: "01",
    title: "Visitor asks naturally",
    label: "Conversation entry",
    body:
      "A visitor can type or speak in normal language: show sprint planning, open Maya's ticket, assign it to Noah, create work, or ask what a tool does."
  },
  {
    number: "02",
    title: "Edith understands the goal",
    label: "Intent and context",
    body:
      "The assistant normalizes messy language, uses recent context, resolves follow-ups like \"that ticket\", and asks a clarification question when the target is unclear."
  },
  {
    number: "03",
    title: "Pixel checks the workspace",
    label: "Project boundary",
    body:
      "Every request is checked against the active project workspace. Work from other projects stays hidden, and outside-product requests are refused instead of shown."
  },
  {
    number: "04",
    title: "Only approved actions run",
    label: "Controlled UI movement",
    body:
      "Edith does not control the browser freely. Pixel allows a small set of safe product actions: open a view, focus an issue, highlight a control, or guide a creation flow."
  },
  {
    number: "05",
    title: "Saved Continuity",
    label: "How work persists",
    body:
      "Created tickets, projects, cycles and members are saved back into the active workspace. The conversation also remembers the last person, ticket, project and feature so the next request feels connected."
  },
  {
    number: "06",
    title: "Voice Feedback",
    label: "How the demo speaks",
    body:
      "Typed and spoken turns use the same control path. Edith can listen, think, speak through Microsoft voice and stop when the visitor interrupts."
  }
];

const systemMap = [
  {
    label: "Visitor",
    title: "Typed or spoken request",
    detail: "Natural language, interruptions, corrections and follow-ups enter through the same demo surface."
  },
  {
    label: "Edith",
    title: "Conversation guide",
    detail: "Understands intent, keeps context, asks clarifying questions and explains the next product move."
  },
  {
    label: "Pixel Control Layer",
    title: "Scope and action validation",
    detail: "Checks project visibility, validates allowed actions, blocks off-limit data and rejects unsupported tools."
  },
  {
    label: "Workspace",
    title: "Product surface changes",
    detail: "Dashboards, tickets, projects, cycles, teams and integrations update through approved actions only."
  },
  {
    label: "Memory",
    title: "Saved demo state",
    detail: "Created records, recent actions, last person and current ticket stay available for the next turn."
  }
];

const journeys = [
  {
    title: "Guided Product Tour",
    steps: ["Visitor asks about planning", "Edith opens the cycle view", "Pixel explains the current project work"]
  },
  {
    title: "Scoped Ticket Work",
    steps: ["Visitor asks for Maya's ticket", "Pixel confirms it belongs to the active workspace", "Edith opens the ticket and can guide assignment"]
  },
  {
    title: "Creation Flow",
    steps: ["Visitor asks to create work", "Pixel opens the correct form", "The new record joins the active workspace"]
  },
  {
    title: "Guardrail Moment",
    steps: ["Visitor asks for an outside system", "Pixel keeps the demo in scope", "Edith explains the boundary without breaking flow"]
  },
  {
    title: "Saved Workspace Update",
    steps: ["Visitor creates or edits work", "Pixel saves the change", "The update appears in activity and survives refresh"]
  }
];

export default function ArchitecturePage() {
  return (
    <main className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/" className={styles.brand} aria-label="Return to Pixel workspace">
          <span>P</span>
          <strong>Pixel</strong>
        </Link>
        <nav aria-label="Architecture navigation">
          <Link href="/">Workspace</Link>
          <a href="https://github.com/PushkarSikharam/Linear-Simplified">GitHub</a>
        </nav>
      </header>

      <section className={styles.hero}>
        <div className={styles.heroCopy}>
          <p className={styles.kicker}>Product architecture</p>
          <h1>How Pixel turns conversation into scoped product action.</h1>
          <p>
            Pixel is a voice-first product demo system. Edith listens to the visitor, understands the goal,
            checks the active project boundary, then moves the workspace through approved product actions.
            The result is a guided demo that feels live without exposing unrelated project data.
          </p>
        </div>
        <aside className={styles.credit} aria-label="Developer credit">
          <span>Developed by</span>
          <a href="https://my-port-folio-1x8e.vercel.app/">PushkarSikharam</a>
          <small>Adaptive product demo system</small>
        </aside>
      </section>

      <section className={styles.systemMap} aria-labelledby="map-title">
        <div className={styles.sectionIntro}>
          <p className={styles.kicker}>System map</p>
          <h2 id="map-title">One controlled path from conversation to product movement.</h2>
        </div>
        <div className={styles.mapRail}>
          {systemMap.map((item, index) => (
            <article key={item.title} className={styles.mapNode}>
              <span>{item.label}</span>
              <h3>{item.title}</h3>
              <p>{item.detail}</p>
              {index < systemMap.length - 1 && <i aria-hidden="true" />}
            </article>
          ))}
        </div>
      </section>

      <section className={styles.flow} aria-labelledby="flow-title">
        <div className={styles.sectionIntro}>
          <p className={styles.kicker}>End-to-end flow</p>
          <h2 id="flow-title">What happens after a visitor asks Edith something?</h2>
        </div>
        <div className={styles.timeline}>
          {pillars.map((pillar) => (
            <article key={pillar.number} className={styles.pillar}>
              <div className={styles.pillarNumber}>{pillar.number}</div>
              <p>{pillar.label}</p>
              <h3>{pillar.title}</h3>
              <span>{pillar.body}</span>
            </article>
          ))}
        </div>
      </section>

      <section className={styles.operatingModel} aria-labelledby="model-title">
        <div>
          <p className={styles.kicker}>Operating model</p>
          <h2 id="model-title">The product separates conversation from authority.</h2>
        </div>
        <div className={styles.modelGrid}>
          <article>
            <span>Human-facing layer</span>
            <h3>Edith keeps the demo conversational.</h3>
            <p>
              The visitor can ask naturally, interrupt, correct themselves, ask follow-up questions and request
              common project operations. Edith responds with short explanations, speaks through Microsoft voice,
              and keeps the product moving.
            </p>
          </article>
          <article>
            <span>System-control layer</span>
            <h3>Pixel decides what is allowed.</h3>
            <p>
              Before the interface changes, Pixel checks whether the requested action belongs to the current
              workspace and supported product surface. That is what prevents the demo from wandering into
              unrelated projects or outside tools.
            </p>
          </article>
        </div>
      </section>

      <section className={styles.journeys} aria-labelledby="journey-title">
        <div className={styles.sectionIntro}>
          <p className={styles.kicker}>Reviewer paths</p>
          <h2 id="journey-title">Five moments that prove the system.</h2>
        </div>
        <div className={styles.journeyGrid}>
          {journeys.map((journey) => (
            <article key={journey.title}>
              <h3>{journey.title}</h3>
              <ol>
                {journey.steps.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
            </article>
          ))}
        </div>
      </section>

      <section className={styles.boundaries} aria-labelledby="boundaries-title">
        <div>
          <p className={styles.kicker}>Current boundaries</p>
          <h2 id="boundaries-title">Clear demo limits.</h2>
        </div>
        <div className={styles.boundaryList}>
          <p><strong>Demo-ready:</strong> project scoping, safe actions, creation flows, saved state, guided GitHub and Slack flows, voice input, Microsoft voice output and interruption handling.</p>
          <p><strong>Production gap:</strong> real identity, tenant permissions, audit logs, streaming voice, live third-party integrations and deeper AI reasoning would be needed before customer deployment.</p>
          <p><strong>Positioning:</strong> this is a controlled product-demo agent. It is intentionally safer than an unrestricted autonomous browser agent.</p>
        </div>
      </section>

      <footer className={styles.footer}>
        <span>Pixel Product Architecture</span>
        <span>Developed by <a href="https://github.com/PushkarSikharam">PushkarSikharam</a></span>
        <Link href="/">Return to workspace</Link>
      </footer>
    </main>
  );
}
