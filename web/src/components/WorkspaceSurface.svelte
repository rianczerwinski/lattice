<script lang="ts">
  import type { WorkspaceDiff, WorkspaceDirectory, WorkspaceFileView, WorkspaceReviewStatus, WorkspaceSearchHit } from '../types';

  export let workspacePath: string;
  export let directory: WorkspaceDirectory | null;
  export let file: WorkspaceFileView | null;
  export let searchHits: WorkspaceSearchHit[];
  export let searchOutcome: { query: string; mode: 'text' | 'path' | 'symbol'; state: 'success' | 'failure' } | null;
  export let reviewStatus: WorkspaceReviewStatus | null;
  export let diff: WorkspaceDiff | null;
  export let loading: string;
  export let error: string;
  export let selectedFileReceipt: string;
  export let searchQuery: string;
  export let searchMode: 'text' | 'path' | 'symbol';
  export let diffView: 'unstaged' | 'staged' | 'revision';
  export let diffRevision: string;
  export let onOpenDirectory: (path?: string) => void | Promise<void>;
  export let onOpenFile: (path: string, line?: number | null) => void | Promise<void>;
  export let onSearch: () => void | Promise<void>;
  export let onRefreshReview: () => void | Promise<void>;
  export let onLoadDiff: () => void | Promise<void>;
  export let onUseFile: () => void | Promise<void>;

  const parentPath = (path: string): string => path === '.' ? '' : path.split('/').slice(0, -1).join('/');
  const formatBytes = (value: number | null): string => value === null ? '' : value < 1024 ? `${value} B` : value < 1024 ** 2 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`;
  $: emptySearchSucceeded = searchOutcome?.state === 'success' && searchOutcome.query === searchQuery.trim() && searchOutcome.mode === searchMode && searchHits.length === 0;
</script>

<details class="workspace-surface">
  <summary><span>Workspace</span><strong>{workspacePath || 'Choose a repository in the composer'}</strong>{#if reviewStatus}<small>{reviewStatus.branch ?? 'detached'} · {reviewStatus.clean ? 'clean' : `${reviewStatus.changes.length} changed`}</small>{/if}</summary>
  {#if !workspacePath}
    <p class="muted">Choose the workspace profile and an absolute repository path below. Lattice will inspect it without granting write authority.</p>
  {:else}
    <div class="workspace-toolbar">
      <button class="quiet" disabled={Boolean(loading)} on:click={() => onOpenDirectory(directory?.path ?? '')}>{loading === 'directory' ? 'Opening…' : 'Browse'}</button>
      <button class="quiet" disabled={Boolean(loading)} on:click={onRefreshReview}>{loading === 'review' ? 'Reading…' : 'Status'}</button>
      <code>{workspacePath}</code>
    </div>
    {#if error}<p class="surface-failure">Workspace unavailable: {error}</p>{/if}

    <div class="workspace-search">
      <select bind:value={searchMode} aria-label="Workspace search mode"><option value="text">text</option><option value="symbol">symbol</option><option value="path">path</option></select>
      <input bind:value={searchQuery} placeholder={searchMode === 'symbol' ? 'Exact symbol name' : searchMode === 'path' ? 'Part of a path' : 'Exact text'} on:keydown={(event) => event.key === 'Enter' && onSearch()} />
      <button class="quiet" disabled={Boolean(loading) || !searchQuery.trim()} on:click={onSearch}>{loading === 'search' ? 'Searching…' : 'Find'}</button>
    </div>

    {#if searchHits.length}
      <div class="workspace-hits">
        {#each searchHits as hit}<button on:click={() => onOpenFile(hit.path, hit.line)}><code>{hit.path}{hit.line ? `:${hit.line}` : ''}</code><span>{hit.preview}</span></button>{/each}
      </div>
    {:else if emptySearchSucceeded}
      <p class="muted">No matches for this search.</p>
    {/if}

    <div class="workspace-browser">
      <section>
        <div class="workspace-pathbar"><strong>{directory?.path ?? '.'}</strong>{#if directory && directory.path !== '.'}<button class="quiet" on:click={() => onOpenDirectory(parentPath(directory?.path ?? '.'))}>up</button>{/if}</div>
        {#if directory}
          <div class="workspace-entries">
            {#each directory.entries as entry}
              <button disabled={entry.kind === 'symlink' || entry.kind === 'other'} on:click={() => entry.kind === 'directory' ? onOpenDirectory(entry.path) : onOpenFile(entry.path)}><span>{entry.kind === 'directory' ? '▸' : entry.kind === 'file' ? '·' : '×'}</span><code>{entry.name}</code><small>{formatBytes(entry.byte_count)}</small></button>
            {/each}
            {#if !directory.entries.length}<p class="muted">This folder is empty.</p>{/if}
            {#if directory.truncated}<p class="muted">Only the first 500 entries are shown. Search for a narrower path.</p>{/if}
          </div>
        {:else}<p class="muted">Open the repository to navigate its files.</p>{/if}
      </section>
      <section class="workspace-file">
        {#if file}
          <div class="workspace-filehead"><div><strong>{file.path}</strong><small>lines {file.start_line}–{file.end_line} of {file.total_lines} · {formatBytes(file.byte_count)} · {file.content_sha256.slice(0, 12)}</small></div><button class="quiet" disabled={Boolean(loading)} on:click={onUseFile}>{loading === 'context' ? 'Capturing…' : 'Use exact file'}</button></div>
          {#if selectedFileReceipt}<small class="workspace-receipt">Selected as context · {selectedFileReceipt.slice(0, 12)}</small>{/if}
          <pre>{file.content}</pre>
        {:else}<p class="muted">Select a text file or search result.</p>{/if}
      </section>
    </div>

    <details class="workspace-review">
      <summary>Review repository changes</summary>
      <div class="workspace-review-controls"><select bind:value={diffView}><option value="unstaged">unstaged</option><option value="staged">staged</option><option value="revision">revision</option></select>{#if diffView === 'revision'}<input bind:value={diffRevision} placeholder="commit or branch" />{/if}<button class="quiet" disabled={Boolean(loading) || (diffView === 'revision' && !diffRevision.trim())} on:click={onLoadDiff}>{loading === 'diff' ? 'Loading…' : 'Open diff'}</button></div>
      {#if reviewStatus}<div class="workspace-change-list"><strong>{reviewStatus.clean ? 'No local changes' : `${reviewStatus.changes.length} changed path${reviewStatus.changes.length === 1 ? '' : 's'}`}</strong>{#each reviewStatus.changes as change}<code>{change}</code>{/each}</div>{/if}
      {#if diff}<pre>{diff.empty ? 'No changes in this view.' : diff.diff}</pre>{/if}
    </details>
  {/if}
</details>
