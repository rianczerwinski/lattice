import { describe, expect, it } from 'vitest';
import { recoveryNotice } from './recoveryStatus';

describe('recovery surface', () => {
  it('stays quiet when nothing was interrupted', () => {
    expect(recoveryNotice({ startup_recovery_failures: [], durable_run_startup_reconciliation: { adopted_attempt_ids: [], orphaned_attempt_ids: [], processes_proven_dead: [] } })).toBeNull();
  });

  it('names what survived and what must be repeated', () => {
    const notice = recoveryNotice({
      startup_recovery_failures: [{ invocation_id: 'invocation_abcd1234', detail: 'Claude lease expired during restart.' }],
      durable_run_startup_reconciliation: {
        adopted_attempt_ids: ['attempt_alive1234'],
        orphaned_attempt_ids: ['attempt_dead5678'],
        processes_proven_dead: [],
      },
    });
    expect(notice?.title).toBe('Work recovered after an interruption');
    expect(notice?.survived.some((item) => item.includes('live1234'))).toBe(true);
    expect(notice?.mustRepeat).toEqual([
      'Claude lease expired during restart.',
      'Durable run attempt dead5678 was orphaned; resume to repeat at most the active unit.',
    ]);
  });
});
