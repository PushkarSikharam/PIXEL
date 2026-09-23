export function starterDefinition(definitionId: string, name: string): string {
  return JSON.stringify({
    definition: { definition_id: definitionId, version: 1, ownership: "platform_shared" },
    identity: {
      product_name: name,
      assistant_name: "Edith",
      persona: `A precise product operator for ${name}.`,
      voice_style: "Calm, clear and professional.",
      greeting: "Welcome to {product}. I'm {assistant}.",
    },
    vocabulary: {
      terms: ["ticket", "tickets", "issue", "issues", "member", "members", "owner", "owners", "status"],
      corrections: { tikcet: "ticket" },
      correction_markers: ["actually", "instead"],
      negatable_terms: ["ticket", "tickets", "member", "members"],
    },
    entities: {
      ticket: {
        label: "Ticket", plural: "Tickets",
        id: { strategy: "prefix", prefix: "TKT" },
        title_field: "title",
        summary_fields: ["status", "owner"],
        fields: {
          title: { type: "text", required: true, max: 160 },
          status: { type: "enum", required: true, values: ["Todo", "In progress", "Done"], default: "Todo" },
          owner: { type: "ref", target: "member", required: true },
        },
      },
      member: {
        label: "Member", plural: "Members",
        id: { strategy: "slug", from_field: "name" },
        title_field: "name",
        fields: {
          name: { type: "text", required: true, editable: false },
          tickets: { type: "refs", target: "ticket" },
        },
      },
    },
    people: { entity: "member", assigned_by: ["ticket.owner"], match_on: ["name"] },
    scope: { anchor: "ticket", paths: { ticket: [], member: ["tickets"] } },
    views: {
      tickets: { label: "Tickets", kind: "list", entity: "ticket", columns: ["title", "status", "owner"] },
      members: { label: "Members", kind: "list", entity: "member", columns: ["name"] },
    },
    actions: {
      open_tickets: { capability: "NAVIGATE_VIEW", view: "tickets", description: "Open the ticket list." },
      open_members: { capability: "NAVIGATE_VIEW", view: "members", description: "Open the team directory." },
      open_ticket: { capability: "OPEN_RECORD", entity: "ticket", description: "Open one ticket." },
      tickets_by_owner: { capability: "FILTER_RECORDS", entity: "ticket", by: "owner", description: "Show tickets assigned to one member." },
      create_ticket: { capability: "CREATE_RECORD", entity: "ticket", fields: ["title", "status", "owner"], description: "Create a ticket." },
      update_ticket: { capability: "UPDATE_RECORD", entity: "ticket", fields: ["status", "owner"], description: "Update a ticket." },
      add_member: { capability: "CREATE_RECORD", entity: "member", fields: ["name"], description: "Add a team member." },
    },
    intents: [
      { action: "open_tickets", response: "anchor_count", match: [["how many", "count", "number of"], ["ticket", "tickets"]] },
      { action: "open_tickets", response: "view_opened", match: [["ticket", "tickets", "issue", "issues"]] },
      { action: "open_members", response: "view_opened", match: [["member", "members", "team"]] },
      { action: "tickets_by_owner", requires: ["person"], response: "records_filtered", match: [["assigned to", "for"], ["ticket", "tickets"]] },
      { action: "open_ticket", requires: ["record"], response: "record_opened", match: [["open", "show"]] },
      { action: "create_ticket", requires: ["person"], response: "record_created", match: [["add", "create", "new"], ["ticket", "issue"]] },
      { action: "add_member", response: "record_created", match: [["add", "create", "new"], ["member", "person"]] },
      { action: "update_ticket", requires: ["record"], response: "record_updated", match: [["assign", "owner", "status", "done", "todo"]] },
    ],
    guardrails: [
      { topic: "destructive_change", response: "destructive_refused", match: [["delete", "erase", "wipe", "remove all"]] },
    ],
    responses: {
      view_opened: "I'll open {view}.",
      anchor_count: "{scope} has {count} visible tickets: {records}.",
      record_opened: "I'll open {record_id}.",
      records_filtered: "I found {count} tickets for {person}.",
      destructive_refused: "I can't delete or erase anything here.",
      record_created: "I'll create the record.",
      record_updated: "I'll update {record_id}: {changes}.",
      clarify_create: "What title should I use?",
    },
  }, null, 2);
}
