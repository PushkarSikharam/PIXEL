"use client";

import { Pencil, Plus } from "lucide-react";
import { Button, EmptyState } from "./ui";
import type { ApiProductShape, ApiRecord, ApiRecords } from "@pixel-console/lib/pixel-api";

/**
 * A product's records as the guided demo shows its tickets and team: one card each, with its
 * title, the details that say what it is, its state as coloured badges, and the ways to open or
 * change it. People are shown as people, with their initials, as the demo shows its team.
 */
export function RecordCards({ shape, records, entity, rows, columns, onAdd, onOpen, onEdit }: {
  shape: ApiProductShape;
  records: ApiRecords;
  entity: string;
  rows: ApiRecord[];
  columns: string[];
  onAdd?: () => void;
  onOpen?: (row: ApiRecord) => void;
  onEdit?: (row: ApiRecord) => void;
}) {
  const entityShape = shape.entities.find((candidate) => candidate.name === entity);
  if (!entityShape) return null;
  const fields = entityShape.fields;
  if (rows.length === 0) {
    return (
      <EmptyState title={`No ${entityShape.plural.toLowerCase()} yet`}
        action={onAdd ? <Button variant="primary" onClick={onAdd}>
          <Plus aria-hidden />Add the first {entityShape.label.toLowerCase()}</Button> : undefined}>
        Add one here, or ask {shape.assistant_name} to create it for you.
      </EmptyState>
    );
  }
  const shown = (columns.length ? columns : fields.filter((field) => field.display).map((field) => field.name))
    .filter((name) => name !== entityShape.title_field);
  const badgeFields = shown.filter((name) => fields.find((field) => field.name === name)?.type === "enum");
  const detailFields = shown.filter((name) => !badgeFields.includes(name));
  const title = (row: ApiRecord) => String(row[entityShape.title_field] ?? row.title ?? row.id);
  const detail = (row: ApiRecord) => [row.id, ...detailFields.map((name) => said(shape, records, fields, name, row[name]))]
    .filter((part): part is string => Boolean(part));

  if (entityShape.is_people) {
    return (
      <ul className="px-people-cards" aria-label={entityShape.plural}>
        {rows.map((row) => (
          <li key={row.id} className="px-person-card">
            <span className="px-avatar" aria-hidden>{initials(title(row))}</span>
            <span className="px-person-text">
              <strong>{title(row)}</strong>
              <span>{detail(row).slice(1).join(" · ") || entityShape.label}</span>
            </span>
            {onOpen ? <Button size="sm" variant="ghost" onClick={() => onOpen(row)}
              aria-label={`Open ${title(row)}`}>Open</Button> : null}
          </li>
        ))}
      </ul>
    );
  }

  return (
    <ul className="px-record-cards" aria-label={entityShape.plural}>
      {rows.map((row) => (
        <li key={row.id} className="px-record-card">
          <strong className="px-record-card-title">{title(row)}</strong>
          <span className="px-record-card-detail">{detail(row).join(" · ")}</span>
          <span className="px-record-card-actions">
            {badgeFields.map((name) => {
              const value = row[name];
              if (value === null || value === undefined || value === "") return null;
              return <span key={name} className="px-state-badge" data-tone={toneFor(String(value))}
                title={fields.find((field) => field.name === name)?.label}>{String(value)}</span>;
            })}
            {onOpen ? <Button size="sm" onClick={() => onOpen(row)} aria-label={`Open ${title(row)}`}>Open</Button> : null}
            {onEdit && Number(row.revision ?? 0) >= 1 ? (
              <Button size="sm" variant="ghost" onClick={() => onEdit(row)} aria-label={`Edit ${title(row)}`}>
                <Pencil aria-hidden />Edit</Button>
            ) : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** A value as a person would read it: a reference by the title of what it points at. */
function said(shape: ApiProductShape, records: ApiRecords, fields: ApiProductShape["entities"][number]["fields"],
              name: string, value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  const target = fields.find((field) => field.name === name)?.target;
  const one = (id: unknown) => {
    if (!target) return String(id);
    const found = (records.records[target] ?? []).find((record) => record.id === id);
    return found ? String(found.title || found.id) : String(id);
  };
  if (typeof value === "boolean") return value ? fields.find((field) => field.name === name)?.label ?? "Yes" : null;
  return Array.isArray(value) ? value.map(one).join(", ") || null : one(value);
}

function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "") + (words.length > 1 ? words[words.length - 1][0] : words[0]?.[1] ?? "")).toUpperCase() || "?";
}

/** Colour for a state, from what the word means: finished is green, urgent is red. */
export function toneFor(value: string): "ok" | "warn" | "danger" | "info" | "neutral" {
  const word = value.toLowerCase();
  if (/(urgent|critical|high|blocked|lost|rejected|overdue|failed)/.test(word)) return "danger";
  if (/(medium|waiting|pending|review|screening|interview|offer|proposal|at risk)/.test(word)) return "warn";
  if (/(done|won|solved|complete|closed|hired|active|resolved|shipped)/.test(word)) return "ok";
  if (/(progress|qualified|open|new|planned|todo|to do|applied)/.test(word)) return "info";
  return "neutral";
}
