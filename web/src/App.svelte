<script lang="ts">
  import { api, beginSession, extractWorkflow, post } from './api';
  import { renderMarkdown } from './renderMarkdown';
  import type { ContextDelta, ContextPreflight, Invocation, InvocationSummary, Project, Route, WorkflowCommand, WorkflowPack, WorkflowTemplate, WorkflowTransition } from './types';
  import { resolveSelector, selectorReady, selectorSummary, selectorValue, workflowProjectTitle } from './workflowPresentation';

  let projects: Project[] = [];
  let project: Project | null = null;
  let routes: Route[] = [];
  let workflowPacks: WorkflowPack[] = [];
  let selectedRoute = '';
  let selectedSession = '';
  let prompt = '';
  let title = '';
  let purpose = '';
  let selectedParents = new Set<string>();
  let preview: { draft_id: string; revision: number; preflight: ContextPreflight; parent_deltas: ContextDelta[]; compiled_prompt_preview: string } | null = null;
  let selected: Invocation | null = null;
  let comparison: Record<string, unknown> | null = null;
  let neighborhood: { nodes: InvocationSummary[]; edges: Array<Record<string, unknown>>; coverage: { state: 'complete' | 'partial'; requested_depth: number; completed_depth: number; node_limit: number; edge_limit: number } } | null = null;
  let loading = true;
  let working = false;
  let error = '';
  let artifactContents: Record<string, string> = {};
  let commandArguments: Record<string, Record<string, string>> = {};
  let commandIdempotency: Record<string, string> = {};
  let commandReviews: Record<string, { review_sha256: string; prepared_arguments_sha256: string; state_basis: { blackboard_block_id: string; blackboard_sha256: string; adapter_state_sha256: string } | null; file_receipts: Array<{ argument: string; source_ref: string; content_sha256: string; byte_count: number }> }> = {};
  let armedCommand = '';
  let armedAutomation = '';
  let armedTransition = '';
  let transitionValues: Record<string, string> = {};
  let workflowSelectorValues: Record<string, Record<string, string>> = {};
  let blackboardContents: Record<string, string> = {};
  let blackboardLoading = '';
  let activeAttemptId = '';
  let liveProgress = '';
  let liveProgressState = '';
  let extractedTemplateId = '';

  $: selectedRole = project?.roles?.find((role) => role.role_id === project?.sessions?.find((session) => session.role_session_id === selectedSession)?.role_id);
  $: installedRoutes = routes.filter((route) => route.qualified && (!selectedRole || selectedRole.route_policy.kind === 'operator-selectable' || (selectedRole.route_policy.kind === 'model' && selectedRole.route_policy.provider === route.provider_code && selectedRole.route_policy.harness === route.official_cli && (!selectedRole.route_policy.allowed_models?.length || selectedRole.route_policy.allowed_models.includes(route.model_code)))));
  $: if (installedRoutes.length && !installedRoutes.some((route) => `${route.provider_code}:${route.model_code}` === selectedRoute)) selectedRoute = `${installedRoutes[0].provider_code}:${installedRoutes[0].model_code}`;
  $: if (!installedRoutes.length && selectedRole) selectedRoute = '';
  $: activeRoute = installedRoutes.find((route) => `${route.provider_code}:${route.model_code}` === selectedRoute);
  $: responseHtml = selected?.output_block?.content ? renderMarkdown(selected.output_block.content) : '';
  $: trailheads = project?.trailheads ?? [];
  $: activeWorkflowPack = workflowPacks.find((pack) => pack.definition.pack_id === project?.workflow?.pack_id);
  $: workflowPackCurrent = Boolean(project?.workflow?.pack_canonical_sha256 && project.workflow.pack_canonical_sha256 === activeWorkflowPack?.canonical_sha256);
  $: nextWorkflowTransitions = activeWorkflowPack?.definition.allowed_transitions.filter((transition) => transition.from === project?.workflow?.state) ?? [];

  async function boot() {
    loading = true;
    error = '';
    try {
      await beginSession();
      [projects, routes, workflowPacks] = await Promise.all([api<Project[]>('/api/v1/projects'), api<Route[]>('/api/v1/routes'), api<WorkflowPack[]>('/api/v1/workflow-packs')]);
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
    armedCommand = '';
    commandIdempotency = {};
    commandReviews = {};
    armedAutomation = '';
    armedTransition = '';
    extractedTemplateId = '';
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

  function terminalFailure(invocation: Invocation): string {
    const last = invocation.attempts[invocation.attempts.length - 1] as { failure_kind?: string; error_text?: string } | undefined;
    return last?.error_text ?? last?.failure_kind ?? 'No successful terminal receipt exists.';
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
      const invocation = await post<Invocation>(`/api/v1/drafts/${preview.draft_id}/seal`, { expected_revision: preview.revision, expected_preflight_sha256: preview.preflight.preflight_sha256 });
      const attempt = await post<{ attempt_id: string; event_cursor: number }>(`/api/v1/invocations/${invocation.invocation_id}/execute`, { idempotency_key: crypto.randomUUID() });
      activeAttemptId = attempt.attempt_id;
      liveProgress = '';
      liveProgressState = 'Live journal connected';
      const closeProgress = followAttemptProgress(project.project_id, attempt.attempt_id, attempt.event_cursor);
      preview = null;
      prompt = '';
      selectedParents = new Set();
      try { await pollInvocation(invocation.invocation_id); }
      finally { closeProgress(); activeAttemptId = ''; }
      await openProject(project.project_id);
      await inspect(invocation.invocation_id);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally { working = false; }
  }

  function followAttemptProgress(projectId: string, attemptId: string, after: number): () => void {
    const stream = new EventSource(`/api/v1/projects/${encodeURIComponent(projectId)}/events/stream?after=${after}`);
    stream.addEventListener('attempt.chunk', (event) => {
      try {
        const item = JSON.parse(event.data) as { subject_id?: string; payload?: { channel?: string; preview?: string } };
        if (item.subject_id !== attemptId || typeof item.payload?.preview !== 'string') return;
        const line = `${item.payload.channel ?? 'output'} · ${item.payload.preview}`;
        liveProgress = `${liveProgress}${liveProgress ? '\n' : ''}${line}`.slice(-100_000);
        liveProgressState = 'Receiving provider journal';
      } catch (cause: unknown) {
        console.warn('Loom progress event was malformed:', cause);
        liveProgressState = 'Live journal event was unreadable; terminal status polling continues';
      }
    });
    stream.onerror = () => { liveProgressState = 'Live journal disconnected; terminal status polling continues'; };
    return () => stream.close();
  }

  async function cancelActiveAttempt() {
    if (!activeAttemptId) return;
    error = '';
    try {
      await post(`/api/v1/attempts/${activeAttemptId}/cancel`, {});
      liveProgressState = 'Cancellation requested; waiting for the terminal receipt';
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
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
        api<{ nodes: InvocationSummary[]; edges: Array<Record<string, unknown>>; coverage: { state: 'complete' | 'partial'; requested_depth: number; completed_depth: number; node_limit: number; edge_limit: number } }>(`/api/v1/invocations/${id}/neighborhood?depth=2`),
      ]);
      armedTransition = '';
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
  }

  function toggleParent(id: string) {
    const next = new Set(selectedParents);
    next.has(id) ? next.delete(id) : next.add(id);
    selectedParents = next;
    preview = null;
    armedTransition = '';
  }

  async function compareSelected() {
    const ids = [...selectedParents];
    if (ids.length !== 2) return;
    comparison = await api(`/api/v1/diff?left=${encodeURIComponent(ids[0])}&right=${encodeURIComponent(ids[1])}`);
  }

  async function loadOlderInvocations() {
    if (!project?.invocation_page?.next_before) return;
    working = true;
    error = '';
    try {
      const cursor = project.invocation_page.next_before;
      const page = await api<{ items: InvocationSummary[]; has_older: boolean; next_before: { created_at: string; invocation_id: string } | null }>(`/api/v1/projects/${project.project_id}/invocations?limit=250&before_created_at=${encodeURIComponent(cursor.created_at)}&before_invocation_id=${encodeURIComponent(cursor.invocation_id)}`);
      const known = new Set((project.invocations ?? []).map((item) => item.invocation_id));
      project = { ...project, invocations: [...page.items.filter((item) => !known.has(item.invocation_id)), ...(project.invocations ?? [])], invocation_page: { has_older: page.has_older, next_before: page.next_before } };
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  async function extractSelectedPath() {
    if (!project || !selectedParents.size) return;
    working = true;
    error = '';
    try {
      const selectedIds = [...selectedParents];
      const template = await extractWorkflow<WorkflowTemplate>(project.project_id, selectedIds, `${project.title} · ${selectedIds.length}-edge path`);
      extractedTemplateId = template.generated_object.artifact_id;
      await loadArtifact(extractedTemplateId);
      await openProject(project.project_id);
      extractedTemplateId = template.generated_object.artifact_id;
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
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

  function setTransitionValue(key: string, value: string) {
    transitionValues = { ...transitionValues, [key]: value };
    armedTransition = '';
  }

  async function advanceWorkflow(transition: WorkflowTransition) {
    if (!project?.workflow || !activeWorkflowPack) return;
    const key = `${transition.from}:${transition.to}`;
    const trigger = activeWorkflowPack.definition.triggers.find((item) => item.trigger === transition.trigger);
    const payload: Record<string, unknown> = {};
    let excludedBlockId = '';
    if (trigger?.action.type === 'freeze_payload_field') {
      const exact = transitionValues[key] ?? '';
      if (!exact.length) { error = `${trigger.action.field} requires exact accepted bytes.`; return; }
      payload[trigger.action.field] = exact;
    }
    if (trigger?.action.type === 'invoke_role' && trigger.action.exclude_payload_block_field) {
      excludedBlockId = selected?.output_block_id ?? '';
      if (!excludedBlockId) { error = 'Inspect the proposer response that must be excluded from the blinded challenger.'; return; }
      payload[trigger.action.exclude_payload_block_field] = excludedBlockId;
    }
    const evidence = new Set<string>();
    let terminalInvocationOutputs = 0;
    if (project.workflow.blackboard?.block_id) evidence.add(project.workflow.blackboard.block_id);
    for (const invocationId of selectedParents) {
      const invocation = project.invocations?.find((item) => item.invocation_id === invocationId);
      const outputBlockId = invocation?.output_block_id;
      if (outputBlockId && outputBlockId !== excludedBlockId) {
        evidence.add(outputBlockId);
        if (['succeeded', 'imported'].includes(invocation.state)) terminalInvocationOutputs += 1;
      }
    }
    if (excludedBlockId) evidence.delete(excludedBlockId);
    if (evidence.size < transition.minimum_evidence_blocks) {
      error = `This transition requires ${transition.minimum_evidence_blocks} evidence block(s). Check source responses before advancing.`;
      return;
    }
    if (terminalInvocationOutputs < transition.minimum_terminal_invocation_outputs) {
      error = `This transition requires ${transition.minimum_terminal_invocation_outputs} completed response output(s). Check the independent evidence responses before advancing.`;
      return;
    }
    if (armedTransition !== key) {
      armedTransition = key;
      return;
    }
    working = true;
    error = '';
    try {
      await post(`/api/v1/workflow-packs/${project.workflow.pack_id}/transitions`, { project_id: project.project_id, expected_revision: project.workflow.revision, from_state: transition.from, to_state: transition.to, evidence_block_ids: [...evidence], payload });
      armedTransition = '';
      await openProject(project.project_id);
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  function setWorkflowSelectorValue(packId: string, key: string, value: string) {
    workflowSelectorValues = { ...workflowSelectorValues, [packId]: { ...(workflowSelectorValues[packId] ?? {}), [key]: value } };
  }

  async function createWorkflowProject(pack: WorkflowPack, selectorOverride?: Record<string, unknown>) {
    working = true;
    error = '';
    try {
      const selector = selectorOverride ?? resolveSelector(pack, workflowSelectorValues);
      const result = await post<{ project: Project }>(`/api/v1/workflow-packs/${pack.definition.pack_id}/projects`, {
        title: workflowProjectTitle(pack, selector),
        purpose: `Governed through ${pack.definition.title}`,
        selector,
        slice_external_ref: pack.definition.pack_id === 'quaternary' && selector.language === 'de' ? 'slate:canon-quaternary-german' : null,
      });
      projects = await api<Project[]>('/api/v1/projects');
      await openProject(result.project.project_id);
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  async function createCurrentWorkflowProject(pack: WorkflowPack) {
    if (!project?.workflow) return;
    await createWorkflowProject(pack, project.workflow.selector);
  }

  async function revealBlackboard(blockId: string) {
    if (blackboardContents[blockId] !== undefined || blackboardLoading === blockId) return;
    blackboardLoading = blockId;
    error = '';
    try {
      const block = await api<{ content: string }>(`/api/v1/context-blocks/${blockId}`);
      blackboardContents = { ...blackboardContents, [blockId]: block.content };
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { blackboardLoading = ''; }
  }

  function commandValue(command: WorkflowCommand, key: string, fallback: unknown): string {
    return commandArguments[command.operation]?.[key] ?? (fallback === undefined || fallback === null ? '' : String(fallback));
  }

  function setCommandValue(operation: string, key: string, value: string) {
    commandArguments = { ...commandArguments, [operation]: { ...(commandArguments[operation] ?? {}), [key]: value } };
    const nextIdempotency = { ...commandIdempotency };
    delete nextIdempotency[operation];
    commandIdempotency = nextIdempotency;
    const nextReviews = { ...commandReviews };
    delete nextReviews[operation];
    commandReviews = nextReviews;
    armedCommand = '';
  }

  async function runWorkflowCommand(command: WorkflowCommand) {
    if (!project?.workflow) return;
    const argumentsValue: Record<string, unknown> = {};
    for (const argument of command.arguments) {
      const raw = commandValue(command, argument.key, argument.default).trim();
      if (!raw && !argument.required) continue;
      if (!raw) { error = `${argument.label} is required.`; return; }
      if (argument.value_type === 'integer') {
        const parsed = Number(raw);
        if (!Number.isInteger(parsed)) { error = `${argument.label} must be a whole number.`; return; }
        argumentsValue[argument.key] = parsed;
      }
      else if (argument.value_type === 'json') {
        try { argumentsValue[argument.key] = JSON.parse(raw); }
        catch (cause: unknown) { console.warn(`Workflow argument ${argument.key} was not valid JSON:`, cause); error = `${argument.label} must be valid JSON.`; return; }
      } else argumentsValue[argument.key] = raw;
    }
    if (command.authority !== 'read' && armedCommand !== command.operation) {
      working = true;
      error = '';
      try {
        const review = await post<{ review_sha256: string; prepared_arguments_sha256: string; state_basis: { blackboard_block_id: string; blackboard_sha256: string; adapter_state_sha256: string } | null; file_receipts: Array<{ argument: string; source_ref: string; content_sha256: string; byte_count: number }> }>(`/api/v1/workflow-packs/${project.workflow.pack_id}/commands/preflight`, { project_id: project.project_id, expected_revision: project.workflow.revision, operation: command.operation, arguments: argumentsValue });
        commandReviews = { ...commandReviews, [command.operation]: review };
        commandIdempotency = { ...commandIdempotency, [command.operation]: crypto.randomUUID() };
        armedCommand = command.operation;
      } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
      finally { working = false; }
      return;
    }
    const idempotencyKey = commandIdempotency[command.operation] ?? crypto.randomUUID();
    commandIdempotency = { ...commandIdempotency, [command.operation]: idempotencyKey };
    working = true;
    error = '';
    try {
      const outcome = await post<{ command: { state: string; error_text?: string }; blackboard_state: string; blackboard_error?: string }>(`/api/v1/workflow-packs/${project.workflow.pack_id}/commands`, { project_id: project.project_id, expected_revision: project.workflow.revision, operation: command.operation, arguments: argumentsValue, idempotency_key: idempotencyKey, review_sha256: commandReviews[command.operation]?.review_sha256 ?? null });
      if (outcome.command.state !== 'completed') {
        error = `Command is ${outcome.command.state}: ${outcome.command.error_text ?? 'no terminal success receipt exists'}. Loom will not retry this idempotency key.`;
        return;
      }
      armedCommand = '';
      const nextIdempotency = { ...commandIdempotency };
      delete nextIdempotency[command.operation];
      commandIdempotency = nextIdempotency;
      const nextReviews = { ...commandReviews };
      delete nextReviews[command.operation];
      commandReviews = nextReviews;
      await openProject(project.project_id);
      if (outcome.blackboard_state !== 'data') error = `Command completed, but authoritative state was not refreshed: ${outcome.blackboard_error ?? outcome.blackboard_state}.`;
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  function hasAutomationGrant(trigger: string): boolean {
    return Boolean(project?.workflow?.automatic_grants.some((grant) => grant.trigger === trigger && grant.pack_canonical_sha256 === activeWorkflowPack?.canonical_sha256));
  }

  async function setAutomationGrant(trigger: string, enabled: boolean) {
    if (!project?.workflow || !activeWorkflowPack) return;
    const key = `${trigger}:${enabled}`;
    if (armedAutomation !== key) {
      armedAutomation = key;
      return;
    }
    working = true;
    error = '';
    try {
      await api(`/api/v1/workflow-packs/${project.workflow.pack_id}/projects/${project.project_id}/automatic-grants/${trigger}`, { method: 'PUT', body: JSON.stringify({ expected_revision: project.workflow.revision, enabled, pack_canonical_sha256: activeWorkflowPack.canonical_sha256 }) });
      armedAutomation = '';
      await openProject(project.project_id);
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  async function refreshWorkflow() {
    if (!project?.workflow) return;
    working = true;
    error = '';
    try {
      await post(`/api/v1/workflow-packs/${project.workflow.pack_id}/projects/${project.project_id}/refresh`, {});
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
        <div class="section-label">Governed workflows</div>
        {#each workflowPacks.filter((pack) => pack.available) as pack}
          <div class="workflow-launch">
            <strong>{pack.definition.title}</strong>
            {#each pack.definition.project_selector as field}
              <label>{field.label}<input type={field.value_type === 'integer' ? 'number' : 'text'} value={selectorValue(pack, field, workflowSelectorValues)} on:input={(event) => setWorkflowSelectorValue(pack.definition.pack_id, field.key, event.currentTarget.value)} /></label>
            {/each}
            <button class="quiet" disabled={working || !selectorReady(pack, workflowSelectorValues)} on:click={() => createWorkflowProject(pack)}>Open authoritative live view</button>
          </div>
        {/each}
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
                {#each selectorSummary(activeWorkflowPack, project.workflow.selector) as item}<small class="workflow-selector">{item}</small>{/each}
                <small>revision {project.workflow.revision} · {workflowPackCurrent ? `authoritative state remains in ${project.workflow.pack_id === 'quaternary' ? 'the quaternary system' : 'Octopus'}` : 'pack generation changed; this history is preserved but cannot be reinterpreted'}</small>
                {#if !workflowPackCurrent && activeWorkflowPack}<button class="quiet" disabled={working} on:click={() => createCurrentWorkflowProject(activeWorkflowPack)}>Open current generation</button>{/if}
              </div>
              <details>
                <summary>Blackboard & roles</summary>
                {#if project.workflow.blackboard}
                  {@const blackboard = project.workflow.blackboard}
                  <div class="blackboard-receipt"><small>{blackboard.byte_count} bytes · {blackboard.content_sha256.slice(0, 12)} · {blackboard.source_ref ?? 'local source'}</small></div>
                  {#if blackboardContents[blackboard.block_id] !== undefined}<pre>{blackboardContents[blackboard.block_id]}</pre>{:else}<button class="quiet" disabled={blackboardLoading === blackboard.block_id} on:click={() => revealBlackboard(blackboard.block_id)}>{blackboardLoading === blackboard.block_id ? 'Loading exact snapshot…' : 'Reveal exact snapshot'}</button>{/if}
                {:else}<p class="muted">No authoritative blackboard snapshot is available.</p>{/if}
                <div class="role-list">{#each project.roles ?? [] as role}<span>{role.name}</span>{/each}</div>
                {#if workflowPackCurrent && activeWorkflowPack?.definition.triggers.some((trigger) => trigger.automatic)}
                  <h3>Automatic advancement</h3>
                  {#each activeWorkflowPack.definition.triggers.filter((trigger) => trigger.automatic) as trigger}
                    <div class="automation-grant">
                      <div><strong>{trigger.trigger}</strong><small>{hasAutomationGrant(trigger.trigger) ? 'Granted for this exact pack generation' : 'Manual activation only'}</small></div>
                      <button class:armed={armedAutomation === `${trigger.trigger}:${!hasAutomationGrant(trigger.trigger)}`} class="quiet" disabled={working} on:click={() => setAutomationGrant(trigger.trigger, !hasAutomationGrant(trigger.trigger))}>{armedAutomation === `${trigger.trigger}:${!hasAutomationGrant(trigger.trigger)}` ? (hasAutomationGrant(trigger.trigger) ? 'Confirm revoke' : 'Confirm grant') : (hasAutomationGrant(trigger.trigger) ? 'Revoke' : 'Enable')}</button>
                    </div>
                  {/each}
                {/if}
                {#if project.workflow.triggers.length}<h3>Governed actions</h3>{#each project.workflow.triggers as trigger}<div class="trigger"><div><strong>{trigger.trigger_name}</strong><small>{trigger.state} · {trigger.required_action}</small>{#if trigger.error_text}<small>{trigger.error_text}</small>{/if}</div>{#if ['pending', 'execution_failed'].includes(trigger.state)}<button class="quiet" disabled={working} on:click={() => retryTrigger(trigger.trigger_id)}>{trigger.state === 'execution_failed' ? 'Retry execution' : (trigger.error_text ? 'Retry planning' : 'Activate once')}</button>{/if}</div>{/each}{/if}
                <button class="quiet" disabled={working || !workflowPackCurrent} on:click={refreshWorkflow}>Refresh authoritative state</button>
                {#if workflowPackCurrent && nextWorkflowTransitions.length}
                  <h3>Next governed edge</h3>
                  {#each nextWorkflowTransitions as transition}
                    {@const transitionKey = `${transition.from}:${transition.to}`}
                    {@const transitionTrigger = activeWorkflowPack?.definition.triggers.find((item) => item.trigger === transition.trigger)}
                    <div class="workflow-transition">
                      <strong>{transition.from} → {transition.to}</strong>
                      {#if transitionTrigger?.action.type === 'freeze_payload_field'}<label>Exact {transitionTrigger.action.field}<textarea rows="3" value={transitionValues[transitionKey] ?? ''} on:input={(event) => setTransitionValue(transitionKey, event.currentTarget.value)}></textarea></label>{/if}
                      {#if transitionTrigger?.action.type === 'invoke_role'}<small>The inspected response is excluded as the proposal; checked responses plus the live blackboard become challenger evidence.</small>{:else}<small>The live blackboard and checked response outputs become the evidence receipt.</small>{/if}
                      {#if transition.evidence_requirements.some((requirement) => requirement.after_current_state_entry)}<small>Freshness gate: refresh or produce this evidence after entering the current state; older receipts cannot advance it.</small>{/if}
                      {#each transition.terminal_output_requirements as requirement}<small>Role gate: {requirement.minimum_count} fresh {requirement.workflow_role_key} output ({requirement.invocation_states.join(' or ')}).</small>{/each}
                      <button class:armed={armedTransition === transitionKey} class="quiet" disabled={working} on:click={() => advanceWorkflow(transition)}>{armedTransition === transitionKey ? `Confirm ${transition.to}` : `Review ${transition.to}`}</button>
                    </div>
                  {/each}
                {/if}
                {#if workflowPackCurrent && activeWorkflowPack?.definition.commands.length}
                  <h3>Control plane</h3>
                  {#each activeWorkflowPack.definition.commands as command}
                    {@const review = commandReviews[command.operation]}
                    <details class="workflow-command">
                      <summary>{command.label} · {command.authority}</summary>
                      <p>{command.description}</p>
                      {#each command.arguments as argument}
                        <label>{argument.label}<input type={argument.value_type === 'integer' ? 'number' : 'text'} min={argument.minimum} max={argument.maximum} value={commandValue(command, argument.key, argument.default)} placeholder={argument.required ? 'Required' : 'Optional'} on:input={(event) => setCommandValue(command.operation, argument.key, event.currentTarget.value)} /></label>
                      {/each}
                      {#if command.authority !== 'read'}<p class="authority-warning">{command.authority === 'provider_transmission' ? 'This sends the entered material to a subscription model provider.' : 'This crosses the authoritative workflow write boundary.'}</p>{/if}
                      {#if review}
                        <div class="command-review"><small>Exact review {review.review_sha256.slice(0, 12)} · arguments {review.prepared_arguments_sha256.slice(0, 12)}</small>{#if review.state_basis}<small>Authoritative state {review.state_basis.adapter_state_sha256.slice(0, 12)} · snapshot {review.state_basis.blackboard_block_id.slice(-8)}</small>{/if}{#each review.file_receipts as receipt}<small>{receipt.argument} · {receipt.byte_count} bytes · {receipt.content_sha256.slice(0, 12)}</small>{/each}</div>
                      {/if}
                      <button class:armed={armedCommand === command.operation} class="quiet" disabled={working} on:click={() => runWorkflowCommand(command)}>{command.authority !== 'read' && armedCommand !== command.operation ? `Review ${command.label}` : `Run ${command.label}`}</button>
                    </details>
                  {/each}
                {/if}
              </details>
            </section>
          {/if}

          <section class="response-card" class:empty={!selected?.output_block && !activeAttemptId}>
            {#if selected}
              <div class="response-meta"><span>{selected.state}</span><code>{selected.invocation_id.slice(-8)}</code></div>
              {#if activeAttemptId}<div class="live-progress"><div><strong>{liveProgressState}</strong><button class="quiet" on:click={cancelActiveAttempt}>Cancel invocation</button></div><pre>{liveProgress || 'Waiting for the first observable provider bytes…'}</pre></div>{/if}
              {#if selected.output_block}<article>{@html responseHtml}</article>{:else if ['failed', 'interrupted'].includes(selected.state)}<p class="terminal-failure">Invocation ended {selected.state}: {terminalFailure(selected)}</p>{:else}<p class="muted">This invocation has not reached a terminal response.</p>{/if}
            {:else}
              <div class="empty-call"><span>Begin here</span><h3>Say what should happen next.</h3><p>Loom will show the exact context before anything is sent.</p></div>
            {/if}
          </section>

          <section class="history">
            <div class="section-label">Lattice trail</div>
            {#if project.invocation_page?.has_older}<button class="quiet load-older" disabled={working} on:click={loadOlderInvocations}>Load older history</button>{/if}
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
              {#if selectedParents.size}<button class="quiet" disabled={working} on:click={extractSelectedPath}>Extract reusable path</button>{/if}
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
          <div class="truth-status valid">Ready to seal · {preview.preflight.compilation.byte_count} bytes</div>
          <h3>Next invocation receives</h3>
          <div class="segments">
            {#each preview.preflight.segments as segment}<div class="segment"><i class={`plane ${segment.plane}`}></i><div><strong>{segment.label}</strong><small>{segment.plane} · {segment.status}</small><code>{segment.sha256.slice(0, 12)}</code></div></div>{/each}
          </div>
          {#if preview.preflight.omitted.length}<h3>Explicitly absent</h3>{#each preview.preflight.omitted as segment}<div class="omitted">{segment.label} · {segment.disposition}</div>{/each}{/if}
          {#if preview.parent_deltas.length}<h3>Delta from selected trail</h3>{#each preview.parent_deltas as delta}<details><summary>{delta.invocation_id.slice(-8)} · +{delta.added.length} / −{delta.removed.length} · {delta.unchanged_count} unchanged</summary>{#each delta.added as segment}<div class="segment delta-added"><i class={`plane ${segment.plane}`}></i><div><strong>+ {segment.label}</strong><small>{segment.plane} · {segment.disposition}</small></div></div>{/each}{#each delta.removed as segment}<div class="omitted">− {segment.label} · {segment.disposition}</div>{/each}</details>{/each}{/if}
          <details><summary>Exact compiled context</summary><pre>{preview.compiled_prompt_preview}</pre></details>
          <details><summary>Provider boundary</summary>{#each preview.preflight.provider_boundary.proof_limits as limit}<p>{limit}</p>{/each}</details>
        {:else if selected}
          <div class="truth-status" class:valid={selected.manifest_verification?.valid}>{selected.manifest_verification?.valid ? 'Manifest reconstructed' : 'Manifest unavailable'}</div>
          <h3>What this invocation saw</h3>
          <div class="segments">{#each selected.manifest.segments as segment}<div class="segment"><i class={`plane ${segment.plane}`}></i><div><strong>{segment.label}</strong><small>{segment.plane} · {segment.status}</small><code>{segment.sha256.slice(0, 12)}</code></div></div>{/each}</div>
          {#if selected.manifest.omitted.length}<h3>Not supplied</h3>{#each selected.manifest.omitted as segment}<div class="omitted">{segment.label} · {segment.disposition}</div>{/each}{/if}
          <details><summary>Exact compiled context</summary><pre>{selected.compiled_prompt}</pre></details>
          <details><summary>Execution route</summary><pre>{JSON.stringify(selected.manifest.execution, null, 2)}</pre></details>
          <details><summary>Response contract</summary><pre>{JSON.stringify(selected.manifest.response_contract, null, 2)}</pre></details>
          <details><summary>Tools & authority</summary><pre>{JSON.stringify(selected.manifest.capabilities, null, 2)}</pre></details>
          <details><summary>Proof limits</summary>{#each selected.manifest.provider_boundary.proof_limits as limit}<p>{limit}</p>{/each}</details>
          <details><summary>Attempts ({selected.attempts.length})</summary><pre>{JSON.stringify(selected.attempts, null, 2)}</pre></details>
          {#if selected.artifacts.length}<details><summary>Artifacts ({selected.artifacts.length})</summary>{#each selected.artifacts as artifact}<details on:toggle={(event) => event.currentTarget.open && loadArtifact(artifact.artifact_id)}><summary>{artifact.artifact_id}</summary><pre>{artifactContents[artifact.artifact_id] ?? 'Loading exact bytes…'}</pre></details>{/each}</details>{/if}
          {#if neighborhood}<details><summary>Local graph{neighborhood.coverage.state === 'partial' ? ' partial' : ''} · {neighborhood.nodes.length} nodes</summary>{#if neighborhood.coverage.state === 'partial'}<p class="muted">This bounded view reached its node or edge limit at depth {neighborhood.coverage.completed_depth} of {neighborhood.coverage.requested_depth}.</p>{/if}<pre>{JSON.stringify(neighborhood.edges, null, 2)}</pre></details>{/if}
        {:else}<p class="muted">Prepare an edge to inspect its exact world before execution.</p>{/if}

        {#if comparison}<div class="comparison"><h3>Branch delta</h3><pre>{JSON.stringify(comparison, null, 2)}</pre></div>{/if}
        {#if project?.workflow_templates?.length}
          <div class="template-list"><h3>Reusable paths</h3>{#if project.workflow_templates_partial}<p class="muted">Showing the newest 100 templates; the complete artifact ledger remains available through Loom.</p>{/if}{#each project.workflow_templates as template}<details open={template.artifact_id === extractedTemplateId} on:toggle={(event) => event.currentTarget.open && loadArtifact(template.artifact_id)}><summary>{template.artifact_id === extractedTemplateId ? 'Just extracted · ' : ''}{template.artifact_id.slice(-12)}</summary><small>{template.created_at} · {template.content_sha256.slice(0, 12)}</small><pre>{artifactContents[template.artifact_id] ?? 'Open to inspect exact template bytes…'}</pre></details>{/each}</div>
        {/if}
      </aside>
    </div>
  </div>
{/if}
