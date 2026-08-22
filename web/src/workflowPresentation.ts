import type { WorkflowPack, WorkflowSelectorField } from './types';

export type WorkflowSelectorValues = Record<string, Record<string, string>>;

export function selectorValue(pack: WorkflowPack, field: WorkflowSelectorField, values: WorkflowSelectorValues): string {
  return values[pack.definition.pack_id]?.[field.key] ?? (field.default === undefined || field.default === null ? '' : String(field.default));
}

export function selectorReady(pack: WorkflowPack, values: WorkflowSelectorValues): boolean {
  return pack.definition.project_selector.every((field) => !field.required || selectorValue(pack, field, values).trim().length > 0);
}

export function resolveSelector(pack: WorkflowPack, values: WorkflowSelectorValues): Record<string, string | number> {
  const selector: Record<string, string | number> = {};
  for (const field of pack.definition.project_selector) {
    const raw = selectorValue(pack, field, values).trim();
    if (!raw) continue;
    if (field.value_type === 'integer') {
      const parsed = Number(raw);
      if (!Number.isInteger(parsed)) throw new Error(`${field.label} must be a whole number.`);
      selector[field.key] = parsed;
    } else selector[field.key] = raw;
  }
  return selector;
}

export function workflowProjectTitle(pack: WorkflowPack, selector: Record<string, unknown>): string {
  const identity = pack.definition.project_selector
    .map((field) => selector[field.key] ?? field.default)
    .filter((value) => value !== undefined && value !== null && String(value).trim().length > 0)
    .map(String);
  return identity.length ? `${pack.definition.title} · ${identity.join(' · ')}` : pack.definition.title;
}

export function selectorSummary(pack: WorkflowPack | undefined, selector: Record<string, unknown>): string[] {
  return Object.entries(selector).map(([key, value]) => {
    const label = pack?.definition.project_selector.find((field) => field.key === key)?.label ?? key;
    return `${label}: ${String(value)}`;
  });
}
