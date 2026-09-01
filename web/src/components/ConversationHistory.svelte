<script lang="ts">
  import type { Project } from '../types';

  export let project: Project;
  export let selectedInvocationId: string | null;
  export let selectedParents: Set<string>;
  export let working: boolean;
  export let onLoadOlder: () => void | Promise<void>;
  export let onToggleParent: (invocationId: string) => void;
  export let onInspect: (invocationId: string) => void | Promise<void>;
</script>

<section class="history">
  <div class="section-label">Lattice trail</div>
  {#if project.invocation_page?.has_older}<button class="quiet load-older" disabled={working} on:click={onLoadOlder}>Load older history</button>{/if}
  {#each [...(project.invocations ?? [])].reverse() as item}
    <div class="history-row" class:selected={selectedInvocationId === item.invocation_id}>
      <input type="checkbox" checked={selectedParents.has(item.invocation_id)} on:change={() => onToggleParent(item.invocation_id)} aria-label="Select as parent" />
      <button on:click={() => onInspect(item.invocation_id)}><span>{item.state}</span><code>{item.invocation_id.slice(-8)}</code><time>{item.created_at}</time></button>
    </div>
  {/each}
</section>
