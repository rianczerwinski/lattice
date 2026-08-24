import { describe, expect, it } from 'vitest';
import { batchIdempotencyKey } from './invocationBatch';

describe('invocation batch submission', () => {
  it('reuses one content-bound key when a committed response must be retried', () => {
    const batchId = 'batch_0123456789abcdef';
    expect(batchIdempotencyKey(batchId)).toBe(batchIdempotencyKey(batchId));
    expect(batchIdempotencyKey(batchId)).not.toBe(batchIdempotencyKey('batch_fedcba9876543210'));
  });

  it('rejects a missing or malformed batch identity', () => {
    expect(() => batchIdempotencyKey('undefined')).toThrow('sealed batch identity');
  });
});
