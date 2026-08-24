<script lang="ts">
  import { api, beginSession, extractWorkflow, post } from './api';
  import { batchIdempotencyKey, reciprocalCrossFeedParents } from './invocationBatch';
  import { renderMarkdown } from './renderMarkdown';
  import { applyModelOverride, buildRoutePayload } from './executionRoutes';
  import type { BatchPreview, Episode, Invocation, InvocationBatch, InvocationSummary, Project, Route, WorkflowCommand, WorkflowPack, WorkflowTemplate, WorkflowTransition } from './types';
  import { resolveSelector, selectorReady, selectorSummary, selectorValue, workflowProjectTitle } from './workflowPresentation';

  let projects: Project[] = [];
  let project: Project | null = null;
  let routes: Route[] = [];
  let workflowPacks: WorkflowPack[] = [];
  let selectedRoute = '';
  let routeMode: 'anthropic' | 'openai' | 'both' = 'anthropic';
  let executionProfile: 'compiled-fresh' | 'workspace-agent' | 'native-continuation' = 'compiled-fresh';
  let continuationMode: 'resume' | 'fork' = 'resume';
  let continuationSource: Invocation | null = null;
  let workspacePath = '';
  let workspaceAuthority: 'read-only' | 'workspace-write' = 'read-only';
  let claudeShell = false;
  let effort: 'low' | 'medium' | 'high' | 'xhigh' | 'max' = 'high';
  let modelOverrides: Record<string, string> = {};
  let selectedSession = '';
  let prompt = '';
  let title = '';
  let purpose = '';
  let selectedParents = new Set<string>();
  let preview: BatchPreview | null = null;
  let pendingBatchRequest: Record<string, unknown> | null = null;
  let explicitSelections: Array<Record<string, unknown>> = [];
  let selectedExcerpt: { text: string; startByte: number; endByte: number } | null = null;
  let episodes: Episode[] = [];
  let currentEpisode: Episode | null = null;
  let slateConcern = '';
  let slateLabel = '';
  let slateTagReceipt = '';
  let pinRoleName = '';
  let pinRoleInstructions = '';
  let pinRoleSessionLabel = '';
  let pinRoleReceipt = '';
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
  let activeAttemptIds: string[] = [];
  let liveProgress = '';
  let liveProgressState = '';
  let extractedTemplateId = '';

  $: selectedRole = project?.roles?.find((role) => role.role_id === project?.sessions?.find((session) => session.role_session_id === selectedSession)?.role_id);
  $: installedRoutes = routes.filter((route) => route.qualified && (!selectedRole || selectedRole.route_policy.kind === 'operator-selectable' || (selectedRole.route_policy.kind === 'model' && selectedRole.route_policy.provider === route.provider_code && selectedRole.route_policy.harness === route.official_cli && (!selectedRole.route_policy.allowed_models?.length || selectedRole.route_policy.allowed_models.includes(route.model_code)))));
  $: if (installedRoutes.length && !installedRoutes.some((route) => `${route.provider_code}:${route.model_code}` === selectedRoute)) selectedRoute = `${installedRoutes[0].provider_code}:${installedRoutes[0].model_code}`;
  $: if (!installedRoutes.length && selectedRole) selectedRoute = '';
  $: activeRoute = installedRoutes.find((route) => `${route.provider_code}:${route.model_code}` === selectedRoute);
  $: routeChoices = routeMode === 'both'
    ? ['anthropic', 'openai'].map((provider) => installedRoutes.find((route) => route.provider_code === provider)).filter((route): route is Route => Boolean(route))
    : [installedRoutes.find((route) => route.provider_code === routeMode)].filter((route): route is Route => Boolean(route));
  $: responseHtml = selected?.output_block?.content ? renderMarkdown(selected.output_block.content) : '';
  $: trailheads = project?.trailheads ?? [];
  $: activeWorkflowPack = workflowPacks.find((pack) => pack.definition.pack_id === project?.workflow?.pack_id);
  $: workflowPackCurrent = Boolean(project?.workflow?.pack_canonical_sha256 && project.workflow.pack_canonical_sha256 === activeWorkflowPack?.canonical_sha256);
  $: nextWorkflowTransitions = activeWorkflowPack?.definition.allowed_transitions.filter((transition) => transition.from === project?.workflow?.state) ?? [];
  $: parentEpisode = episodes.find((episode) => episode.slice_id === currentEpisode?.parent_episode_id) ?? null;
  $: selectedNativeSession = selected?.attempts.slice().reverse().find((attempt) => attempt.state === 'succeeded' && (attempt.runtime_receipt as { native_session?: { id?: string } } | undefined)?.native_session?.id);
  $: selectedNativeEligible = Boolean(selectedNativeSession && ['workspace-agent', 'native-continuation'].includes(String(selected?.route?.execution_profile ?? 'compiled-fresh')));

  async function boot() {
    loading = true;
    error = '';
    try {
      await beginSession();
      [projects, routes, workflowPacks] = await Promise.all([api<Project[]>('/api/v1/projects'), api<Route[]>('/api/v1/routes'), api<WorkflowPack[]>('/api/v1/workflow-packs')]);
      modelOverrides = Object.fromEntries(routes.map((route) => [route.provider_code, route.model_code]));
      const firstRoute = routes.find((route) => route.qualified && route.provider_code === 'anthropic') ?? routes.find((route) => route.qualified && route.provider_code === 'openai');
      if (firstRoute) {
        selectedRoute = `${firstRoute.provider_code}:${firstRoute.model_code}`;
        routeMode = firstRoute.provider_code as 'anthropic' | 'openai';
      }
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
      const result = await post<Project>('/api/v1/projects', { title, purpose });
      title = '';
      purpose = '';
      projects = await api<Project[]>('/api/v1/projects');
      await openProject(result.project_id);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally { working = false; }
  }

  async function openProject(projectId: string) {
    const switchingProjects = project?.project_id !== projectId;
    project = await api<Project>(`/api/v1/projects/${projectId}`);
    episodes = await api<Episode[]>(`/api/v1/projects/${projectId}/episodes`);
    currentEpisode = switchingProjects ? null : episodes.find((episode) => episode.slice_id === currentEpisode?.slice_id) ?? null;
    selectedSession = '';
    selectedParents = new Set();
    preview = null;
    pendingBatchRequest = null;
    explicitSelections = [];
    selectedExcerpt = null;
    comparison = null;
    armedCommand = '';
    commandIdempotency = {};
    commandReviews = {};
    armedAutomation = '';
    armedTransition = '';
    extractedTemplateId = '';
    pinRoleName = '';
    pinRoleInstructions = '';
    pinRoleSessionLabel = '';
    pinRoleReceipt = '';
    executionProfile = 'compiled-fresh';
    continuationSource = null;
    if (project.invocations?.length) await inspect(project.invocations[project.invocations.length - 1].invocation_id);
    else selected = null;
  }

  function routePayload(route: Route) {
    const exactRoute = executionProfile === 'native-continuation' && continuationSource
      ? { ...route, model_code: String(continuationSource.route.model) }
      : applyModelOverride(route, modelOverrides[route.provider_code]);
    return buildRoutePayload(exactRoute, { executionProfile, continuationMode, continuationSource, workspacePath, workspaceAuthority, claudeShell, effort });
  }

  function setModelOverride(provider: string, value: string) {
    modelOverrides = { ...modelOverrides, [provider]: value };
    resetPreview();
  }

  function terminalFailure(invocation: Invocation): string {
    const last = invocation.attempts[invocation.attempts.length - 1] as { failure_kind?: string; error_text?: string } | undefined;
    return last?.error_text ?? last?.failure_kind ?? 'No successful terminal receipt exists.';
  }

  function resetPreview() {
    preview = null;
    pendingBatchRequest = null;
  }

  function makeBatchRequest(routesToUse: Route[] = routeChoices, parentIdsByProvider?: Record<'anthropic' | 'openai', string[]>) {
    if (!project || !prompt.trim() || !routesToUse.length) throw new Error('Write a prompt and choose an available route.');
    const batchId = `batch_${crypto.randomUUID().replaceAll('-', '')}`;
    const sharedAssembly = {
      project_id: project.project_id,
      actor_binding: selectedSession ? { kind: 'role-session', role_session_id: selectedSession } : { kind: 'direct' },
      prompt,
      selections: explicitSelections,
      parent_invocation_ids: [...selectedParents],
      relation: selectedParents.size ? 'inherited_context_from' : 'responds_to',
      context_mode: 'standard',
    };
    return {
      batch_id: batchId,
      specifications: routesToUse.map((route) => {
        const routeParents = parentIdsByProvider?.[route.provider_code as 'anthropic' | 'openai'];
        const assembly = routeParents
          ? { ...sharedAssembly, parent_invocation_ids: routeParents, relation: 'inherited_context_from' }
          : sharedAssembly;
        return { spec_id: route.provider_code, assembly, route: routePayload(route), concern_refs: currentEpisode ? [currentEpisode.concern_ref] : [] };
      }),
    };
  }

  async function previewBatch(request: Record<string, unknown>, executeWhenClear: boolean) {
    pendingBatchRequest = request;
    preview = await post<BatchPreview>('/api/v1/invocation-batches/preview', request);
    if (executeWhenClear && preview.confirmation_reasons.length === 0) await executePreparedBatch([]);
  }

  async function prepare(executeWhenClear = false) {
    if (!project || !prompt.trim()) return;
    working = true;
    error = '';
    try {
      await previewBatch(makeBatchRequest(), executeWhenClear);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally { working = false; }
  }

  async function prepareReciprocalCrossFeed() {
    if (!project || selectedParents.size !== 2 || !prompt.trim()) return;
    working = true;
    error = '';
    try {
      const sources = await Promise.all([...selectedParents].map((invocationId) => api<Invocation>(`/api/v1/invocations/${invocationId}`)));
      const parentIdsByProvider = reciprocalCrossFeedParents(sources);
      const crossFeedRoutes = ['anthropic', 'openai'].map((provider) => installedRoutes.find((route) => route.provider_code === provider)).filter((route): route is Route => Boolean(route));
      if (crossFeedRoutes.length !== 2) throw new Error('Both qualified Claude and Codex routes are required for reciprocal cross-feed.');
      routeMode = 'both';
      selectedSession = '';
      executionProfile = 'compiled-fresh';
      continuationSource = null;
      await previewBatch(makeBatchRequest(crossFeedRoutes, parentIdsByProvider), true);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally { working = false; }
  }

  async function executePreparedBatch(confirmedReasonSha256s: string[]) {
    if (!preview || !pendingBatchRequest || !project) return;
    const projectId = project.project_id;
    working = true;
    error = '';
    try {
      const batch = await post<InvocationBatch>('/api/v1/invocation-batches', {
        ...pendingBatchRequest,
        idempotency_key: batchIdempotencyKey(String(pendingBatchRequest.batch_id)),
        expected_preview_sha256: preview.preview_sha256,
        confirmed_reason_sha256s: confirmedReasonSha256s,
      });
      activeAttemptIds = batch.execution_attempts.map((attempt) => attempt.attempt_id);
      liveProgress = '';
      liveProgressState = `${batch.execution_attempts.length} provider journal${batch.execution_attempts.length === 1 ? '' : 's'} connected`;
      const closeProgress = batch.execution_attempts.map((attempt) => followAttemptProgress(projectId, attempt.attempt_id, attempt.event_cursor, batch.members.find((member) => member.invocation_id === attempt.invocation_id)?.spec_id ?? 'route'));
      preview = null;
      pendingBatchRequest = null;
      prompt = '';
      explicitSelections = [];
      selectedParents = new Set();
      try { await Promise.all(batch.invocation_ids.map((invocationId) => pollInvocation(invocationId))); }
      finally { closeProgress.forEach((close) => close()); activeAttemptIds = []; }
      await openProject(projectId);
      await inspect(batch.invocation_ids[batch.invocation_ids.length - 1]);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally { working = false; }
  }

  function followAttemptProgress(projectId: string, attemptId: string, after: number, label: string): () => void {
    const stream = new EventSource(`/api/v1/projects/${encodeURIComponent(projectId)}/events/stream?after=${after}`);
    stream.addEventListener('attempt.chunk', (event) => {
      try {
        const item = JSON.parse(event.data) as { subject_id?: string; payload?: { channel?: string; preview?: string } };
        if (item.subject_id !== attemptId || typeof item.payload?.preview !== 'string') return;
        const line = `${label} · ${item.payload.channel ?? 'output'} · ${item.payload.preview}`;
        liveProgress = `${liveProgress}${liveProgress ? '\n' : ''}${line}`.slice(-100_000);
        liveProgressState = 'Receiving provider journal';
      } catch (cause: unknown) {
        console.warn('Lattice progress event was malformed:', cause);
        liveProgressState = 'Live journal event was unreadable; terminal status polling continues';
      }
    });
    stream.onerror = () => { liveProgressState = 'Live journal disconnected; terminal status polling continues'; };
    return () => stream.close();
  }

  async function cancelActiveAttempts() {
    if (!activeAttemptIds.length) return;
    error = '';
    try {
      await Promise.all(activeAttemptIds.map((attemptId) => post(`/api/v1/attempts/${attemptId}/cancel`, {})));
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
    resetPreview();
    armedTransition = '';
  }

  function chooseRoute(mode: 'anthropic' | 'openai' | 'both') {
    routeMode = mode;
    selectedSession = '';
    executionProfile = 'compiled-fresh';
    continuationSource = null;
    resetPreview();
  }

  function chooseWorkspaceProfile() {
    executionProfile = 'workspace-agent';
    continuationSource = null;
    resetPreview();
  }

  function configureNativeContinuation(mode: 'resume' | 'fork') {
    if (!selected || !selectedNativeEligible) return;
    const provider = String(selected.route.provider);
    if (provider !== 'anthropic' && provider !== 'openai') return;
    routeMode = provider;
    modelOverrides = { ...modelOverrides, [provider]: String(selected.route.model) };
    executionProfile = 'native-continuation';
    continuationMode = mode;
    continuationSource = selected;
    selectedParents = new Set([selected.invocation_id]);
    selectedSession = '';
    workspacePath = String(selected.route.workspace ?? '');
    workspaceAuthority = selected.route.sandbox === 'workspace-write' ? 'workspace-write' : 'read-only';
    resetPreview();
    document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
  }

  async function beginSideQuest(mode: 'anthropic' | 'openai' | 'both' = routeMode, anchor?: { type: 'invocation' | 'context-block' | 'artifact'; id: string }) {
    if (!selected || !project) return;
    working = true;
    error = '';
    try {
      const exactAnchor = anchor ?? { type: 'invocation' as const, id: selected.invocation_id };
      let parent = currentEpisode;
      if (!parent) {
        parent = await post<Episode>('/api/v1/episodes', { project_id: project.project_id, name: 'Main trail', anchor_type: 'invocation', anchor_id: selected.invocation_id });
      }
      const child = await post<Episode>('/api/v1/episodes', { project_id: project.project_id, name: `Side quest · ${exactAnchor.id.slice(-8)}`, parent_episode_id: parent.slice_id, anchor_type: exactAnchor.type, anchor_id: exactAnchor.id });
      episodes = await api<Episode[]>(`/api/v1/projects/${project.project_id}/episodes`);
      currentEpisode = episodes.find((episode) => episode.slice_id === child.slice_id) ?? child;
      routeMode = mode;
      selectedSession = '';
      executionProfile = 'compiled-fresh';
      continuationSource = null;
      selectedParents = new Set([selected.invocation_id]);
      prompt = '';
      explicitSelections = [];
      selectedExcerpt = null;
      resetPreview();
      document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  function returnToParentEpisode() {
    if (!currentEpisode?.parent_episode_id || !selected) return;
    currentEpisode = episodes.find((episode) => episode.slice_id === currentEpisode?.parent_episode_id) ?? null;
    selectedParents = new Set([selected.invocation_id]);
    prompt = '';
    resetPreview();
    document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
  }

  function captureResponseSelection() {
    const selection = window.getSelection();
    const anchor = selection?.anchorNode instanceof Element ? selection.anchorNode : selection?.anchorNode?.parentElement;
    if (!anchor?.closest('article')) { selectedExcerpt = null; return; }
    const text = selection?.toString() ?? '';
    const source = selected?.output_block?.content ?? '';
    if (!text.trim() || !source) { selectedExcerpt = null; return; }
    const start = source.indexOf(text);
    if (start < 0 || source.indexOf(text, start + 1) >= 0) { selectedExcerpt = null; return; }
    const encoder = new TextEncoder();
    selectedExcerpt = {
      text,
      startByte: encoder.encode(source.slice(0, start)).byteLength,
      endByte: encoder.encode(source.slice(0, start + text.length)).byteLength,
    };
  }

  async function graftSelectedExcerpt(sideQuest: boolean) {
    if (!project || !selected?.output_block_id || !selectedExcerpt) return;
    working = true;
    error = '';
    try {
      const block = await post<{ block_id: string }>('/api/v1/context-blocks/excerpts', {
        project_id: project.project_id,
        source_block_id: selected.output_block_id,
        start_byte: selectedExcerpt.startByte,
        end_byte: selectedExcerpt.endByte,
      });
      if (sideQuest) await beginSideQuest(routeMode, { type: 'context-block', id: block.block_id });
      explicitSelections = [...explicitSelections, { block_id: block.block_id, plane: 'knowledge', label: `graft:${block.block_id.slice(-8)}`, disposition: 'selected', derived_from_block_ids: [selected.output_block_id] }];
      selectedExcerpt = null;
      resetPreview();
      document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  async function graftResponseBlock(sideQuest: boolean) {
    if (!selected?.output_block_id) return;
    if (sideQuest) await beginSideQuest(routeMode, { type: 'context-block', id: selected.output_block_id });
    explicitSelections = [...explicitSelections, { block_id: selected.output_block_id, plane: 'knowledge', label: `response:${selected.output_block_id.slice(-8)}`, disposition: 'selected', derived_from_block_ids: [] }];
    resetPreview();
    document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
  }

  async function graftArtifact(artifactId: string, sideQuest: boolean) {
    working = true;
    error = '';
    try {
      const block = await post<{ block_id: string }>(`/api/v1/artifacts/${artifactId}/context-blocks`, {});
      if (sideQuest) await beginSideQuest(routeMode, { type: 'artifact', id: artifactId });
      explicitSelections = [...explicitSelections, { block_id: block.block_id, plane: 'knowledge', label: `artifact:${artifactId.slice(-8)}`, disposition: 'selected', derived_from_block_ids: [] }];
      resetPreview();
      document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  async function tagSelectedToSlate(target: 'invocation' | 'response') {
    if (!selected || !slateConcern.trim() || !slateLabel.trim()) return;
    const identity = slateConcern.startsWith('slate:') ? slateConcern : `slate:${slateConcern}`;
    const path = target === 'invocation' ? `/api/v1/invocations/${selected.invocation_id}/tags` : `/api/v1/context-blocks/${selected.output_block_id}/tags`;
    if (target === 'response' && !selected.output_block_id) return;
    working = true;
    error = '';
    try {
      const result = await post<{ edge_id: string; slice: { slice_id: string } }>(path, { concern_ref: identity, label: slateLabel });
      slateTagReceipt = `${result.slice.slice_id} · ${result.edge_id}`;
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  async function pinSelectedAsRole() {
    if (!selected || !pinRoleName.trim() || !pinRoleInstructions.trim() || !pinRoleSessionLabel.trim()) return;
    working = true;
    error = '';
    try {
      const result = await post<{ role: { role_id: string }; session: { role_session_id: string } }>(`/api/v1/invocations/${selected.invocation_id}/roles`, {
        name: pinRoleName,
        instructions: pinRoleInstructions,
        incentives: [],
        authority_limits: [],
        session_label: pinRoleSessionLabel,
      });
      const sourceId = selected.invocation_id;
      await openProject(project?.project_id ?? '');
      await inspect(sourceId);
      pinRoleReceipt = `${result.role.role_id} · ${result.session.role_session_id}`;
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
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
        error = `Command is ${outcome.command.state}: ${outcome.command.error_text ?? 'no terminal success receipt exists'}. Lattice will not retry this idempotency key.`;
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

<svelte:head><meta name="description" content="Lattice conversational lattice" /></svelte:head>
<svelte:window on:mouseup={captureResponseSelection} />

{#if loading}
  <main class="center-state"><div class="pulse"></div><p>Opening the lattice…</p></main>
{:else}
  <div class="shell">
    <header>
      <div class="mark">L</div>
      <div><h1>Lattice</h1><p>One consequential edge at a time</p></div>
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
        <details class="secondary-control workflow-catalog">
          <summary>Governed workflows</summary>
          {#each workflowPacks.filter((pack) => pack.available) as pack}
            <div class="workflow-launch">
              <strong>{pack.definition.title}</strong>
              {#each pack.definition.project_selector as field}
                <label>{field.label}<input type={field.value_type === 'integer' ? 'number' : 'text'} value={selectorValue(pack, field, workflowSelectorValues)} on:input={(event) => setWorkflowSelectorValue(pack.definition.pack_id, field.key, event.currentTarget.value)} /></label>
              {/each}
              <button class="quiet" disabled={working || !selectorReady(pack, workflowSelectorValues)} on:click={() => createWorkflowProject(pack)}>Open authoritative live view</button>
            </div>
          {/each}
        </details>
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
          {#if currentEpisode}<nav class="episode-breadcrumb" aria-label="Current episode">{#if parentEpisode}<button on:click={() => currentEpisode = parentEpisode}>{parentEpisode.name}</button><span>›</span>{/if}<strong>{currentEpisode.name}</strong><small>{currentEpisode.items.length} member{currentEpisode.items.length === 1 ? '' : 's'}</small></nav>{/if}

          {#if project.workflow}
            <details class="workflow-control">
              <summary>Workflow control · {project.workflow.pack_id} · {project.workflow.state}</summary>
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
            </details>
          {/if}

          <section class="response-card" class:empty={!selected?.output_block && !activeAttemptIds.length}>
            {#if selected}
              <div class="response-meta"><span>{selected.state}</span><code>{selected.invocation_id.slice(-8)}</code></div>
              {#if activeAttemptIds.length}<div class="live-progress"><div><strong>{liveProgressState}</strong><button class="quiet" on:click={cancelActiveAttempts}>Cancel running routes</button></div><pre>{liveProgress || 'Waiting for the first observable provider bytes…'}</pre></div>{/if}
              {#if selected.output_block}<article>{@html responseHtml}</article>{:else if ['failed', 'interrupted'].includes(selected.state)}<p class="terminal-failure">Invocation ended {selected.state}: {terminalFailure(selected)}</p>{:else}<p class="muted">This invocation has not reached a terminal response.</p>{/if}
              <div class="response-actions">
                <button class="quiet" on:click={() => beginSideQuest()}>Side quest</button>
                <button class="quiet" on:click={() => beginSideQuest('anthropic')}>Ask Claude</button>
                <button class="quiet" on:click={() => beginSideQuest('openai')}>Ask Codex</button>
                <button class="quiet" on:click={() => beginSideQuest('both')}>Ask both</button>
                {#if selected.output_block_id}<button class="quiet" disabled={working} on:click={() => graftResponseBlock(true)}>Side quest on response</button><button class="quiet" disabled={working} on:click={() => graftResponseBlock(false)}>Bring response here</button>{/if}
                {#if selectedNativeEligible}<button class="quiet native" on:click={() => configureNativeContinuation('resume')}>Continue native session</button><button class="quiet native" on:click={() => configureNativeContinuation('fork')}>Fork native session</button>{/if}
                {#if currentEpisode?.parent_episode_id}<button class="quiet return" on:click={returnToParentEpisode}>Return to {parentEpisode?.name ?? 'parent trail'}</button>{/if}
                {#if selectedExcerpt}<button class="quiet selected-text" disabled={working} on:click={() => graftSelectedExcerpt(true)}>Side quest on “{selectedExcerpt.text.slice(0, 36)}{selectedExcerpt.text.length > 36 ? '…' : ''}”</button><button class="quiet selected-text" disabled={working} on:click={() => graftSelectedExcerpt(false)}>Bring selection here</button>{/if}
              </div>
            {:else}
              <div class="empty-call"><span>Begin here</span><h3>Say what should happen next.</h3><p>Lattice will show the exact context before anything is sent.</p></div>
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
              <div class="route-chips" aria-label="Model route">
                <button class:active={routeMode === 'anthropic'} disabled={!installedRoutes.some((route) => route.provider_code === 'anthropic')} on:click={() => chooseRoute('anthropic')}>Claude</button>
                <button class:active={routeMode === 'openai'} disabled={!installedRoutes.some((route) => route.provider_code === 'openai')} on:click={() => chooseRoute('openai')}>Codex</button>
                <button class:active={routeMode === 'both'} disabled={!['anthropic', 'openai'].every((provider) => installedRoutes.some((route) => route.provider_code === provider))} on:click={() => chooseRoute('both')}>both</button>
              </div>
              <span class="parent-count">{selectedParents.size ? `${selectedParents.size} parent${selectedParents.size === 1 ? '' : 's'}` : 'fresh edge'}</span>
            </div>
            <div class="session-strip">
              <div class="profile-chips" aria-label="Execution profile">
                <button class:active={executionProfile === 'compiled-fresh'} on:click={() => { executionProfile = 'compiled-fresh'; continuationSource = null; resetPreview(); }}>fresh</button>
                <button class:active={executionProfile === 'workspace-agent'} disabled={routeChoices.some((route) => !route.qualified_profiles.includes('workspace-agent'))} on:click={chooseWorkspaceProfile}>workspace</button>
                {#if executionProfile === 'native-continuation'}<span>{continuationMode} · {continuationSource?.invocation_id.slice(-8)}</span>{/if}
              </div>
              <label>effort<select bind:value={effort} on:change={resetPreview}><option>low</option><option>medium</option><option>high</option><option>xhigh</option><option>max</option></select></label>
              <details class="advanced-models"><summary>models</summary>{#each routeChoices as route}<label>{route.provider_code}<input value={modelOverrides[route.provider_code] ?? route.model_code} on:input={(event) => setModelOverride(route.provider_code, event.currentTarget.value)} /></label>{/each}</details>
              {#if executionProfile === 'workspace-agent'}
                <label class="workspace-path">workspace<input bind:value={workspacePath} on:input={resetPreview} placeholder="/absolute/project/folder" /></label>
                <label>authority<select bind:value={workspaceAuthority} on:change={resetPreview}><option value="read-only">read only</option><option value="workspace-write">edit workspace</option></select></label>
                {#if routeChoices.some((route) => route.provider_code === 'anthropic')}<label class="shell-toggle"><input type="checkbox" bind:checked={claudeShell} on:change={resetPreview} />Claude shell + provider network</label>{/if}
              {/if}
              <small>{executionProfile === 'compiled-fresh' ? 'Exact context, no tools, no persistent provider session.' : executionProfile === 'workspace-agent' ? 'Starts a recorded provider session with the authority shown here.' : `Uses only ${continuationSource?.invocation_id.slice(-8)} under its exact prior authority.`}</small>
            </div>
            <textarea bind:value={prompt} rows="5" placeholder={selectedParents.size > 1 ? 'Bring these branches together…' : selectedParents.size === 1 ? 'Continue here or take a side quest…' : 'Say what should happen next…'} on:input={resetPreview}></textarea>
            {#if explicitSelections.length}<div class="graft-strip">{explicitSelections.length} exact graft{explicitSelections.length === 1 ? '' : 's'} selected</div>{/if}
            <div class="composer-actions">
              {#if selectedParents.size === 2}<button class="quiet" on:click={compareSelected}>Compare branches</button>{/if}
              {#if selectedParents.size === 2}<button class="quiet" disabled={working || !prompt.trim()} on:click={prepareReciprocalCrossFeed}>Reciprocal cross-feed</button>{/if}
              {#if selectedParents.size}<button class="quiet" disabled={working} on:click={extractSelectedPath}>Extract reusable path</button>{/if}
              <details class="role-binding"><summary>{selectedSession ? 'Role-bound' : 'Direct'}</summary><select bind:value={selectedSession} on:change={resetPreview} aria-label="Optional role session"><option value="">Direct conversation</option>{#each project.sessions ?? [] as session}<option value={session.role_session_id}>{session.label}</option>{/each}</select></details>
              <button class="quiet" disabled={working || !prompt.trim()} on:click={() => prepare(false)}>Inspect context</button>
              {#if preview?.confirmation_reasons.length}<button class="send confirm" disabled={working} on:click={() => executePreparedBatch(preview?.confirmation_reasons.map((item) => item.reason_sha256) ?? [])}>{working ? 'Working…' : 'Confirm & send'}</button>{:else if preview}<button class="send" disabled={working} on:click={() => executePreparedBatch([])}>{working ? 'Working…' : `Send to ${routeMode}`}</button>{:else}<button class="send" disabled={working || !prompt.trim() || !routeChoices.length} on:click={() => prepare(true)}>{working ? 'Working…' : `Send to ${routeMode}`}</button>{/if}
            </div>
          </section>
        {:else}
          <div class="empty-call standalone"><span>No project yet</span><h3>Create the first operational thread.</h3></div>
        {/if}
      </main>

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
          {#each preview.members as member}<details><summary>{member.preflight.execution.provider} route, tools & proof limits</summary><pre>{JSON.stringify(member.preflight.capabilities, null, 2)}</pre>{#each member.preflight.provider_boundary.proof_limits as limit}<p>{limit}</p>{/each}</details>{/each}
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
          {#if selected.artifacts.length}<details><summary>Artifacts ({selected.artifacts.length})</summary>{#each selected.artifacts as artifact}<details on:toggle={(event) => event.currentTarget.open && loadArtifact(artifact.artifact_id)}><summary>{artifact.artifact_id}</summary><pre>{artifactContents[artifact.artifact_id] ?? 'Loading exact bytes…'}</pre><div class="response-actions"><button class="quiet" disabled={working} on:click={() => graftArtifact(artifact.artifact_id, true)}>Side quest on artifact</button><button class="quiet" disabled={working} on:click={() => graftArtifact(artifact.artifact_id, false)}>Bring artifact here</button></div></details>{/each}</details>{/if}
          {#if neighborhood}<details><summary>Local graph{neighborhood.coverage.state === 'partial' ? ' partial' : ''} · {neighborhood.nodes.length} nodes</summary>{#if neighborhood.coverage.state === 'partial'}<p class="muted">This bounded view reached its node or edge limit at depth {neighborhood.coverage.completed_depth} of {neighborhood.coverage.requested_depth}.</p>{/if}<pre>{JSON.stringify(neighborhood.edges, null, 2)}</pre></details>{/if}
        {:else}<p class="muted">Prepare an edge to inspect its exact world before execution.</p>{/if}

        {#if comparison}<div class="comparison"><h3>Branch delta</h3><pre>{JSON.stringify(comparison, null, 2)}</pre></div>{/if}
        {#if selected}<details class="slate-tag"><summary>Tag into a live Slate topic</summary><label>Stable Slate identity<input bind:value={slateConcern} placeholder="canon-quaternary-german" /></label><label>Visible topic label<input bind:value={slateLabel} placeholder="Canon > Quaternary > German" /></label><div><button class="quiet" disabled={working || !slateConcern.trim() || !slateLabel.trim()} on:click={() => tagSelectedToSlate('invocation')}>Tag invocation</button><button class="quiet" disabled={working || !selected.output_block_id || !slateConcern.trim() || !slateLabel.trim()} on:click={() => tagSelectedToSlate('response')}>Tag response block</button></div>{#if slateTagReceipt}<small>Live slice updated · {slateTagReceipt}</small>{/if}</details>{/if}
        {#if selected && ['succeeded', 'imported'].includes(selected.state)}<details class="role-pin"><summary>Pin demonstrated function as role</summary><p class="muted">Creates an optional role for future edges. This invocation and its context remain unchanged.</p><label>Role name<input bind:value={pinRoleName} placeholder="Critic" /></label><label>Future instructions<textarea bind:value={pinRoleInstructions} rows="4" placeholder="Describe the function worth repeating…"></textarea></label><label>Session label<input bind:value={pinRoleSessionLabel} placeholder="Critic spine" /></label><button class="quiet" disabled={working || !pinRoleName.trim() || !pinRoleInstructions.trim() || !pinRoleSessionLabel.trim()} on:click={pinSelectedAsRole}>Pin as role</button>{#if pinRoleReceipt}<small>Future role created · {pinRoleReceipt}</small>{/if}</details>{/if}
        {#if project?.workflow_templates?.length}
          <div class="template-list"><h3>Reusable paths</h3>{#if project.workflow_templates_partial}<p class="muted">Showing the newest 100 templates; the complete artifact ledger remains available through Lattice.</p>{/if}{#each project.workflow_templates as template}<details open={template.artifact_id === extractedTemplateId} on:toggle={(event) => event.currentTarget.open && loadArtifact(template.artifact_id)}><summary>{template.artifact_id === extractedTemplateId ? 'Just extracted · ' : ''}{template.artifact_id.slice(-12)}</summary><small>{template.created_at} · {template.content_sha256.slice(0, 12)}</small><pre>{artifactContents[template.artifact_id] ?? 'Open to inspect exact template bytes…'}</pre></details>{/each}</div>
        {/if}
      </aside>
    </div>
  </div>
{/if}
