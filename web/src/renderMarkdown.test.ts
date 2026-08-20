import { describe, expect, it } from 'vitest';
import { renderMarkdown } from './renderMarkdown';

describe('renderMarkdown', () => {
  it('preserves safe response formatting', () => {
    expect(renderMarkdown('**clear** response')).toContain('<strong>clear</strong>');
  });

  it('rejects executable HTML and URL mutations', () => {
    const rendered = renderMarkdown('<img src=x onerror="globalThis.pwned=1"><svg onload="globalThis.pwned=2"></svg>[bad](javascript:alert(1))');
    expect(rendered).not.toMatch(/onerror|onload|javascript:/i);
  });
});
