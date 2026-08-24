import { describe, expect, it } from 'vitest';
import { batchIdempotencyKey, reciprocalCrossFeedParents } from './invocationBatch';
import type { Invocation } from './types';

function source(invocationId: string, provider: 'anthropic' | 'openai', state = 'succeeded', outputBlockId: string | undefined = `block_${provider}`): Invocation {
  return { invocation_id: invocationId, state, output_block_id: outputBlockId, route: { provider } } as unknown as Invocation;
}

describe('invocation batch submission', () => {
  it('reuses one content-bound key when a committed response must be retried', () => {
    const batchId = 'batch_0123456789abcdef';
    expect(batchIdempotencyKey(batchId)).toBe(batchIdempotencyKey(batchId));
    expect(batchIdempotencyKey(batchId)).not.toBe(batchIdempotencyKey('batch_fedcba9876543210'));
  });

  it('rejects a missing or malformed batch identity', () => {
    expect(() => batchIdempotencyKey('undefined')).toThrow('sealed batch identity');
  });

  it('cross-feeds only the opposite completed provider response', () => {
    const parents = reciprocalCrossFeedParents([source('invocation_claude', 'anthropic'), source('invocation_codex', 'openai')]);
    expect(parents).toEqual({ anthropic: ['invocation_codex'], openai: ['invocation_claude'] });
  });

  it('rejects same-provider and nonterminal cross-feed mutations', () => {
    expect(() => reciprocalCrossFeedParents([source('one', 'anthropic'), source('two', 'anthropic')])).toThrow('one Claude response and one Codex response');
    expect(() => reciprocalCrossFeedParents([source('one', 'anthropic', 'running'), source('two', 'openai')])).toThrow('nonterminal source');
  });
});
