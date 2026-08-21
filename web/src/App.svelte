<script lang="ts">
  import { api, beginSession, post } from './api';
  import { renderMarkdown } from './renderMarkdown';
  import type { Invocation, InvocationSummary, Manifest, Project, Route } from './types';

  let projects: Project[] = [];
  let project: Project | null = null;
  let routes: Route[] = [];
  let selectedRoute = '';
  let selectedSession = '';
  let prompt = '';
  let title = '';
  let purpose = '';
  let selectedParents = new Set<string>();
  let preview: { draft_id: string; revision: number; manifest: Manifest; compiled_prompt_preview: string } | null = null;
  let selected: Invocation | null = null;
  let comparison: Record<string, unknown> | null = null;
  let neighborhood: { nodes: InvocationSummary[]; edges: Array<Record<string, unknown>> } | null = null;
  let loading = true;
  let working = false;
  let error = '';
  let artifactContents: Record<string, string> = {};

  $: selectedRole = project?.roles?.find((role) => role.role_id === project?.sessions?.find((session) => session.role_session_id === selectedSession)?.role_id);
  $: installedRoutes = routes.filter((route) => route.qualified && ['anthropic', 'openai'].includes(route.provider_code) && (!selectedRole || selectedRole.route_policy.kind === 'operator-selectable' || (selectedRole.route_policy.kind === 'model' && selectedRole.route_policy.provider === route.provider_code && selectedRole.route_policy.harness === route.official_cli && (!selectedRole.route_policy.allowed_models?.length || selectedRole.route_policy.allowed_models.includes(route.model_code)))));
  $: if (installedRoutes.length && !installedRoutes.some((route) => `${route.provider_code}:${route.model_code}` === selectedRoute)) selectedRoute = `${installedRoutes[0].provider_code}:${installedRoutes[0].model_code}`;
  $: if (!installedRoutes.length && selectedRole) selectedRoute = '';
  $: activeRoute = installedRoutes.find((route) => `${route.provider_code}:${route.model_code}` === selectedRoute);
  $: responseHtml = selected?.output_block?.content ? renderMarkdown(selected.output_block.content) : '';
  $: trailheads = project?.trailheads ?? [];

  async function boot() {
    loading = true;
    error = '';
    try {
      await beginSession();
      [projects, routes] = await Promise.all([api<Project[]>('/api/v1/projects'), api<Route[]>('/api/v1/routes')]);
      const firstRoute = routes.find((route) => route.qualified && route.provider_code === 'anthropic') ?? routes.find((route) => route.qualified && route.provider_code === 'openai');
      if (firstRoute) selectedRoute = `${firstRoute.provider_code}:${firstRoute.model_code}`;
      if (projects[0]) await openProject(projects[0].project_id);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally {
      loading = false;
    }
  }

  async function createProject() {
    if (!title.trim()) return;
    working = true;
    error = '';
    try {
      const result = await post<{ project: Project; session: { role_session_id: string } }>('/api/v1/projects/bootstrap', { title, purpose });
      title = '';
      purpose = '';
      projects = await api<Project[]>('/api/v1/projects');
      await openProject(result.project.project_id);
      selectedSession = result.session.role_session_id;
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally { working = false; }
  }

  async function openProject(projectId: string) {
    project = await api<Project>(`/api/v1/projects/${projectId}`);
    selectedSession = project.sessions?.[0]?.role_session_id ?? '';
    selectedParents = new Set();
    preview = null;
    comparison = null;
    if (project.invocations?.length) await inspect(project.invocations[project.invocations.length - 1].invocation_id);
    else selected = null;
  }

  function routePayload() {
    if (!activeRoute) throw new Error(selectedRole?.route_policy.kind === 'non-model' ? `${selectedRole.name} is a deterministic or human role, not a model route.` : 'Choose an installed route authorized for this role.');
    return {
      provider: activeRoute.provider_code,
      model: activeRoute.model_code,
      harness: activeRoute.official_cli,
      effort: 'high',
      session_policy: 'fresh',
      tools: [],
      permissions: [],
      workspace: null,
      network_policy: 'disabled',
    };
  }

  async function prepare() {
    if (!project || !selectedSession || !prompt.trim()) return;
    working = true;
    error = '';
    try {
      const draft = await post<{ draft_id: string }>('/api/v1/drafts', {
        project_id: project.project_id,
        role_session_id: selectedSession,
        prompt,
        route: routePayload(),
        selections: [],
        parent_invocation_ids: [...selectedParents],
        relation: selectedParents.size ? 'inherited_context_from' : 'responds_to',
      });
      preview = await api(`/api/v1/drafts/${draft.draft_id}/preview`);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally { working = false; }
  }

  async function send() {
    if (!preview || !project) return;
    working = true;
    error = '';
    try {
      const invocation = await post<Invocation>(`/api/v1/drafts/${preview.draft_id}/seal`, {});
      await post(`/api/v1/invocations/${invocation.invocation_id}/execute`, { idempotency_key: crypto.randomUUID() });
      preview = null;
      prompt = '';
      selectedParents = new Set();
      await pollInvocation(invocation.invocation_id);
      await openProject(project.project_id);
      await inspect(invocation.invocation_id);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally { working = false; }
  }

  async function pollInvocation(id: string) {
    for (;;) {
      const invocation = await api<Invocation>(`/api/v1/invocations/${id}`);
      selected = invocation;
      if (['succeeded', 'failed', 'interrupted'].includes(invocation.state)) return;
      await new Promise((resolve) => setTimeout(resolve, 600));
    }
  }

  async function inspect(id: string) {
    error = '';
    try {
      [selected, neighborhood] = await Promise.all([
        api<Invocation>(`/api/v1/invocations/${id}`),
        api<{ nodes: InvocationSummary[]; edges: Array<Record<string, unknown>> }>(`/api/v1/invocations/${id}/neighborhood?depth=2`),
      ]);
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
  }

  function toggleParent(id: string) {
    const next = new Set(selectedParents);
    next.has(id) ? next.delete(id) : next.add(id);
    selectedParents = next;
    preview = null;
  }

  async function compareSelected() {
    const ids = [...selectedParents];
    if (ids.length !== 2) return;
    comparison = await api(`/api/v1/diff?left=${encodeURIComponent(ids[0])}&right=${encodeURIComponent(ids[1])}`);
  }

  async function loadArtifact(artifactId: string) {
    if (artifactContents[artifactId] !== undefined) return;
    try {
      const artifact = await api<{ content: string }>(`/api/v1/artifacts/${artifactId}`);
      artifactContents = { ...artifactContents, [artifactId]: artifact.content };
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
  }

  async function retryTrigger(triggerId: string) {
    if (!project) return;
    working = true;
    try {
      await post(`/api/v1/workflow-triggers/${triggerId}/retry`, {});
      await openProject(project.project_id);
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  boot();
</script>

<svelte:head><meta name="description" content="Loom conversational lattice" /></svelte:head>

{#if loading}
  <main class="center-state"><div class="pulse"></div><p>Opening the lattice…</p></main>
{:else}
  <div class="shell">
    <header>
      <div class="mark">L</div>
      <div><h1>Loom</h1><p>One consequential edge at a time</p></div>
      <div class="route-status">{activeRoute ? `${activeRoute.official_cli} · ${activeRoute.model_code}` : 'No qualified route'}</div>
    </header>

    {#if error}<div class="error"><span>Execution boundary</span>{error}<button on:click={() => error = ''}>×</button></div>{/if}

    <div class="workspace">
      <aside class="projects">
        <div class="section-label">Projects</div>
        {#each projects as item}
          <button class:active={project?.project_id === item.project_id} on:click={() => openProject(item.project_id)}>
            <span>{item.title}</span><small>{item.purpose || 'Open lattice'}</small>
          </button>
        {/each}
        <div class="new-project">
          <input bind:value={title} placeholder="New project" />
          <textarea bind:value={purpose} rows="2" placeholder="What is this for?"></textarea>
          <button class="quiet" disabled={working || !title.trim()} on:click={createProject}>Create</button>
        </div>
      </aside>

      <main class="conversation">
        {#if project}
          <div class="project-heading">
            <div><div class="eyebrow">Current project</div><h2>{project.title}</h2><p>{project.purpose}</p></div>
            <div class="trailheads">
              {#each trailheads as trail}
                <button title="Open trailhead" class:chosen={selectedParents.has(trail.invocation_id)} on:click={() => toggleParent(trail.invocation_id)}>{trail.state.slice(0, 1).toUpperCase()}</button>
              {/each}
            </div>
          </div>

          {#if project.workflow}
            <section class="workflow-strip">
              <div>
                <span>{project.workflow.pack_id}</span>
                <strong>{project.workflow.state}</strong>
                <small>revision {project.workflow.revision} · authoritative state remains in {project.workflow.pack_id === 'quaternary' ? 'the quaternary system' : 'Octopus'}</small>
              </div>
              <details>
                <summary>Blackboard & roles</summary>
                {#if project.workflow.blackboard}<pre>{project.workflow.blackboard.content}</pre>{:else}<p class="muted">No authoritative blackboard snapshot is available.</p>{/if}
                <div class="role-list">{#each project.roles ?? [] as role}<span>{role.name}</span>{/each}</div>
                {#if project.workflow.triggers.length}<h3>Governed actions</h3>{#each project.workflow.triggers as trigger}<div class="trigger"><strong>{trigger.trigger_name}</strong><small>{trigger.state} · {trigger.required_action}</small>{#if trigger.error_text}<small>{trigger.error_text}</small><button class="quiet" disabled={working} on:click={() => retryTrigger(trigger.trigger_id)}>Retry planning</button>{/if}</div>{/each}{/if}
              </details>
            </section>
          {/if}

          <section class="response-card" class:empty={!selected?.output_block}>
            {#if selected}
              <div class="response-meta"><span>{selected.state}</span><code>{selected.invocation_id.slice(-8)}</code></div>
              {#if selected.output_block}<article>{@html responseHtml}</article>{:else}<p class="muted">This invocation has no terminal response yet.</p>{/if}
            {:else}
              <div class="empty-call"><span>Begin here</span><h3>Say what should happen next.</h3><p>Loom will show the exact context before anything is sent.</p></div>
            {/if}
          </section>

          <section class="history">
            <div class="section-label">Lattice trail</div>
            {#each [...(project.invocations ?? [])].reverse() as item}
              <div class="history-row" class:selected={selected?.invocation_id === item.invocation_id}>
                <input type="checkbox" checked={selectedParents.has(item.invocation_id)} on:change={() => toggleParent(item.invocation_id)} aria-label="Select as parent" />
                <button on:click={() => inspect(item.invocation_id)}><span>{item.state}</span><code>{item.invocation_id.slice(-8)}</code><time>{item.created_at}</time></button>
              </div>
            {/each}
          </section>

          <section class="composer">
            <div class="composer-top">
              <select bind:value={selectedSession} aria-label="Role session">{#each project.sessions ?? [] as session}<option value={session.role_session_id}>{session.label}</option>{/each}</select>
              <select bind:value={selectedRoute} aria-label="Model route">{#each installedRoutes as route}<option value={`${route.provider_code}:${route.model_code}`}>{route.official_cli} · {route.model_code}</option>{/each}</select>
              <span class="parent-count">{selectedParents.size ? `${selectedParents.size} parent${selectedParents.size === 1 ? '' : 's'}` : 'fresh edge'}</span>
            </div>
            <textarea bind:value={prompt} rows="5" placeholder="Route the next thought…" on:input={() => preview = null}></textarea>
            <div class="composer-actions">
              {#if selectedParents.size === 2}<button class="quiet" on:click={compareSelected}>Compare branches</button>{/if}
              <button class="quiet" disabled={working || !prompt.trim()} on:click={prepare}>Inspect context</button>
              <button class="send" disabled={working || !preview} on:click={send}>{working ? 'Working…' : 'Send edge'}</button>
            </div>
          </section>
        {:else}
          <div class="empty-call standalone"><span>No project yet</span><h3>Create the first operational thread.</h3></div>
        {/if}
      </main>

      <aside class="inspector">
        <div class="section-label">Context truth</div>
        {#if preview}
          <div class="truth-status valid">Ready to seal · {preview.manifest.compilation.byte_count} bytes</div>
          <h3>Next invocation receives</h3>
          <div class="segments">
            {#each preview.manifest.segments as segment}<div class="segment"><i class={`plane ${segment.plane}`}></i><div><strong>{segment.label}</strong><small>{segment.plane} · {segment.status}</small><code>{segment.sha256.slice(0, 12)}</code></div></div>{/each}
          </div>
          {#if preview.manifest.omitted.length}<h3>Explicitly absent</h3>{#each preview.manifest.omitted as segment}<div class="omitted">{segment.label} · {segment.disposition}</div>{/each}{/if}
          <details><summary>Exact compiled context</summary><pre>{preview.compiled_prompt_preview}</pre></details>
          <details><summary>Provider boundary</summary>{#each preview.manifest.provider_boundary.proof_limits as limit}<p>{limit}</p>{/each}</details>
        {:else if selected}
          <div class="truth-status" class:valid={selected.manifest_verification?.valid}>{selected.manifest_verification?.valid ? 'Manifest reconstructed' : 'Manifest unavailable'}</div>
          <h3>What this invocation saw</h3>
          <div class="segments">{#each selected.manifest.segments as segment}<div class="segment"><i class={`plane ${segment.plane}`}></i><div><strong>{segment.label}</strong><small>{segment.plane} · {segment.status}</small><code>{segment.sha256.slice(0, 12)}</code></div></div>{/each}</div>
          {#if selected.manifest.omitted.length}<h3>Not supplied</h3>{#each selected.manifest.omitted as segment}<div class="omitted">{segment.label} · {segment.disposition}</div>{/each}{/if}
          <details><summary>Exact compiled context</summary><pre>{selected.compiled_prompt}</pre></details>
          <details><summary>Execution route</summary><pre>{JSON.stringify(selected.manifest.execution, null, 2)}</pre></details>
          <details><summary>Tools & authority</summary><pre>{JSON.stringify(selected.manifest.capabilities, null, 2)}</pre></details>
          <details><summary>Proof limits</summary>{#each selected.manifest.provider_boundary.proof_limits as limit}<p>{limit}</p>{/each}</details>
          <details><summary>Attempts ({selected.attempts.length})</summary><pre>{JSON.stringify(selected.attempts, null, 2)}</pre></details>
          {#if selected.artifacts.length}<details><summary>Artifacts ({selected.artifacts.length})</summary>{#each selected.artifacts as artifact}<details on:toggle={(event) => event.currentTarget.open && loadArtifact(artifact.artifact_id)}><summary>{artifact.artifact_id}</summary><pre>{artifactContents[artifact.artifact_id] ?? 'Loading exact bytes…'}</pre></details>{/each}</details>{/if}
          {#if neighborhood}<details><summary>Local graph · {neighborhood.nodes.length} nodes</summary><pre>{JSON.stringify(neighborhood.edges, null, 2)}</pre></details>{/if}
        {:else}<p class="muted">Prepare an edge to inspect its exact world before execution.</p>{/if}

        {#if comparison}<div class="comparison"><h3>Branch delta</h3><pre>{JSON.stringify(comparison, null, 2)}</pre></div>{/if}
      </aside>
    </div>
  </div>
{/if}
