import { describe, expect, it } from 'vitest';
import { extractFileLinks } from './fileLinks';

describe('exact file-link extraction', () => {
  it('collects unique path and line addresses from a response', () => {
    const text = 'See lattice/store.py:3281 and `web/src/App.svelte` then lattice/store.py:3281 again.';
    expect(extractFileLinks(text)).toEqual([
      { path: 'lattice/store.py', line: 3281 },
      { path: 'web/src/App.svelte', line: null },
    ]);
  });

  it('ignores bare filenames without a directory', () => {
    expect(extractFileLinks('README.md and store.py')).toEqual([]);
  });
});
