<script lang="ts">
  import type { ContextAction } from '../contextActions';
  import type { Episode, Invocation } from '../types';

  export let selected: Invocation | null;
  export let activeAttemptIds: string[];
  export let liveProgress: string;
  export let liveProgressState: string;
  export let responseHtml: string;
  export let working: boolean;
  export let selectedNativeEligible: boolean;
  export let currentEpisode: Episode | null;
  export let parentEpisode: Episode | null;
  export let selectedExcerpt: { text: string; startByte: number; endByte: number } | null;
  export let selectionProofLimit: string;
  export let terminalFailure: (invocation: Invocation) => string;
  export let onCancelActive: () => void | Promise<void>;
  export let onBeginSideQuest: (mode?: 'anthropic' | 'openai' | 'both') => unknown | Promise<unknown>;
  export let onGraftResponse: (action: ContextAction) => void | Promise<void>;
  export let onConfigureNative: (mode: 'resume' | 'fork') => void;
  export let onReturnToParent: () => void;
  export let onGraftExcerpt: (action: ContextAction) => void | Promise<void>;
</script>

<section class="response-card" class:empty={!selected?.output_block && !activeAttemptIds.length}>
  {#if selected}
    <div class="response-meta"><span>{selected.state}</span><code>{selected.invocation_id.slice(-8)}</code></div>
    {#if activeAttemptIds.length}
      <div class="live-progress">
        <div><strong>{liveProgressState}</strong><button class="quiet" on:click={onCancelActive}>Cancel running routes</button></div>
        <pre>{liveProgress || 'Waiting for the first observable provider bytes…'}</pre>
      </div>
    {/if}
    {#if selected.output_block}
      <article>{@html responseHtml}</article>
    {:else if ['failed', 'interrupted'].includes(selected.state)}
      <p class="terminal-failure">Invocation ended {selected.state}: {terminalFailure(selected)}</p>
    {:else}
      <p class="muted">This invocation has not reached a terminal response.</p>
    {/if}
    <div class="response-actions">
      <button class="quiet" on:click={() => onBeginSideQuest()}>Side quest</button>
      <button class="quiet" on:click={() => onBeginSideQuest('anthropic')}>Ask Claude</button>
      <button class="quiet" on:click={() => onBeginSideQuest('openai')}>Ask Codex</button>
      <button class="quiet" on:click={() => onBeginSideQuest('both')}>Ask both</button>
      {#if selected.output_block_id}
        <details class="context-actions">
          <summary>Use exact response</summary>
          <div>
            <button class="quiet" disabled={working} on:click={() => onGraftResponse('side-quest')}>Side quest</button>
            <button class="quiet" disabled={working} on:click={() => onGraftResponse('ask-claude')}>Ask Claude</button>
            <button class="quiet" disabled={working} on:click={() => onGraftResponse('ask-codex')}>Ask Codex</button>
            <button class="quiet" disabled={working} on:click={() => onGraftResponse('ask-both')}>Ask both</button>
            <button class="quiet" disabled={working} on:click={() => onGraftResponse('bring-here')}>Bring here</button>
            <button class="quiet" disabled={working} on:click={() => onGraftResponse('compare')}>Compare</button>
          </div>
        </details>
      {/if}
      {#if selectedNativeEligible}
        <button class="quiet native" on:click={() => onConfigureNative('resume')}>Continue native session</button>
        <button class="quiet native" on:click={() => onConfigureNative('fork')}>Fork native session</button>
      {/if}
      {#if currentEpisode?.parent_episode_id}
        <button class="quiet return" on:click={onReturnToParent}>Return to {parentEpisode?.name ?? 'parent trail'}</button>
      {/if}
      {#if selectedExcerpt}
        <details class="context-actions selected-text">
          <summary>Use “{selectedExcerpt.text.slice(0, 36)}{selectedExcerpt.text.length > 36 ? '…' : ''}”</summary>
          <div>
            <button class="quiet" disabled={working} on:click={() => onGraftExcerpt('side-quest')}>Side quest</button>
            <button class="quiet" disabled={working} on:click={() => onGraftExcerpt('ask-claude')}>Ask Claude</button>
            <button class="quiet" disabled={working} on:click={() => onGraftExcerpt('ask-codex')}>Ask Codex</button>
            <button class="quiet" disabled={working} on:click={() => onGraftExcerpt('ask-both')}>Ask both</button>
            <button class="quiet" disabled={working} on:click={() => onGraftExcerpt('bring-here')}>Bring here</button>
            <button class="quiet" disabled={working} on:click={() => onGraftExcerpt('compare')}>Compare</button>
          </div>
        </details>
      {/if}
      {#if selectionProofLimit}<small class="selection-proof-limit">{selectionProofLimit}</small>{/if}
    </div>
  {:else}
    <div class="empty-call"><span>Begin here</span><h3>Say what should happen next.</h3><p>Lattice will show the exact context before anything is sent.</p></div>
  {/if}
</section>
