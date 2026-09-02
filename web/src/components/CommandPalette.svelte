<script lang="ts">
  import { tick } from 'svelte';
  import { filterPaletteCommands, type PaletteCommand } from '../commandPalette';

  export let open: boolean;
  export let commands: PaletteCommand[];
  export let onClose: () => void;
  export let onChoose: (commandId: string) => void | Promise<void>;
  export let onQuery: (query: string) => void = () => {};

  let query = '';
  let input: HTMLInputElement;
  let activeIndex = 0;
  let wasOpen = false;
  let lastQueried = '';

  $: filtered = filterPaletteCommands(commands, query);
  $: if (query !== lastQueried) {
    lastQueried = query;
    onQuery(query);
  }
  $: if (activeIndex >= filtered.length) activeIndex = Math.max(0, filtered.length - 1);
  $: if (open && !wasOpen) {
    query = '';
    activeIndex = 0;
    void tick().then(() => input?.focus());
  }
  $: wasOpen = open;

  function choose(command: PaletteCommand | undefined) {
    if (!command || command.disabled) return;
    onClose();
    void onChoose(command.id);
  }

  function keydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      activeIndex = Math.min(filtered.length - 1, activeIndex + 1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      activeIndex = Math.max(0, activeIndex - 1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      choose(filtered[activeIndex]);
    }
  }
</script>

{#if open}
  <div class="palette-backdrop" role="presentation" on:mousedown={(event) => event.currentTarget === event.target && onClose()}>
    <div class="command-palette" role="dialog" aria-modal="true" aria-label="Lattice commands" tabindex="-1" on:keydown={keydown}>
      <input bind:this={input} bind:value={query} placeholder="Find a project, conversation, artifact, run, or command…" aria-label="Search commands" />
      <div class="palette-results" role="listbox">
        {#if filtered.length}
          {#each filtered as command, index}
            <button role="option" aria-selected={index === activeIndex} class:active={index === activeIndex} disabled={command.disabled} on:mouseenter={() => activeIndex = index} on:click={() => choose(command)}>
              <span><strong>{command.label}</strong><small>{command.detail}</small></span>
              {#if command.shortcut}<kbd>{command.shortcut}</kbd>{/if}
            </button>
          {/each}
        {:else}
          <p>No matching command.</p>
        {/if}
      </div>
    </div>
  </div>
{/if}
