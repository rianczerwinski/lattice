<script lang="ts">
  import { onDestroy } from 'svelte';
  import { api } from '../api';
  import type { Invocation } from '../types';
  import InvocationEvidence from './InvocationEvidence.svelte';

  export let invocationId: string | null;
  let selected: Invocation | null = null;
  let error = '';
  let loading = false;
  let generation = 0;
  async function load(id: string | null) {
    const current = ++generation;
    selected = null;
    error = '';
    loading = Boolean(id);
    if (!id) return;
    try {
      const result = await api<Invocation>(`/api/v1/invocations/${encodeURIComponent(id)}`);
      if (current === generation) selected = result;
    } catch (cause: unknown) {
      if (current === generation) error = cause instanceof Error ? cause.message : String(cause);
    } finally {
      if (current === generation) loading = false;
    }
  }
  $: void load(invocationId);
  onDestroy(() => { generation++; });
</script>

<aside class="inspector" aria-label="Conversation execution context">
  <div class="section-label">Context truth</div>
  {#if loading}<p role="status">Loading selected execution…</p>
  {:else if error}<p role="alert" class="surface-failure">Selected execution unavailable: {error}</p><button on:click={()=>load(invocationId)}>Retry inspection</button>
  {:else if selected}
    <p>Execution <code>{selected.invocation_id}</code></p>
    <InvocationEvidence {selected} />
    {#each selected.artifacts as artifact}<a href={`/api/v1/artifacts/${encodeURIComponent(artifact.artifact_id)}`}>Retained artifact {artifact.artifact_id}</a>{/each}
  {:else}<p class="muted">This conversation has no execution to inspect yet.</p>{/if}
</aside>
