import { describe, expect, it } from 'vitest';
import { copyText, invocationExportPayload } from './copyExport';

describe('copy and export contract', () => {
  it('copies exact text through the clipboard port', async () => {
    const written: string[] = [];
    await copyText('exact response', { writeText: async (value) => { written.push(value); } });
    expect(written).toEqual(['exact response']);
  });

  it('exports invocation identity, prompt, response, and manifest without inventing fields', () => {
    const payload = JSON.parse(invocationExportPayload({
      invocation_id: 'invocation_1',
      state: 'succeeded',
      compiled_prompt: 'exact prompt',
      output_block: { content: 'exact response' },
      manifest: { manifest_sha256: 'a'.repeat(64) },
    }));
    expect(payload).toEqual({
      invocation_id: 'invocation_1',
      state: 'succeeded',
      compiled_prompt: 'exact prompt',
      response: 'exact response',
      manifest: { manifest_sha256: 'a'.repeat(64) },
    });
  });
});
