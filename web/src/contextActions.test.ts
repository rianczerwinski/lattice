import { describe, expect, it } from 'vitest';

import { contextActionIntent } from './contextActions';

describe('context action intents', () => {
  it('maps every visible verb onto the one-edge invocation primitive', () => {
    expect(contextActionIntent('side-quest', 'openai', 'excerpt')).toEqual({ sideQuest: true, route: 'openai', promptSeed: '' });
    expect(contextActionIntent('ask-claude', 'openai', 'excerpt').route).toBe('anthropic');
    expect(contextActionIntent('ask-codex', 'anthropic', 'excerpt').route).toBe('openai');
    expect(contextActionIntent('ask-both', 'anthropic', 'excerpt').route).toBe('both');
    expect(contextActionIntent('bring-here', 'anthropic', 'excerpt')).toEqual({ sideQuest: false, route: 'anthropic', promptSeed: '' });
    expect(contextActionIntent('compare', 'anthropic', 'excerpt')).toEqual({ sideQuest: true, route: 'both', promptSeed: 'Analyze this excerpt independently so the two responses can be compared.' });
  });

  it('does not turn bring-here into a hidden route change', () => {
    expect(contextActionIntent('bring-here', 'both', 'artifact').route).toBe('both');
  });
});
