import { describe, expect, it } from 'vitest';
import type { WorkflowPack } from './types';
import { resolveSelector, selectorReady, selectorSummary, workflowProjectTitle } from './workflowPresentation';

const pack: WorkflowPack = {
  available: true,
  canonical_sha256: 'a'.repeat(64),
  source_sha256: 'b'.repeat(64),
  definition: {
    pack_id: 'platform-foundry',
    title: 'Octopus Platform Foundry',
    project_selector: [{ key: 'platform_id', label: 'Octopus platform ID', value_type: 'string', required: true }],
    commands: [],
    triggers: [],
    allowed_transitions: [],
  },
};

describe('workflow project presentation', () => {
  it('reacts to an explicit required selector and keeps it visible in project identity', () => {
    expect(selectorReady(pack, {})).toBe(false);
    const values = { 'platform-foundry': { platform_id: 'pinterest-com' } };
    expect(selectorReady(pack, values)).toBe(true);
    const selector = resolveSelector(pack, values);
    expect(workflowProjectTitle(pack, selector)).toBe('Octopus Platform Foundry · pinterest-com');
    expect(selectorSummary(pack, selector)).toEqual(['Octopus platform ID: pinterest-com']);
  });

  it('rejects a non-integral integer selector instead of changing its meaning', () => {
    const integerPack = structuredClone(pack);
    integerPack.definition.project_selector[0].value_type = 'integer';
    expect(() => resolveSelector(integerPack, { 'platform-foundry': { platform_id: '1.5' } })).toThrow('whole number');
  });
});
