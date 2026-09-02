import { describe, expect, it } from 'vitest';
import { activeAttemptIds, activityFailureForInvocation, activityForInvocation, attentionActivityTransitions, mergeActivitySnapshots, projectActivityBadges, projectReplayCursor, terminalActivityTransitions } from './invocationActivity';
import type { InvocationActivity, InvocationActivitySnapshot } from './types';

function activity(overrides: Partial<InvocationActivity> = {}): InvocationActivity {
  return {
    activity_id: 'batch_a',
    batch_id: 'batch_a',
    project_id: 'project_a',
    state: 'running',
    terminal: false,
    needs_attention: false,
    created_at: '20260902T010000',
    completed_at: null,
    last_event_seq: 11,
    replay_event_cursor: 6,
    active_attempt_ids: ['attempt_a'],
    members: [{ ordinal: 0, spec_id: 'codex', invocation_id: 'invocation_a', state: 'running', terminal_kind: null, output_block_id: null, route: { provider: 'openai' }, attempts: [{ attempt_id: 'attempt_a', attempt_number: 1, state: 'running', started_at: '20260902T010000', replay_event_cursor: 7, latest_event_seq: 11 }] }],
    ...overrides,
  };
}

describe('project-scoped invocation activity', () => {
  it('keeps selection, cancellation, replay, and unread state inside the owning project', () => {
    const paired = activity({ members: [activity().members[0], { ...activity().members[0], spec_id: 'claude', invocation_id: 'invocation_sibling', attempts: [{ ...activity().members[0].attempts[0], attempt_id: 'attempt_sibling', replay_event_cursor: 8, latest_event_seq: 10 }] }], active_attempt_ids: ['attempt_a', 'attempt_sibling'] });
    const second = activity({ activity_id: 'batch_b', batch_id: 'batch_b', project_id: 'project_b', members: [{ ...activity().members[0], invocation_id: 'invocation_b', attempts: [{ ...activity().members[0].attempts[0], attempt_id: 'attempt_b', replay_event_cursor: 20, latest_event_seq: 22 }] }], active_attempt_ids: ['attempt_b'], last_event_seq: 22, replay_event_cursor: 19 });
    const snapshot = { items: [paired, second], project_cursors: { project_a: 11, project_b: 22 }, activity_cursors: { project_a: 11, project_b: 22 }, recent_has_more: { project_a: false, project_b: false } };

    expect(activityForInvocation(snapshot.items, 'invocation_b')?.project_id).toBe('project_b');
    expect(activeAttemptIds(activityForInvocation(snapshot.items, 'invocation_a'), 'invocation_a')).toEqual(['attempt_a']);
    expect(activeAttemptIds(activityForInvocation(snapshot.items, 'invocation_sibling'), 'invocation_sibling')).toEqual(['attempt_sibling']);
    expect(projectReplayCursor(snapshot, 'project_b')).toBe(19);
    expect(projectActivityBadges(snapshot, { project_a: 11, project_b: 10 }, 'project_a')).toEqual({
      project_a: { active: 1, attention: 0, unread: false, failed: false },
      project_b: { active: 1, attention: 0, unread: true, failed: false },
    });
  });

  it('reports only a real nonterminal-to-terminal transition', () => {
    const before: InvocationActivitySnapshot = { items: [activity()], project_cursors: { project_a: 11 }, activity_cursors: { project_a: 11 }, recent_has_more: { project_a: false } };
    const terminal = activity({ state: 'succeeded', terminal: true, completed_at: '20260902T010100', active_attempt_ids: [], last_event_seq: 14, members: [{ ...activity().members[0], state: 'succeeded', attempts: [{ ...activity().members[0].attempts[0], state: 'succeeded', completed_at: '20260902T010100', latest_event_seq: 14 }] }] });
    const after: InvocationActivitySnapshot = { items: [terminal], project_cursors: { project_a: 14 }, activity_cursors: { project_a: 14 }, recent_has_more: { project_a: false } };

    expect(terminalActivityTransitions(before, after).map((item) => item.activity_id)).toEqual(['batch_a']);
    expect(terminalActivityTransitions(after, after)).toEqual([]);
  });

  it('surfaces unresolved dispatch failures without treating unrelated project events as unread work', () => {
    const stalled = activity({ needs_attention: true, members: [{ ...activity().members[0], state: 'sealed', attempts: [], dispatch_failure: { seq: 12, event_type: 'invocation-batch.dispatch-failed', detail: { error: 'provider route unavailable' } } }], active_attempt_ids: [], last_event_seq: 12 });
    const before: InvocationActivitySnapshot = { items: [activity()], project_cursors: { project_a: 30 }, activity_cursors: { project_a: 11 }, recent_has_more: { project_a: false } };
    const after: InvocationActivitySnapshot = { items: [stalled], project_cursors: { project_a: 31 }, activity_cursors: { project_a: 12 }, recent_has_more: { project_a: false } };

    expect(projectActivityBadges(before, { project_a: 11 })).toEqual({ project_a: { active: 1, attention: 0, unread: false, failed: false } });
    expect(activityFailureForInvocation(stalled, 'invocation_a')).toBe('provider route unavailable');
    expect(attentionActivityTransitions(before, after)).toEqual([stalled]);
  });

  it('refuses to replace a newer activity projection with an older overlapping refresh', () => {
    const current: InvocationActivitySnapshot = { items: [activity({ last_event_seq: 14 })], project_cursors: { project_a: 14 }, activity_cursors: { project_a: 14 }, recent_has_more: { project_a: false } };
    const stale: InvocationActivitySnapshot = { items: [activity({ last_event_seq: 11 })], project_cursors: { project_a: 11 }, activity_cursors: { project_a: 11 }, recent_has_more: { project_a: false } };

    expect(mergeActivitySnapshots(current, stale)).toEqual(current);
  });
});
