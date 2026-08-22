import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, beginSession, extractWorkflow } from './api';

afterEach(() => vi.unstubAllGlobals());

describe('api boundary', () => {
  it('treats a successful no-content mutation as success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ csrf: 'bound' }), { status: 200, headers: { 'content-type': 'application/json' } })).mockResolvedValueOnce(new Response(null, { status: 204 })));
    await beginSession();
    await expect(api('/api/v1/roles/r/context-defaults/b', { method: 'DELETE' })).resolves.toBeUndefined();
  });

  it('keeps request failure distinct from an empty successful response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'backend unavailable' }), { status: 503, headers: { 'content-type': 'application/json' } })));
    await expect(api('/api/v1/projects')).rejects.toThrow('backend unavailable');
  });

  it('binds workflow extraction to the exact checked invocation identities', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrf: 'bound' }), { status: 200, headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ generated_object: { artifact_id: 'artifact:exact' } }), { status: 200, headers: { 'content-type': 'application/json' } }));
    vi.stubGlobal('fetch', fetch);
    await beginSession();

    await extractWorkflow('project_exact', ['invocation_a', 'invocation_b'], 'Reusable path');

    expect(fetch).toHaveBeenLastCalledWith('/api/v1/workflows/extract', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ project_id: 'project_exact', invocation_ids: ['invocation_a', 'invocation_b'], title: 'Reusable path' }),
    }));
  });
});
