import type { Invocation } from './types';

export type ExactSelectionResolution =
  | { kind: 'exact'; text: string; startByte: number; endByte: number }
  | { kind: 'unavailable'; reason: string };

function occurrences(value: string, needle: string): number[] {
  const result: number[] = [];
  for (let offset = 0; offset <= value.length - needle.length; offset += 1) {
    if (value.startsWith(needle, offset)) result.push(offset);
  }
  return result;
}

export function resolveExactSelection(source: string, rendered: string, selected: string, renderedStart: number): ExactSelectionResolution {
  if (!selected || renderedStart < 0 || rendered.slice(renderedStart, renderedStart + selected.length) !== selected) {
    return { kind: 'unavailable', reason: 'The rendered selection could not be located exactly.' };
  }
  const renderedOccurrences = occurrences(rendered, selected);
  const sourceOccurrences = occurrences(source, selected);
  const ordinal = renderedOccurrences.indexOf(renderedStart);
  if (ordinal < 0 || renderedOccurrences.length !== sourceOccurrences.length) {
    return { kind: 'unavailable', reason: 'Formatting makes this rendered selection ambiguous in the exact source.' };
  }
  const sourceStart = sourceOccurrences[ordinal];
  const encoder = new TextEncoder();
  return {
    kind: 'exact',
    text: selected,
    startByte: encoder.encode(source.slice(0, sourceStart)).byteLength,
    endByte: encoder.encode(source.slice(0, sourceStart + selected.length)).byteLength,
  };
}

export function batchIdempotencyKey(batchId: string): string {
  if (!/^batch_[a-z0-9]+$/i.test(batchId)) throw new Error('A sealed batch identity is required before execution.');
  return `lattice-ui:${batchId}`;
}

export function reciprocalCrossFeedParents(invocations: Invocation[]): Record<'anthropic' | 'openai', string[]> {
  if (invocations.length !== 2) throw new Error('Reciprocal cross-feed requires exactly two completed source responses.');
  const byProvider = new Map<string, Invocation>();
  for (const invocation of invocations) {
    const provider = String(invocation.route.provider ?? '');
    if (!['anthropic', 'openai'].includes(provider)) throw new Error('Reciprocal cross-feed requires one Claude response and one Codex response.');
    if (byProvider.has(provider)) throw new Error('Reciprocal cross-feed requires one Claude response and one Codex response.');
    if (!['succeeded', 'imported'].includes(invocation.state) || !invocation.output_block_id) throw new Error('Reciprocal cross-feed cannot consume a nonterminal source.');
    byProvider.set(provider, invocation);
  }
  const anthropic = byProvider.get('anthropic');
  const openai = byProvider.get('openai');
  if (!anthropic || !openai) throw new Error('Reciprocal cross-feed requires one Claude response and one Codex response.');
  return { anthropic: [openai.invocation_id], openai: [anthropic.invocation_id] };
}
