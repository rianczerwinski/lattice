import { describe, expect, it } from 'vitest';
import { nextPrompt, previousPrompt, recordPrompt } from './promptHistory';

describe('prompt history contract', () => {
  it('records unique recent prompts newest first and drops the oldest', () => {
    const history = Array.from({ length: 40 }, (_, index) => `prompt-${index}`);
    const next = recordPrompt(history, 'newest exact draft');
    expect(next[0]).toBe('newest exact draft');
    expect(next).toHaveLength(40);
    expect(next).not.toContain('prompt-39');
  });

  it('walks backward then forward without inventing prompts', () => {
    const history = recordPrompt(recordPrompt([], 'first'), 'second');
    const older = previousPrompt(history, null);
    expect(older).toEqual({ prompt: 'second', cursor: 0 });
    const oldest = previousPrompt(history, older?.cursor ?? null);
    expect(oldest).toEqual({ prompt: 'first', cursor: 1 });
    expect(nextPrompt(history, 1, 'live draft')).toEqual({ prompt: 'second', cursor: 0 });
    expect(nextPrompt(history, 0, 'live draft')).toEqual({ prompt: 'live draft', cursor: null });
  });
});
