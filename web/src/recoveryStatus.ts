export type RecoveryHealth = {
  startup_recovery_failures?: Array<{ invocation_id?: string; detail?: string }>;
  durable_run_startup_reconciliation?: {
    adopted_attempt_ids?: string[];
    orphaned_attempt_ids?: string[];
    processes_proven_dead?: string[];
  };
};

export type RecoveryNotice = {
  title: string;
  survived: string[];
  mustRepeat: string[];
};

export function recoveryNotice(health: RecoveryHealth): RecoveryNotice | null {
  const failures = health.startup_recovery_failures ?? [];
  const reconciliation = health.durable_run_startup_reconciliation ?? {};
  const adopted = reconciliation.adopted_attempt_ids ?? [];
  const orphaned = reconciliation.orphaned_attempt_ids ?? [];
  const dead = reconciliation.processes_proven_dead ?? [];
  if (!failures.length && !adopted.length && !orphaned.length && !dead.length) return null;
  const survived = [
    ...adopted.map((id) => `Durable run attempt ${id.slice(-8)} is still owned and can continue.`),
  ];
  const mustRepeat = [
    ...failures.map((item) => item.detail || `Provider work ${item.invocation_id ?? 'unknown'} did not recover; send it again.`),
    ...orphaned.map((id) => `Durable run attempt ${id.slice(-8)} was orphaned; resume to repeat at most the active unit.`),
    ...dead.map((id) => `Process ${id} is gone; Lattice will not pretend it is still running.`),
  ];
  if (!survived.length) survived.push('Sealed conversation history, manifests, and stored artifacts remain in place.');
  return {
    title: mustRepeat.length ? 'Work recovered after an interruption' : 'Background work was reattached',
    survived,
    mustRepeat,
  };
}
