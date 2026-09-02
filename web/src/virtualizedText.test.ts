import { describe, expect, it } from 'vitest';
import { windowedTail, windowedText } from './virtualizedText';

describe('large-output windowing', () => {
  it('keeps short text exact', () => {
    expect(windowedText('exact')).toEqual({ text: 'exact', omittedBefore: 0, omittedAfter: 0 });
  });

  it('preserves the start and end of a large response and reports omitted bytes', () => {
    const value = `${'A'.repeat(90)}MIDDLE${'Z'.repeat(90)}`;
    const windowed = windowedText(value, 40);
    expect(windowed.text.startsWith('A')).toBe(true);
    expect(windowed.text.endsWith('Z')).toBe(true);
    expect(windowed.text).not.toContain('MIDDLE');
    expect(windowed.omittedAfter).toBeGreaterThan(0);
  });

  it('keeps only the newest live-progress tail', () => {
    const windowed = windowedTail('abcdefghij', 4);
    expect(windowed).toEqual({ text: 'ghij', omittedBefore: 6, omittedAfter: 0 });
  });
});
