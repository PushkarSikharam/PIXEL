"use client";

/**
 * A form for a product nobody wrote a form for.
 *
 * Every product Pixel runs describes its own records: what they are called, what fields they
 * carry, which of those are required, what an enum may contain and which records a reference may
 * point at. That is enough to build the form, so a product somebody added this morning gets the
 * same create and edit screens as the one this repository ships, without a line written for it.
 *
 * Two rules keep it honest. The definition decides what is offered: a field it holds fixed is
 * shown but cannot be typed into, and a reference offers only records the person can actually
 * see, because the list comes from what the server sent them. And the server decides what is
 * accepted: everything here is checked again there, so a control that gets a field wrong is a
 * bad form rather than a way past a rule.
 */

import { useEffect, useMemo, useState } from "react";
import { Dialog } from "@pixel-console/components/overlays";
import { Alert, Button, Field, Input } from "@pixel-console/components/ui";
import {
  RecordConflictError, createProductRecord, editProductRecord,
  type ApiEntityShape, type ApiFieldShape, type ApiRecord, type ApiRecords, type ApiSession,
} from "@pixel-console/lib/pixel-api";

/** What a control holds while it is being edited, before it becomes a field value. */
type Draft = Record<string, string | boolean | string[]>;

export function RecordFormDialog({ open, onOpenChange, session, productId, entity, records,
  editing, onSaved }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  session: ApiSession;
  productId: string;
  entity: ApiEntityShape;
  /** Every record of this product the person can see, for the reference pickers. */
  records: ApiRecords["records"];
  /** The record being changed, or nothing when this is a new one. */
  editing: ApiRecord | null;
  onSaved: (record: ApiRecord, created: boolean) => void;
}) {
  const fields = useMemo(() => editableFields(entity, editing !== null), [entity, editing]);
  const [draft, setDraft] = useState<Draft>({});
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [conflict, setConflict] = useState<ApiRecord | null>(null);

  // Start from the record being changed, or from the product's own defaults for a new one. Reset
  // whenever the dialog opens, so a cancelled edit is never carried into the next one.
  useEffect(() => {
    if (!open) return;
    setDraft(Object.fromEntries(fields.map((field) => [field.name, toDraft(field, editing?.[field.name])])));
    setProblem(null);
    setConflict(null);
  }, [open, fields, editing]);

  const title = editing
    ? `Edit ${String(editing[entity.title_field] ?? editing.id)}`
    : `New ${entity.label.toLowerCase()}`;

  async function save() {
    setSaving(true);
    setProblem(null);
    setConflict(null);
    try {
      const values = Object.fromEntries(fields.map((field) => [field.name, fromDraft(field, draft[field.name])]));
      const saved = editing
        ? await editProductRecord(session, productId, entity.name, editing.id,
            changedOnly(values, editing), Number(editing.revision ?? 0))
        : await createProductRecord(session, productId, entity.name, values);
      onSaved(saved, editing === null);
      onOpenChange(false);
    } catch (error) {
      if (error instanceof RecordConflictError) {
        setConflict(error.current);
        setProblem(error.message);
      } else {
        setProblem(error instanceof Error ? error.message : "This could not be saved.");
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={title}
      description={editing
        ? `Changing this ${entity.label.toLowerCase()}. Only what you change is sent.`
        : `Adding a ${entity.label.toLowerCase()} to this product.`}
      actions={<>
        <Button onClick={() => onOpenChange(false)} disabled={saving}>Cancel</Button>
        <Button variant="primary" loading={saving} onClick={() => void save()}>
          {editing ? "Save changes" : `Create ${entity.label.toLowerCase()}`}
        </Button>
      </>}>
      <form className="px-stack" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        {problem ? (
          <Alert tone={conflict ? "warn" : "danger"} title={conflict ? "Somebody else changed this first" : "This could not be saved"}>
            {conflict ? <>
              <p>{problem}</p>
              <p className="px-muted">It now reads: {summarise(entity, conflict)}</p>
              <Button size="sm" onClick={() => {
                setDraft(Object.fromEntries(fields.map((field) => [field.name, toDraft(field, conflict[field.name])])));
                setProblem(null);
                setConflict(null);
                onSaved(conflict, false);
              }}>Start again from this version</Button>
            </> : problem}
          </Alert>
        ) : null}
        {fields.map((field) => (
          <FieldControl key={field.name} field={field} records={records}
            value={draft[field.name]}
            onChange={(value) => setDraft((current) => ({ ...current, [field.name]: value }))} />
        ))}
        {fields.length === 0 ? <p className="px-muted">Nothing on this record can be changed here.</p> : null}
        {/* Submitting with Return works even though the buttons live in the dialog's own row. */}
        <button type="submit" className="px-sr-only" tabIndex={-1} aria-hidden>Save</button>
      </form>
    </Dialog>
  );
}

/** One control, chosen by what the definition says the field is. */
function FieldControl({ field, records, value, onChange }: {
  field: ApiFieldShape;
  records: ApiRecords["records"];
  value: string | boolean | string[] | undefined;
  onChange: (value: string | boolean | string[]) => void;
}) {
  const label = field.required ? `${field.label} (required)` : field.label;

  if (field.type === "boolean") {
    return (
      <Field label={label}>
        {({ id, describedBy }) => (
          <span className="px-row">
            <input id={id} aria-describedby={describedBy} type="checkbox" checked={value === true}
              onChange={(event) => onChange(event.target.checked)} />
            <span className="px-muted">{value === true ? "Yes" : "No"}</span>
          </span>
        )}
      </Field>
    );
  }

  if (field.type === "enum") {
    return (
      <Field label={label}>
        {({ id, describedBy }) => (
          <select id={id} aria-describedby={describedBy} className="px-input" value={String(value ?? "")}
            onChange={(event) => onChange(event.target.value)}>
            <option value="">{field.required ? "Choose one" : "None"}</option>
            {field.values.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
        )}
      </Field>
    );
  }

  if (field.type === "ref" || field.type === "refs") {
    const options = (records[field.target ?? ""] ?? []);
    const many = field.type === "refs";
    const chosen = many ? (Array.isArray(value) ? value : []) : [String(value ?? "")].filter(Boolean);
    if (options.length === 0) {
      return (
        <Field label={label} hint={`There are no ${field.target ?? "records"} to choose from yet.`}>
          {({ id }) => <Input id={id} value="" disabled placeholder="Nothing to choose yet" />}
        </Field>
      );
    }
    return (
      <Field label={label} hint={many ? "Choose any number." : undefined}>
        {({ id, describedBy }) => (
          <select id={id} aria-describedby={describedBy} className="px-input" multiple={many}
            value={many ? chosen : (chosen[0] ?? "")}
            onChange={(event) => onChange(many
              ? Array.from(event.target.selectedOptions, (option) => option.value)
              : event.target.value)}>
            {many ? null : <option value="">{field.required ? "Choose one" : "None"}</option>}
            {options.map((option) => (
              <option key={option.id} value={option.id}>{String(option.title || option.id)}</option>
            ))}
          </select>
        )}
      </Field>
    );
  }

  if (field.type === "text_list") {
    return (
      <Field label={label} hint="One per line.">
        {({ id, describedBy }) => (
          <textarea id={id} aria-describedby={describedBy} className="px-input" rows={3}
            value={Array.isArray(value) ? value.join("\n") : String(value ?? "")}
            onChange={(event) => onChange(event.target.value.split("\n"))} />
        )}
      </Field>
    );
  }

  return (
    <Field label={label}>
      {({ id, describedBy, invalid }) => (
        <Input id={id} describedBy={describedBy} invalid={invalid}
          type={field.type === "integer" ? "number" : field.type === "date" ? "date" : "text"}
          value={String(value ?? "")} onChange={(event) => onChange(event.target.value)} />
      )}
    </Field>
  );
}

/**
 * The fields this form may offer.
 *
 * A field the definition holds fixed is left out of an edit, because it cannot be changed; it is
 * offered on a new record, because that is the only moment it is set. A field the definition
 * hides is left out of both: a product that does not show a field does not ask for one either.
 */
export function editableFields(entity: ApiEntityShape, editing: boolean): ApiFieldShape[] {
  return entity.fields.filter((field) => (editing ? field.editable : true));
}

/** Only what the person actually changed, so an edit never re-sends somebody else's values. */
function changedOnly(values: Record<string, unknown>, editing: ApiRecord): Record<string, unknown> {
  const changes = Object.fromEntries(
    Object.entries(values).filter(([name, value]) => !same(value, editing[name])),
  );
  return Object.keys(changes).length ? changes : values;
}

function same(left: unknown, right: unknown): boolean {
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((item, index) => item === right[index]);
  }
  if (left === null && right === undefined) return true;
  if (left === undefined && right === null) return true;
  return left === right;
}

/** A stored value as a control holds it. */
function toDraft(field: ApiFieldShape, value: unknown): string | boolean | string[] {
  if (field.type === "boolean") return value === true;
  if (field.type === "refs" || field.type === "text_list") {
    return Array.isArray(value) ? value.map(String) : [];
  }
  return value === null || value === undefined ? "" : String(value);
}

/** What a control holds, as the field value the product expects. */
function fromDraft(field: ApiFieldShape, value: string | boolean | string[] | undefined): unknown {
  if (field.type === "boolean") return value === true;
  if (field.type === "refs" || field.type === "text_list") {
    const items = (Array.isArray(value) ? value : []).map((item) => item.trim()).filter(Boolean);
    return items;
  }
  const text = String(value ?? "").trim();
  if (text === "") return field.required ? "" : null;
  if (field.type === "integer") {
    const number = Number(text);
    return Number.isInteger(number) ? number : text;
  }
  return text;
}

/** A refused edit says what the record reads now, in the product's own words. */
function summarise(entity: ApiEntityShape, record: ApiRecord): string {
  const parts = [entity.title_field, ...entity.summary_fields]
    .filter((name, index, all) => all.indexOf(name) === index)
    .map((name) => {
      const field = entity.fields.find((candidate) => candidate.name === name);
      const value = record[name];
      const shown = Array.isArray(value) ? value.join(", ") : String(value ?? "none");
      return `${field?.label ?? name}: ${shown}`;
    });
  return parts.join("; ");
}
