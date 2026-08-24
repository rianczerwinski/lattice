import type { Invocation } from './types';

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
