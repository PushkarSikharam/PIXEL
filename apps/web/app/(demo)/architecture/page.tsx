import type { Metadata } from "next";
import Link from "next/link";
import styles from "./page.module.css";

export const metadata: Metadata = {
  title: "Pixel | How Pixel works",
  description:
    "How Pixel turns a written description of a product into a running one: definitions, one engine, scoped records and changes nobody makes without confirming. Developed by PushkarSikharam."
};

const pillars = [
  {
    number: "01",
    title: "A product is described, not built",
    label: "Definition",
    body:
      "A definition says what a product keeps, what its screens are called, what can be done in it and the words people use for those things. Nothing else is needed to run it: no code, no deployment."
  },
  {
    number: "02",
    title: "The description is pinned",
    label: "Version and checksum",
    body:
      "Every published version is immutable and carries a checksum. A running product is pinned to one version, so what it does cannot change underneath the people using it. Changing a product means publishing a new version."
  },
  {
    number: "03",
    title: "One engine answers every product",
    label: "Conversation",
    body:
      "The same engine reads the pinned definition, works out what was meant, and answers. Adding a product adds a definition; it never adds an assistant, a router or a set of rules of its own."
  },
  {
    number: "04",
    title: "Only records in reach are seen",
    label: "Scope",
    body:
      "Which records a person can see is worked out from the definition, by following each record back to what it belongs to. A record that reaches nothing they hold is not filtered out of an answer; it is never loaded."
  },
  {
    number: "05",
    title: "Nothing changes without confirming",
    label: "Proposal and receipt",
    body:
      "A request that would change something is proposed and waits. The confirmation belongs to that one proposal, stops working when the conversation ends, and what is said afterwards is built from what actually committed."
  },
  {
    number: "06",
    title: "Answers are quoted or declined",
    label: "Approved text",
    body:
      "Questions are answered from text approved for that product at that version, quoted with its source. When there is nothing to answer from, the assistant says so rather than filling the gap."
  }
];

const systemMap = [
  {
    label: "Person",
    title: "Typed or spoken request",
    detail: "Ordinary language, follow-ups, corrections and interruptions all enter the same way, whether typed or spoken."
  },
  {
    label: "Edith",
    title: "One assistant, every product",
    detail: "Reads the product's pinned definition, keeps the thread of the conversation, and asks when a request could mean more than one thing."
  },
  {
    label: "Platform",
    title: "Scope and authority",
    detail: "Decides what this person may see and do, in this organization and this product. The definition describes; it never grants."
  },
  {
    label: "Records",
    title: "One store, kept apart",
    detail: "Every product's records live under the organization, the product and the space they belong to, so two products in one account never meet."
  },
  {
    label: "Ledger",
    title: "What was actually done",
    detail: "Each carried-out change is recorded with what changed and who asked, and the reply afterwards is composed from that record."
  }
];

const journeys = [
  {
    title: "Adding your own product",
    steps: ["Describe what it keeps", "Read the definition Pixel wrote", "Accept it, and it is running"]
  },
  {
    title: "Asking about your records",
    steps: ["Ask in your own words", "Pixel answers from what you can see", "Nothing outside your reach is loaded"]
  },
  {
    title: "Creating and assigning",
    steps: ["Ask for the change", "Pixel says what it is about to do", "It is carried out and recorded once you confirm"]
  },
  {
    title: "Moving around Pixel",
    steps: ["Ask to be taken somewhere", "Pixel opens it, or says plainly that it cannot", "Leaving a product is a move, not a refusal"]
  },
  {
    title: "Running several products",
    steps: ["Each has its own definition", "Each has its own records and words", "Adding one changes nothing about the others"]
  }
];

export default function ArchitecturePage() {
  return (
    <main className={styles.page}>
      <header className={styles.topbar}>
        <Link href="/console" className={styles.brand} aria-label="Return to your Pixel workspace">
          <span>P</span>
          <strong>Pixel</strong>
        </Link>
        <nav aria-label="Architecture navigation">
          <Link href="/console">Your workspace</Link>
          <Link href="/demo">Guided demo</Link>
          <a href="https://github.com/PushkarSikharam/Linear-Simplified">GitHub</a>
        </nav>
      </header>

      <section className={styles.hero}>
        <div className={styles.heroCopy}>
          <p className={styles.kicker}>How Pixel works</p>
          <h1>A product is described once. Pixel runs it.</h1>
          <p>
            Pixel is a platform for running products through conversation. A product is described in a
            definition that says what it keeps and what can be done in it; Pixel pins that description to a
            version, gives the product records and screens, and answers for it with one engine. Several
            products can run in one account, and none of them can reach into another.
          </p>
        </div>
        <aside className={styles.credit} aria-label="Developer credit">
          <span>Developed by</span>
          <a href="https://my-port-folio-1x8e.vercel.app/">PushkarSikharam</a>
          <small>Definition-driven conversational platform</small>
        </aside>
      </section>

      <section className={styles.systemMap} aria-labelledby="map-title">
        <div className={styles.sectionIntro}>
          <p className={styles.kicker}>System map</p>
          <h2 id="map-title">One path, whichever product is being asked.</h2>
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
          <p className={styles.kicker}>End to end</p>
          <h2 id="flow-title">From a description of a product to a product people can use.</h2>
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
          <h2 id="model-title">Conversation and authority are kept apart.</h2>
        </div>
        <div className={styles.modelGrid}>
          <article>
            <span>What a definition decides</span>
            <h3>A product describes itself.</h3>
            <p>
              A definition names what the product keeps, what its screens are called, what can be done in it,
              and the words people use for all of that. It is read as untrusted input, checked before anything
              runs on it, and it never writes the words the assistant says.
            </p>
          </article>
          <article>
            <span>What the platform decides</span>
            <h3>Pixel decides what is allowed.</h3>
            <p>
              Who somebody is, which organization they belong to, which records are within their reach and
              whether a change may be carried out are all settled by the platform, on the server, for every
              product the same way. A product cannot widen any of it by describing itself differently.
            </p>
          </article>
        </div>
      </section>

      <section className={styles.journeys} aria-labelledby="journey-title">
        <div className={styles.sectionIntro}>
          <p className={styles.kicker}>What it looks like</p>
          <h2 id="journey-title">Five things you can do, in any product.</h2>
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
          <p className={styles.kicker}>Where it stands</p>
          <h2 id="boundaries-title">What is real, and what is not yet.</h2>
        </div>
        <div className={styles.boundaryList}>
          <p><strong>Working now:</strong> signing in by email, organizations with teams and roles, adding your own product from a description, immutable definition versions, records kept apart by organization and by product, scoped answers, confirmation before any change, a record of every change carried out, answers quoted from approved text, and voice.</p>
          <p><strong>Not yet:</strong> billing and metering, encrypted backups with restores that are proven rather than assumed, retrieval that understands meaning rather than words, and hand-built screens for a product you describe yourself, which today gets its lists and its assistant rather than a bespoke interface.</p>
          <p><strong>Where the line is:</strong> what a product describes and what a product is allowed to do are separate on purpose. A definition can be wrong, or written by somebody untrusted, without being able to reach past what the platform permits.</p>
        </div>
      </section>

      <footer className={styles.footer}>
        <span>How Pixel works</span>
        <span>Developed by <a href="https://github.com/PushkarSikharam">PushkarSikharam</a></span>
        <Link href="/console">Return to your workspace</Link>
      </footer>
    </main>
  );
}
