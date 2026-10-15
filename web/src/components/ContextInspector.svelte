<script lang="ts">
  import InvocationEvidence from './InvocationEvidence.svelte';
  import type { ContextAction } from '../contextActions';
  import type { BatchPreview, ContextModuleCatalogue, ContextModuleSourceKind, Invocation, InvocationSummary, LiveSlice, Project } from '../types';

  type Neighborhood = {
    nodes: InvocationSummary[];
    edges: Array<Record<string, unknown>>;
    coverage: { state: 'complete' | 'partial'; requested_depth: number; completed_depth: number; node_limit: number; edge_limit: number };
  };

  export let preview: BatchPreview | null;
  export let selected: Invocation | null;
  export let working: boolean;
  export let artifactContents: Record<string, string>;
  export let neighborhood: Neighborhood | null;
  export let neighborhoodError: string;
  export let comparison: Record<string, unknown> | null;
  export let selectedSlateSlice: LiveSlice | null;
  export let slateSliceError: string;
  export let contextModuleCatalogueError: string;
  export let contextModuleCatalogue: ContextModuleCatalogue | null;
  export let project: Project | null;
  export let contextModuleId: string;
  export let contextModuleSourceKind: ContextModuleSourceKind;
  export let contextModuleSource: string;
  export let contextModuleMode: 'manual' | 'project_default';
  export let contextModuleReceipt: string;
  export let slateConcern: string;
  export let slateLabel: string;
  export let slateTagReceipt: string;
  export let pinRoleName: string;
  export let pinRoleInstructions: string;
  export let pinRoleSessionLabel: string;
  export let pinRoleReceipt: string;
  export let extractedTemplateId: string;
  export let onLoadArtifact: (artifactId: string) => void | Promise<void>;
  export let onGraftArtifact: (artifactId: string, action: ContextAction) => void | Promise<void>;
  export let onInspect: (invocationId: string) => void | Promise<void>;
  export let onRemoveContextDefault: (moduleId: string) => void | Promise<void>;
  export let onResolveContextModule: () => void | Promise<void>;
  export let onTagSelectedToSlate: (target: 'invocation' | 'response') => void | Promise<void>;
  export let onPinSelectedAsRole: () => void | Promise<void>;
</script>

<aside class="inspector">
  <div class="section-label">Context truth</div>
  {#if preview}
    {@const primaryPreview = preview.members[0]}
    <div class="truth-status" class:valid={!preview.confirmation_reasons.length}>{preview.confirmation_reasons.length ? 'Confirmation required' : 'Ready for one-action send'} · {primaryPreview.preflight.compilation.byte_count} bytes</div>
    {#if preview.confirmation_reasons.length}<h3>Why execution paused</h3>{#each preview.confirmation_reasons as reason}<div class="confirmation-reason"><strong>{reason.code}</strong><pre>{JSON.stringify(reason.detail, null, 2)}</pre></div>{/each}{/if}
    <h3>Next invocation receives</h3>
    <div class="segments">
      {#each primaryPreview.preflight.segments as segment}<div class="segment"><i class={`plane ${segment.plane}`}></i><div><strong>{segment.label}</strong><small>{segment.plane} · {segment.status}</small><code>{segment.sha256.slice(0, 12)}</code></div></div>{/each}
    </div>
    {#if primaryPreview.preflight.omitted.length}<h3>Explicitly absent</h3>{#each primaryPreview.preflight.omitted as segment}<div class="omitted">{segment.label} · {segment.disposition}</div>{/each}{/if}
    {#if primaryPreview.parent_deltas.length}<h3>Delta from selected trail</h3>{#each primaryPreview.parent_deltas as delta}<details><summary>{delta.invocation_id.slice(-8)} · +{delta.added.length} / −{delta.removed.length} / ↻{delta.transformed.length} / ∅{delta.omitted.length} / ?{delta.provider_opaque.length} · {delta.unchanged_count} unchanged</summary>{#each delta.added as segment}<div class="segment delta-added"><i class={`plane ${segment.plane}`}></i><div><strong>+ {segment.label}</strong><small>{segment.plane} · added</small></div></div>{/each}{#each delta.transformed as segment}<div class="segment delta-transformed"><i class={`plane ${segment.plane}`}></i><div><strong>↻ {segment.label}</strong><small>{segment.plane} · transformed with exact receipt</small></div></div>{/each}{#each delta.omitted as segment}<div class="omitted">∅ {segment.label} · {segment.disposition}</div>{/each}{#each delta.provider_opaque as segment}<div class="omitted provider-opaque">? {segment.label} · provider opaque</div>{/each}{#each delta.removed as segment}<div class="omitted">− {segment.label} · removed</div>{/each}</details>{/each}{/if}
    <details><summary>Exact compiled context</summary><pre>{primaryPreview.compiled_prompt_preview}</pre></details>
    {#each preview.members as member}<details><summary>{member.preflight.execution.provider} route, tools & proof limits</summary><pre>{JSON.stringify(member.preflight.capabilities, null, 2)}</pre>{#each member.preflight.provider_boundary.proof_limits as limit}<p>{limit}</p>{/each}{#each (member.preflight.provider_boundary.opaque_contexts ?? []) as opaque}<p><strong>Provider-opaque context:</strong> {opaque.statement}</p><pre>{JSON.stringify(opaque, null, 2)}</pre>{/each}</details>{/each}
  {:else if selected}
    <InvocationEvidence {selected} />
    {#if selected.artifacts.length}<details><summary>Artifacts ({selected.artifacts.length})</summary>{#each selected.artifacts as artifact}<details on:toggle={(event) => event.currentTarget.open && onLoadArtifact(artifact.artifact_id)}><summary>{artifact.artifact_id}</summary><pre>{artifactContents[artifact.artifact_id] ?? 'Loading exact bytes…'}</pre><div class="response-actions"><button class="quiet" disabled={working} on:click={() => onGraftArtifact(artifact.artifact_id, 'side-quest')}>Side quest</button><button class="quiet" disabled={working} on:click={() => onGraftArtifact(artifact.artifact_id, 'ask-claude')}>Ask Claude</button><button class="quiet" disabled={working} on:click={() => onGraftArtifact(artifact.artifact_id, 'ask-codex')}>Ask Codex</button><button class="quiet" disabled={working} on:click={() => onGraftArtifact(artifact.artifact_id, 'ask-both')}>Ask both</button><button class="quiet" disabled={working} on:click={() => onGraftArtifact(artifact.artifact_id, 'bring-here')}>Bring here</button><button class="quiet" disabled={working} on:click={() => onGraftArtifact(artifact.artifact_id, 'compare')}>Compare</button></div></details>{/each}</details>{/if}
    {#if neighborhood}<details><summary>Local graph{neighborhood.coverage.state === 'partial' ? ' partial' : ''} · {neighborhood.nodes.length} nodes</summary>{#if neighborhood.coverage.state === 'partial'}<p class="muted">This bounded view reached its node or edge limit at depth {neighborhood.coverage.completed_depth} of {neighborhood.coverage.requested_depth}.</p>{/if}<pre>{JSON.stringify(neighborhood.edges, null, 2)}</pre></details>{/if}
    {#if neighborhoodError}<details class="surface-failure"><summary>Local graph unavailable</summary><p>{neighborhoodError}</p></details>{/if}
  {:else}<p class="muted">Prepare an edge to inspect its exact world before execution.</p>{/if}

  {#if comparison}<div class="comparison"><h3>Branch delta</h3><pre>{JSON.stringify(comparison, null, 2)}</pre></div>{/if}
  {#if selectedSlateSlice}<div class="slate-slice"><h3>{selectedSlateSlice.name}</h3><small>{selectedSlateSlice.external_ref} · {selectedSlateSlice.coverage.state}</small>{#if selectedSlateSlice.items.length}<div>{#each selectedSlateSlice.items as item}<button class="quiet" disabled={item.object_type !== 'invocation'} on:click={() => item.object_type === 'invocation' && onInspect(item.object_id)}>{item.object_type} · {item.object_id.slice(-8)}</button>{/each}</div>{:else}<p class="muted">This live Slate slice is empty.</p>{/if}</div>{/if}
  {#if slateSliceError}<p class="surface-failure">Slate slice unavailable: {slateSliceError}</p>{/if}
  <details class="context-modules">
    <summary>Context modules</summary>
    <p>Resolve exact local or read-only source bytes for this edge, or keep a removable project default.</p>
    {#if contextModuleCatalogueError}<p class="terminal-failure">Catalogue unavailable: {contextModuleCatalogueError}</p>
    {:else if contextModuleCatalogue}
      {#if project?.context_defaults?.length}<div class="context-defaults">{#each project.context_defaults as item}<div><span>{item.module_id}</span><small>{item.mode} · {item.block_id.slice(-8)}</small>{#if item.mode === 'project_default'}<button class="quiet" disabled={working} on:click={() => onRemoveContextDefault(item.module_id)}>Remove</button>{/if}</div>{/each}</div>{/if}
      <label>Identity<input bind:value={contextModuleId} placeholder="weather.home" /></label>
      <label>Source kind<select bind:value={contextModuleSourceKind}><option value="file">file</option><option value="surface">Surface</option><option value="octopus">Octopus conversation</option><option value="http_readonly">read-only HTTP</option></select></label>
      <label>{contextModuleSourceKind === 'http_readonly' ? 'Exact URL' : contextModuleSourceKind === 'octopus' ? 'Conversation ID' : 'Absolute path'}<input bind:value={contextModuleSource} placeholder={contextModuleSourceKind === 'http_readonly' ? 'https://weather.example/current' : contextModuleSourceKind === 'octopus' ? 'conversation-uuid' : '/absolute/source/path'} /></label>
      <label>Use<select bind:value={contextModuleMode}><option value="manual">this edge</option><option value="project_default">project default</option></select></label>
      <button class="quiet" disabled={working || !contextModuleId.trim() || !contextModuleSource.trim()} on:click={onResolveContextModule}>Resolve exact source</button>
      {#if contextModuleReceipt}<small class="module-receipt">Receipt · {contextModuleReceipt.slice(0, 16)}</small>{/if}
    {/if}
  </details>
  {#if selected}<details class="slate-tag"><summary>Tag into a live Slate topic</summary><label>Stable Slate identity<input bind:value={slateConcern} placeholder="canon-quaternary-german" /></label><label>Visible topic label<input bind:value={slateLabel} placeholder="Canon > Quaternary > German" /></label><div><button class="quiet" disabled={working || !slateConcern.trim() || !slateLabel.trim()} on:click={() => onTagSelectedToSlate('invocation')}>Tag invocation</button><button class="quiet" disabled={working || !selected.output_block_id || !slateConcern.trim() || !slateLabel.trim()} on:click={() => onTagSelectedToSlate('response')}>Tag response block</button></div>{#if slateTagReceipt}<small>Live slice updated · {slateTagReceipt}</small>{/if}</details>{/if}
  {#if selected && ['succeeded', 'imported'].includes(selected.state)}<details class="role-pin"><summary>Pin demonstrated function as role</summary><p class="muted">Creates an optional role for future edges. This invocation and its context remain unchanged.</p><label>Role name<input bind:value={pinRoleName} placeholder="Critic" /></label><label>Future instructions<textarea bind:value={pinRoleInstructions} rows="4" placeholder="Describe the function worth repeating…"></textarea></label><label>Session label<input bind:value={pinRoleSessionLabel} placeholder="Critic spine" /></label><button class="quiet" disabled={working || !pinRoleName.trim() || !pinRoleInstructions.trim() || !pinRoleSessionLabel.trim()} on:click={onPinSelectedAsRole}>Pin as role</button>{#if pinRoleReceipt}<small>Future role created · {pinRoleReceipt}</small>{/if}</details>{/if}
  {#if project?.workflow_templates?.length}
    <div class="template-list"><h3>Reusable paths</h3>{#if project.workflow_templates_partial}<p class="muted">Showing the newest 100 templates; the complete artifact ledger remains available through Lattice.</p>{/if}{#each project.workflow_templates as template}<details open={template.artifact_id === extractedTemplateId} on:toggle={(event) => event.currentTarget.open && onLoadArtifact(template.artifact_id)}><summary>{template.artifact_id === extractedTemplateId ? 'Just extracted · ' : ''}{template.artifact_id.slice(-12)}</summary><small>{template.created_at} · {template.content_sha256.slice(0, 12)}</small><pre>{artifactContents[template.artifact_id] ?? 'Open to inspect exact template bytes…'}</pre></details>{/each}</div>
  {/if}
</aside>
