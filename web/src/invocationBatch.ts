export function batchIdempotencyKey(batchId: string): string {
  if (!/^batch_[a-z0-9]+$/i.test(batchId)) throw new Error('A sealed batch identity is required before execution.');
  return `lattice-ui:${batchId}`;
}
