import { describe, expect, it } from 'vitest';
import { batchIdempotencyKey, reciprocalCrossFeedParents, resolveExactSelection } from './invocationBatch';
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

describe('rendered response selection', () => {
  it('maps a repeated visible phrase to its exact source occurrence', () => {
    const source = 'first 🌊 then first 🌊';
    const renderedStart = source.lastIndexOf('first');
    expect(resolveExactSelection(source, source, 'first', renderedStart)).toEqual({
      kind: 'exact',
      text: 'first',
      startByte: new TextEncoder().encode(source.slice(0, renderedStart)).byteLength,
      endByte: new TextEncoder().encode(source.slice(0, renderedStart + 5)).byteLength,
    });
  });

  it('rejects a rendered occurrence topology hidden by Markdown', () => {
    const source = '[same](https://same.example) and same';
    expect(resolveExactSelection(source, 'same and same', 'same', 9)).toEqual({
      kind: 'unavailable',
      reason: 'Formatting makes this rendered selection ambiguous in the exact source.',
    });
  });

  it('rejects a caller-provided rendered offset that does not bind the selected bytes', () => {
    expect(resolveExactSelection('alpha beta', 'alpha beta', 'beta', 0).kind).toBe('unavailable');
  });
});
