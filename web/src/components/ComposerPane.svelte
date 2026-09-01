<script lang="ts">
  import type { BatchPreview, Invocation, Project, Route } from '../types';

  export let project: Project;
  export let installedRoutes: Route[];
  export let routeChoices: Route[];
  export let routeMode: 'anthropic' | 'openai' | 'both';
  export let executionProfile: 'compiled-fresh' | 'workspace-agent' | 'native-continuation';
  export let continuationMode: 'resume' | 'fork';
  export let continuationSource: Invocation | null;
  export let effort: 'low' | 'medium' | 'high' | 'xhigh' | 'max';
  export let modelOverrides: Record<string, string>;
  export let workspacePath: string;
  export let workspaceAuthority: 'read-only' | 'workspace-write';
  export let claudeShell: boolean;
  export let prompt: string;
  export let selectedParents: Set<string>;
  export let explicitSelections: Array<Record<string, unknown>>;
  export let selectedSession: string;
  export let working: boolean;
  export let preview: BatchPreview | null;
  export let onChooseRoute: (mode: 'anthropic' | 'openai' | 'both') => void;
  export let onChooseFreshProfile: () => void;
  export let onChooseWorkspaceProfile: () => void;
  export let onResetPreview: () => void;
  export let onSetModelOverride: (provider: string, value: string) => void;
  export let onCompareSelected: () => void | Promise<void>;
  export let onPrepareReciprocalCrossFeed: () => void | Promise<void>;
  export let onExtractSelectedPath: () => void | Promise<void>;
  export let onPrepare: (executeWhenClear?: boolean) => void | Promise<void>;
  export let onExecutePreparedBatch: (confirmationHashes: string[]) => void | Promise<void>;
</script>

<section class="composer">
  <div class="composer-top">
    <div class="route-chips" aria-label="Model route">
      <button class:active={routeMode === 'anthropic'} disabled={!installedRoutes.some((route) => route.provider_code === 'anthropic')} on:click={() => onChooseRoute('anthropic')}>Claude</button>
      <button class:active={routeMode === 'openai'} disabled={!installedRoutes.some((route) => route.provider_code === 'openai')} on:click={() => onChooseRoute('openai')}>Codex</button>
      <button class:active={routeMode === 'both'} disabled={!['anthropic', 'openai'].every((provider) => installedRoutes.some((route) => route.provider_code === provider))} on:click={() => onChooseRoute('both')}>both</button>
    </div>
    <span class="parent-count">{selectedParents.size ? `${selectedParents.size} parent${selectedParents.size === 1 ? '' : 's'}` : 'fresh edge'}</span>
  </div>
  <div class="session-strip">
    <div class="profile-chips" aria-label="Execution profile">
      <button class:active={executionProfile === 'compiled-fresh'} on:click={onChooseFreshProfile}>fresh</button>
      <button class:active={executionProfile === 'workspace-agent'} disabled={routeChoices.some((route) => !route.qualified_profiles.includes('workspace-agent'))} on:click={onChooseWorkspaceProfile}>workspace</button>
      {#if executionProfile === 'native-continuation'}<span>{continuationMode} · {continuationSource?.invocation_id.slice(-8)}</span>{/if}
    </div>
    <label>effort<select bind:value={effort} on:change={onResetPreview}><option>low</option><option>medium</option><option>high</option><option>xhigh</option><option>max</option></select></label>
    <details class="advanced-models"><summary>models</summary>{#each routeChoices as route}<label>{route.provider_code}<input value={modelOverrides[route.provider_code] ?? route.model_code} on:input={(event) => onSetModelOverride(route.provider_code, event.currentTarget.value)} /></label>{/each}</details>
    {#if executionProfile === 'workspace-agent'}
      <label class="workspace-path">workspace<input bind:value={workspacePath} on:input={onResetPreview} placeholder="/absolute/project/folder" /></label>
      <label>authority<select bind:value={workspaceAuthority} on:change={onResetPreview}><option value="read-only">read only</option><option value="workspace-write">edit workspace</option></select></label>
      {#if routeChoices.some((route) => route.provider_code === 'anthropic')}<label class="shell-toggle"><input type="checkbox" bind:checked={claudeShell} on:change={onResetPreview} />Claude shell + provider network</label>{/if}
    {/if}
    <small>{executionProfile === 'compiled-fresh' ? 'Exact context, no tools, no persistent provider session.' : executionProfile === 'workspace-agent' ? 'Starts a recorded provider session with the authority shown here.' : `Uses only ${continuationSource?.invocation_id.slice(-8)} under its exact prior authority.`}</small>
  </div>
  <textarea bind:value={prompt} rows="5" placeholder={selectedParents.size > 1 ? 'Bring these branches together…' : selectedParents.size === 1 ? 'Continue here or take a side quest…' : 'Say what should happen next…'} on:input={onResetPreview}></textarea>
  {#if explicitSelections.length}<div class="graft-strip">{explicitSelections.length} exact graft{explicitSelections.length === 1 ? '' : 's'} selected</div>{/if}
  <div class="composer-actions">
    {#if selectedParents.size === 2}<button class="quiet" on:click={onCompareSelected}>Compare branches</button>{/if}
    {#if selectedParents.size === 2}<button class="quiet" disabled={working || !prompt.trim()} on:click={onPrepareReciprocalCrossFeed}>Reciprocal cross-feed</button>{/if}
    {#if selectedParents.size}<button class="quiet" disabled={working} on:click={onExtractSelectedPath}>Extract reusable path</button>{/if}
    <details class="role-binding"><summary>{selectedSession ? 'Role-bound' : 'Direct'}</summary><select bind:value={selectedSession} on:change={onResetPreview} aria-label="Optional role session"><option value="">Direct conversation</option>{#each project.sessions ?? [] as session}<option value={session.role_session_id}>{session.label}</option>{/each}</select></details>
    <button class="quiet" disabled={working || !prompt.trim()} on:click={() => onPrepare(false)}>Inspect context</button>
    {#if preview?.confirmation_reasons.length}<button class="send confirm" disabled={working} on:click={() => onExecutePreparedBatch(preview?.confirmation_reasons.map((item) => item.reason_sha256) ?? [])}>{working ? 'Working…' : 'Confirm & send'}</button>{:else if preview}<button class="send" disabled={working} on:click={() => onExecutePreparedBatch([])}>{working ? 'Working…' : `Send to ${routeMode}`}</button>{:else}<button class="send" disabled={working || !prompt.trim() || !routeChoices.length} on:click={() => onPrepare(true)}>{working ? 'Working…' : `Send to ${routeMode}`}</button>{/if}
  </div>
</section>
