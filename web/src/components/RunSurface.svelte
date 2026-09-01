<script lang="ts">
  import type { DurableRun, DurableRunArtifact, DurableRunEvent, DurableRunState, DurableRunUnitState } from '../types';

  export let runs: DurableRun[];
  export let run: DurableRun | null;
  export let events: DurableRunEvent[];
  export let loading: boolean;
  export let error: string;
  export let working: boolean;
  export let canStart: boolean;
  export let canResume: boolean;
  export let controlBlocker: string;
  export let onSelect: (runId: string) => void | Promise<void>;
  export let onRefresh: () => void | Promise<void>;
  export let onStart: () => void | Promise<void>;
  export let onStop: () => void | Promise<void>;
  export let onResume: () => void | Promise<void>;
  export let onUseArtifact: (artifactId: string) => void | Promise<void>;

  const statusLabels: Record<DurableRunState, string> = {
    declared: 'Prepared',
    admitted: 'Ready to start',
    running: 'Working',
    waiting: 'Waiting',
    stopping: 'Stopping at a safe boundary',
    stopped: 'Stopped and resumable',
    failed: 'Needs attention',
    completed: 'Complete',
  };

  const terminalStates = new Set<DurableRunState>(['stopped', 'failed', 'completed']);
  const formatBytes = (value: number): string => {
    if (value < 1024) return `${value} B`;
    if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
    if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
    return `${(value / 1024 ** 3).toFixed(2)} GiB`;
  };
  const numberValue = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null;
  const stringValue = (value: unknown): string | null => typeof value === 'string' && value.length ? value : null;
  const recordValue = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const stringArray = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
  const count = (state: DurableRunUnitState): number => run?.unit_counts[state] ?? 0;
  const artifactHref = (artifact: DurableRunArtifact): string => `/api/v1/run-artifacts/${encodeURIComponent(artifact.artifact_id)}/content`;
  const logHref = (attemptId: string, stream: 'stdout' | 'stderr'): string => `/api/v1/runs/${encodeURIComponent(run?.run_id ?? '')}/attempts/${encodeURIComponent(attemptId)}/logs/${stream}`;
  const formatDuration = (seconds: number): string => {
    if (seconds < 60) return `${Math.max(1, Math.round(seconds))} sec`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
    return `${(seconds / 3600).toFixed(seconds < 10 * 3600 ? 1 : 0)} hr`;
  };
  const resourceLabel = (key: string): string => ({
    cpu_seconds: 'CPU',
    wall_seconds: 'Elapsed',
    peak_rss_bytes: 'Peak memory',
    disk_bytes: 'Run storage',
    free_bytes: 'Disk free',
    input_audio_bytes: 'Audio read',
    output_bytes: 'Output',
  })[key] ?? key.replaceAll('_', ' ');
  const resourceValue = (key: string, value: unknown): string => {
    if (typeof value !== 'number' || !Number.isFinite(value)) return String(value ?? 'unknown');
    if (key.endsWith('_bytes')) return formatBytes(value);
    if (key.endsWith('_seconds')) return formatDuration(value);
    return value.toLocaleString();
  };

  $: latestProgress = [...events].reverse().find((event) => event.event_type === 'durable-run.progress') ?? null;
  $: latestResource = [...events].reverse().find((event) => event.event_type === 'durable-run.resource') ?? null;
  $: progressCompleted = count('completed');
  $: progressTotal = run?.units.length ?? 0;
  $: progressMessage = stringValue(latestProgress?.payload.message);
  $: latestUnitStartedSequence = [...events].reverse().find((event) => event.event_type === 'durable-run-unit.started')?.seq ?? 0;
  $: latestSafeProgressSequence = [...events].reverse().find((event) => event.event_type === 'durable-run.progress' && event.payload.safe_to_stop === true)?.seq ?? 0;
  $: safeToStop = run?.state === 'waiting' || run?.state === 'stopped' || run?.state === 'completed' || run?.state === 'failed' || count('running') === 0 || latestSafeProgressSequence > latestUnitStartedSequence;
  $: remaining = count('pending') + count('eligible') + count('running');
  $: latestAttempt = run?.attempts[run.attempts.length - 1] ?? null;
  $: latestReport = [...(run?.artifacts ?? [])].reverse().find((artifact) => artifact.artifact_class === 'durable_report' && artifact.state === 'present') ?? null;
  $: currentAttemptProgress = latestProgress ? events.filter((event) => event.event_type === 'durable-run.progress' && event.subject_id === latestProgress?.subject_id && numberValue(event.payload.completed) !== null) : [];
  $: progressRate = (() => {
    if (currentAttemptProgress.length < 2) return null;
    const last = currentAttemptProgress[currentAttemptProgress.length - 1];
    const lastCompleted = numberValue(last.payload.completed);
    const lastNs = numberValue(last.payload.monotonic_ns);
    const first = currentAttemptProgress.find((event) => {
      const completed = numberValue(event.payload.completed);
      const ns = numberValue(event.payload.monotonic_ns);
      return completed !== null && lastCompleted !== null && completed < lastCompleted && ns !== null && lastNs !== null && ns < lastNs;
    });
    if (!first || lastCompleted === null || lastNs === null) return null;
    const firstCompleted = numberValue(first.payload.completed);
    const firstNs = numberValue(first.payload.monotonic_ns);
    if (firstCompleted === null || firstNs === null || lastNs <= firstNs) return null;
    return (lastCompleted - firstCompleted) / ((lastNs - firstNs) / 1e9);
  })();
  $: etaText = progressRate && remaining > 0
    ? `${formatDuration(remaining / progressRate)} at ${(progressRate * 60).toFixed(2)} units/min; rough estimate from this attempt`
    : remaining === 0
      ? 'No work remains.'
      : 'Not enough completed work in this attempt for an honest estimate.';
  $: resourceMetrics = recordValue(latestResource?.payload.metrics);
  $: launchProfile = recordValue(run?.declaration.local_launch_profile);
  $: launchCommand = stringArray(launchProfile.command);
  $: launchEnvironment = recordValue(launchProfile.environment);
  $: launchArtifactRoots = stringArray(launchProfile.artifact_roots);
  $: blocker = run?.state === 'waiting'
    ? stringValue([...events].reverse().find((event) => event.event_type === 'durable-run.waiting')?.payload.reason) ?? 'The worker is waiting for an external condition.'
    : run?.state === 'failed'
      ? latestAttempt?.error_text ?? latestAttempt?.failure_kind ?? 'The last attempt ended without a usable result.'
      : count('refused') > 0
        ? run?.units.find((unit) => unit.state === 'refused')?.error_text ?? 'One or more units refused their inputs.'
        : run?.state === 'declared'
          ? 'The exact work is declared but has not been admitted.'
          : run?.state === 'admitted'
            ? 'The run is admitted and waiting for an operator start.'
            : run?.state === 'stopped'
              ? 'The run is stopped. Completed units are retained; resume may repeat only the interrupted unit.'
              : null;
</script>

{#if runs.length || loading || error}
  <section class="run-surface" aria-label="Durable work">
    <div class="run-surface-heading">
      <div><span class="eyebrow">Durable work</span><h3>{run ? run.name : 'Project runs'}</h3></div>
      <div class="run-switcher">
        {#each runs as item}
          <button class:chosen={item.run_id === run?.run_id} title={item.name} on:click={() => onSelect(item.run_id)}>{statusLabels[item.state]} · {item.run_id.slice(-6)}</button>
        {/each}
        <button class="quiet" disabled={loading} on:click={onRefresh}>{loading ? 'Refreshing…' : 'Refresh'}</button>
      </div>
    </div>

    {#if error}
      <p class="surface-failure">Run state unavailable: {error}</p>
    {:else if run}
      <div class="run-summary">
        <div class="run-status" data-state={run.state}><span>{statusLabels[run.state]}</span><strong>{progressCompleted} / {progressTotal || run.units.length} units</strong><small>{progressMessage ?? `${remaining} remaining · ${count('failed')} failed · ${count('refused')} refused`}</small></div>
        <div><span>Latest result</span>{#if latestReport}<strong>{latestReport.name}</strong><small>{formatBytes(latestReport.byte_count)} · retained report</small>{:else}<strong>{count('completed') ? `${count('completed')} atomic result${count('completed') === 1 ? '' : 's'} retained` : 'No completed result yet'}</strong><small>{terminalStates.has(run.state) ? 'No durable report was published.' : 'Results appear here as units finish.'}</small>{/if}</div>
        <div><span>Interruption</span><strong>{safeToStop ? 'Safe at the current boundary' : 'Active unit must reach its boundary'}</strong><small>{count('running') ? `${count('running')} active unit may repeat after interruption.` : 'No completed unit will repeat.'}</small></div>
        <div><span>Storage</span><strong>{formatBytes(Object.values(run.present_artifact_bytes).reduce((total, value) => total + value, 0))} retained</strong><small>{formatBytes(run.present_artifact_bytes.disposable ?? 0)} currently disposable</small></div>
      </div>

      {#if blocker}<p class="run-blocker"><strong>What prevents progress</strong>{blocker}</p>{/if}
      <p class="run-resource"><strong>Rate and ETA</strong>{etaText}</p>
      {#if latestResource}<p class="run-resource"><strong>Latest resource reading</strong>{#each Object.entries(resourceMetrics) as [key, value]}<span>{resourceLabel(key)}: {resourceValue(key, value)}</span>{/each}</p>{/if}

      <div class="run-actions">
        {#if ['declared', 'admitted'].includes(run.state)}<button disabled={working || !canStart} on:click={onStart}>Start</button>{/if}
        {#if ['running', 'waiting'].includes(run.state)}<button class="quiet" disabled={working} on:click={onStop}>{safeToStop ? 'Stop now' : 'Stop at unit boundary'}</button>{/if}
        {#if ['stopped', 'failed'].includes(run.state)}<button disabled={working || !canResume} on:click={onResume}>Resume</button>{/if}
        {#if controlBlocker && ((['declared', 'admitted'].includes(run.state) && !canStart) || (['stopped', 'failed'].includes(run.state) && !canResume))}<small>{controlBlocker}</small>{/if}
      </div>

      <details class="run-drawer">
        <summary>Units, attempts, evidence, and exact bindings</summary>
        <div class="run-drawer-grid">
          <section><h4>Units</h4>{#each run.units as unit}<div class="run-row"><code>{unit.unit_key}</code><span>{unit.state}</span>{#if unit.failure_kind}<small>{unit.failure_kind}</small>{/if}{#if unit.error_text}<small>{unit.error_text}</small>{/if}</div>{/each}</section>
          <section><h4>Attempts</h4>{#each run.attempts as attempt}<div class="run-row"><code>#{attempt.attempt_number} · {attempt.attempt_id.slice(-8)}</code><span>{attempt.state}</span>{#if attempt.failure_kind}<small>{attempt.failure_kind}</small>{/if}{#if attempt.error_text}<small>{attempt.error_text}</small>{/if}<div class="run-row-actions"><a href={logHref(attempt.attempt_id, 'stdout')} target="_blank" rel="noreferrer">stdout</a><a href={logHref(attempt.attempt_id, 'stderr')} target="_blank" rel="noreferrer">stderr</a></div></div>{/each}</section>
            <section><h4>Artifacts</h4>{#if run.artifacts.length}{#each run.artifacts as artifact}<div class="run-row"><div><code>{artifact.artifact_class}</code><strong>{artifact.name}</strong></div><span>{formatBytes(artifact.byte_count)} · {artifact.state}{artifact.sensitive ? ' · private' : ''}</span>{#if artifact.state === 'present'}<div class="run-row-actions"><a href={artifactHref(artifact)} target="_blank" rel="noreferrer">Open</a>{#if artifact.media_type.startsWith('text/') || artifact.media_type.includes('json')}<button class="quiet" on:click={() => onUseArtifact(artifact.artifact_id)}>Use as context</button>{/if}</div>{/if}</div>{/each}{:else}<p class="muted">No artifacts have been admitted.</p>{/if}</section>
          <section><h4>Bindings and proof limits</h4><div class="run-row"><code>declaration</code><span>{run.declaration_sha256}</span></div><div class="run-row"><code>unit manifest</code><span>{run.unit_manifest_sha256}</span></div><div class="run-row"><code>executable</code><span>{stringValue(run.declaration.executable_ref) ?? 'not declared'}</span><small>{stringValue(run.declaration.executable_sha256) ?? ''}</small></div><div class="run-row"><code>code</code><span>{stringValue(run.declaration.code_revision) ?? 'not declared'}</span></div><div class="run-row"><code>configuration</code><span>{stringValue(run.declaration.configuration_sha256) ?? 'not declared'}</span></div><div class="run-row"><code>inputs</code><span>{stringValue(run.declaration.inputs_sha256) ?? 'not declared'}</span></div><div class="run-row"><code>environment</code><span>{stringValue(run.declaration.environment_profile_sha256) ?? 'not declared'}</span>{#if Object.keys(launchEnvironment).length}<small>Persisted non-secret keys: {Object.keys(launchEnvironment).sort().join(', ')}</small>{/if}</div><div class="run-row"><code>executor</code><span>{stringValue(run.declaration.executor_identity) ?? 'not declared'}</span><small>{stringValue(run.declaration.executor_sha256) ?? ''}</small></div>{#if launchCommand.length}<div class="run-row"><code>command</code><span>{launchCommand.join(' ')}</span></div>{/if}{#if stringValue(launchProfile.cwd)}<div class="run-row"><code>working directory</code><span>{stringValue(launchProfile.cwd)}</span></div>{/if}{#if launchArtifactRoots.length}<div class="run-row"><code>artifact roots</code><span>{launchArtifactRoots.join(', ')}</span></div>{/if}<div class="run-row"><code>resume</code><span>{run.resume_strategy}</span></div><div class="run-row"><code>secret boundary</code><span>{stringValue(launchProfile.environment_policy) ?? 'Legacy declaration; Lattice cannot reconstruct its launch material.'}</span></div></section>
          <section class="run-events"><h4>Structured events</h4>{#each events.slice(-100).reverse() as event}<div class="run-row"><code>{event.seq} · {event.event_type}</code><span>{event.created_at}</span></div>{/each}</section>
        </div>
      </details>
    {/if}
  </section>
{/if}
