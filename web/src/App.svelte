<script lang="ts">
  import { onDestroy, tick } from 'svelte';
  import { api, beginSession, extractWorkflow, post } from './api';
  import { batchIdempotencyKey, reciprocalCrossFeedParents, resolveExactSelection } from './invocationBatch';
  import { contextActionIntent } from './contextActions';
  import type { ContextAction } from './contextActions';
  import { buildContextModuleDefinition } from './contextModules';
  import { renderMarkdown } from './renderMarkdown';
  import { applyModelOverride, buildRoutePayload } from './executionRoutes';
  import type { PaletteCommand } from './commandPalette';
  import type { BatchPreview, ContextModuleCatalogue, ContextModuleResolution, ContextModuleSourceKind, DurableRun, DurableRunEvent, Episode, Invocation, InvocationBatch, InvocationNavigation, InvocationSummary, LiveSlice, Project, Route, TrailheadPage, WorkflowCommand, WorkflowPack, WorkflowPackCatalogue, WorkflowTemplate, WorkflowTransition, WorkspaceDiff, WorkspaceDirectory, WorkspaceFileView, WorkspaceReviewStatus, WorkspaceSearchHit } from './types';
  import { resolveSelector, workflowProjectTitle } from './workflowPresentation';
  import { orderByRecentProjects, readUiState, writeProjectUiState, type ProjectUiState } from './uiPersistence';
  import ConversationHistory from './components/ConversationHistory.svelte';
  import ComposerPane from './components/ComposerPane.svelte';
  import CommandPalette from './components/CommandPalette.svelte';
  import ContextInspector from './components/ContextInspector.svelte';
  import ProjectRail from './components/ProjectRail.svelte';
  import ResponsePane from './components/ResponsePane.svelte';
  import RunSurface from './components/RunSurface.svelte';
  import WorkspaceSurface from './components/WorkspaceSurface.svelte';
  import WorkflowControl from './components/WorkflowControl.svelte';

  type WorkspaceSearchOutcome = {
    query: string;
    mode: 'text' | 'path' | 'symbol';
    state: 'success' | 'failure';
  };

  let projects: Project[] = [];
  let project: Project | null = null;
  let routes: Route[] = [];
  let workflowPacks: WorkflowPack[] = [];
  let workflowPackFailures: Array<{ pack_id: string; detail: string }> = [];
  let workflowCatalogueError = '';
  let routeCatalogueError = '';
  let contextModuleCatalogue: ContextModuleCatalogue | null = null;
  let contextModuleCatalogueError = '';
  let contextModuleId = '';
  let contextModuleMode: 'manual' | 'project_default' = 'manual';
  let contextModuleSourceKind: ContextModuleSourceKind = 'file';
  let contextModuleSource = '';
  let contextModuleReceipt = '';
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
  let allTrailheads: InvocationSummary[] = [];
  let trailheadPage: TrailheadPage | null = null;
  let trailheadsLoading = false;
  let trailheadError = '';
  let preview: BatchPreview | null = null;
  let pendingBatchRequest: Record<string, unknown> | null = null;
  let explicitSelections: Array<Record<string, unknown>> = [];
  let selectedExcerpt: { text: string; startByte: number; endByte: number } | null = null;
  let selectionProofLimit = '';
  let episodes: Episode[] = [];
  let currentEpisode: Episode | null = null;
  let episodeError = '';
  let slateConcern = '';
  let slateLabel = '';
  let slateTagReceipt = '';
  let pinRoleName = '';
  let pinRoleInstructions = '';
  let pinRoleSessionLabel = '';
  let pinRoleReceipt = '';
  let selected: Invocation | null = null;
  let inspectGeneration = 0;
  let selectedNavigation: InvocationNavigation | null = null;
  let selectedSlateSlice: LiveSlice | null = null;
  let navigationError = '';
  let neighborhoodError = '';
  let slateSliceError = '';
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
  let runs: DurableRun[] = [];
  let selectedRunId = '';
  let runEvents: DurableRunEvent[] = [];
  let runsLoading = false;
  let runError = '';
  let runWorking = false;
  let closeRunEventStream: () => void = () => {};
  let runSelectionGeneration = 0;
  let uiStateReady = false;
  let uiStateSuspended = false;
  let uiPersistenceError = '';
  let viewScrollY = 0;
  let pendingScrollY: number | null = null;
  let uiPersistTimer: ReturnType<typeof setTimeout> | null = null;
  let paletteOpen = false;
  let globalRuns: DurableRun[] = [];
  let globalActivityError = '';
  let activityNotice = '';
  let activityInitialized = false;
  let activityTimer: ReturnType<typeof setInterval> | null = null;
  let workspaceSurfaceRoot = '';
  let workspaceDirectory: WorkspaceDirectory | null = null;
  let workspaceFile: WorkspaceFileView | null = null;
  let workspaceSearchHits: WorkspaceSearchHit[] = [];
  let workspaceSearchOutcome: WorkspaceSearchOutcome | null = null;
  let workspaceReviewStatus: WorkspaceReviewStatus | null = null;
  let workspaceDiff: WorkspaceDiff | null = null;
  let workspaceSurfaceLoading = '';
  let workspaceSurfaceError = '';
  let workspaceFileReceipt = '';
  let workspaceSearchQuery = '';
  let workspaceSearchMode: 'text' | 'path' | 'symbol' = 'text';
  let workspaceDiffView: 'unstaged' | 'staged' | 'revision' = 'unstaged';
  let workspaceDiffRevision = '';

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
  $: selectedRun = runs.find((run) => run.run_id === selectedRunId) ?? null;
  $: selectedRunHasDurableLaunch = Boolean(selectedRun?.declaration.local_launch_profile && typeof selectedRun.declaration.local_launch_profile === 'object');
  $: runControlBlocker = selectedRunHasDurableLaunch ? '' : 'This older run has no durable launch profile; its project runner must supply exact launch material.';
  $: activeGlobalRuns = globalRuns.filter((run) => ['running', 'waiting', 'stopping'].includes(run.state));
  $: paletteCommands = buildPaletteCommands();
  $: if (workspaceSurfaceRoot && workspaceSurfaceRoot !== workspacePath) resetWorkspaceSurface();
  $: {
    project?.project_id;
    prompt;
    selectedSession;
    selectedRunId;
    selected?.invocation_id;
    selectedParents;
    explicitSelections;
    currentEpisode?.slice_id;
    routeMode;
    executionProfile;
    workspacePath;
    workspaceAuthority;
    claudeShell;
    effort;
    modelOverrides;
    viewScrollY;
    scheduleUiPersistence();
  }

  function currentProjectUiState(): ProjectUiState | null {
    if (!project) return null;
    return {
      prompt,
      selectedSession,
      selectedRunId,
      selectedInvocationId: selected?.invocation_id ?? '',
      selectedParentIds: [...selectedParents],
      explicitSelections,
      currentEpisodeId: currentEpisode?.slice_id ?? '',
      routeMode,
      executionProfile: executionProfile === 'workspace-agent' ? 'workspace-agent' : 'compiled-fresh',
      workspacePath,
      workspaceAuthority,
      claudeShell,
      effort,
      modelOverrides,
      scrollY: viewScrollY,
      updatedAt: Date.now(),
    };
  }

  function persistCurrentProjectUiState() {
    const state = currentProjectUiState();
    if (!project || !state) return;
    const result = writeProjectUiState(window.localStorage, project.project_id, state);
    uiPersistenceError = result.error;
    if (!result.error) projects = orderByRecentProjects(projects, result.state.recentProjectIds);
  }

  function scheduleUiPersistence() {
    if (!uiStateReady || uiStateSuspended || !project) return;
    if (uiPersistTimer !== null) clearTimeout(uiPersistTimer);
    uiPersistTimer = setTimeout(() => {
      uiPersistTimer = null;
      persistCurrentProjectUiState();
    }, 180);
  }

  function captureViewPosition() {
    viewScrollY = Math.max(0, window.scrollY);
  }

  async function restorePendingViewPosition() {
    if (pendingScrollY === null) return;
    const target = pendingScrollY;
    pendingScrollY = null;
    await tick();
    requestAnimationFrame(() => window.scrollTo({ top: target, behavior: 'instant' }));
  }

  function flushUiState() {
    if (!uiStateReady || uiStateSuspended) return;
    if (uiPersistTimer !== null) {
      clearTimeout(uiPersistTimer);
      uiPersistTimer = null;
    }
    viewScrollY = Math.max(0, window.scrollY);
    persistCurrentProjectUiState();
  }

  function buildPaletteCommands(): PaletteCommand[] {
    return [
      { id: 'focus-composer', label: 'Focus composer', detail: 'Write or continue the current invocation', keywords: ['prompt', 'message'], shortcut: '⌘J', disabled: !project },
      { id: 'refresh-runs', label: 'Refresh work activity', detail: 'Reload durable status without disturbing conversation state', keywords: ['runs', 'progress', 'jobs'] },
      { id: 'open-workspace', label: 'Open workspace', detail: workspacePath || 'Choose a repository path in the composer', keywords: ['files', 'symbols', 'diff', 'review'], disabled: !workspacePath },
      { id: 'route:anthropic', label: 'Route next invocation to Claude', detail: 'Use the current qualified Anthropic route', keywords: ['provider'], disabled: !installedRoutes.some((route) => route.provider_code === 'anthropic') },
      { id: 'route:openai', label: 'Route next invocation to Codex', detail: 'Use the current qualified OpenAI route', keywords: ['provider'], disabled: !installedRoutes.some((route) => route.provider_code === 'openai') },
      { id: 'route:both', label: 'Route next invocation to both providers', detail: 'Create one atomic multi-provider batch', keywords: ['claude', 'codex'], disabled: !['anthropic', 'openai'].every((provider) => installedRoutes.some((route) => route.provider_code === provider)) },
      ...activeGlobalRuns.map((run) => ({ id: `run:${run.run_id}`, label: `Open active run · ${run.name}`, detail: `${run.state} · ${run.unit_counts.completed ?? 0}/${run.units.length} units · ${projects.find((item) => item.project_id === run.project_id)?.title ?? run.project_id}`, keywords: ['activity', 'progress', run.state] })),
      ...projects.map((item) => ({ id: `project:${item.project_id}`, label: `Open ${item.title}`, detail: item.purpose || 'Switch project', keywords: ['project', 'recent'] })),
    ];
  }

  async function executePaletteCommand(commandId: string) {
    activityNotice = '';
    if (commandId === 'focus-composer') {
      await tick();
      document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
      return;
    }
    if (commandId === 'refresh-runs') {
      await Promise.all([refreshRuns(), refreshGlobalActivity()]);
      return;
    }
    if (commandId === 'open-workspace') {
      await openWorkspaceDirectory(workspaceDirectory?.path === '.' ? '' : workspaceDirectory?.path ?? '');
      await tick();
      const surface = document.querySelector<HTMLDetailsElement>('.workspace-surface');
      if (surface) { surface.open = true; surface.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
      return;
    }
    if (commandId.startsWith('route:')) {
      chooseRoute(commandId.slice('route:'.length) as 'anthropic' | 'openai' | 'both');
      await tick();
      document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
      return;
    }
    if (commandId.startsWith('project:')) {
      await openProject(commandId.slice('project:'.length));
      return;
    }
    if (commandId.startsWith('run:')) {
      const runId = commandId.slice('run:'.length);
      const run = globalRuns.find((item) => item.run_id === runId);
      if (!run) return;
      if (project?.project_id !== run.project_id) await openProject(run.project_id);
      await selectRun(runId);
      await tick();
      document.querySelector<HTMLElement>('.run-surface')?.scrollIntoView({ block: 'center' });
    }
  }

  function handleGlobalKeydown(event: KeyboardEvent) {
    if (!(event.metaKey || event.ctrlKey)) return;
    if (event.key.toLocaleLowerCase() === 'k') {
      event.preventDefault();
      paletteOpen = true;
      activityNotice = '';
    } else if (event.key.toLocaleLowerCase() === 'j') {
      event.preventDefault();
      void executePaletteCommand('focus-composer');
    } else if (event.key === 'Enter' && event.target instanceof Element && event.target.closest('.composer')) {
      event.preventDefault();
      void prepare(true);
    }
  }

  async function refreshGlobalActivity() {
    try {
      const next = await api<DurableRun[]>('/api/v1/runs?limit=200');
      if (activityInitialized) {
        const priorStates = new Map(globalRuns.map((run) => [run.run_id, run.state]));
        const changed = next.find((run) => ['completed', 'failed'].includes(run.state) && priorStates.has(run.run_id) && priorStates.get(run.run_id) !== run.state);
        if (changed) activityNotice = `${changed.name} ${changed.state}`;
      }
      globalRuns = next;
      globalActivityError = '';
      activityInitialized = true;
    } catch (cause) {
      globalActivityError = cause instanceof Error ? cause.message : String(cause);
    }
  }

  async function boot() {
    loading = true;
    error = '';
    try {
      await beginSession();
      const [loadedProjects, routeResult, workflowResult] = await Promise.all([
        api<Project[]>('/api/v1/projects'),
        api<Route[]>('/api/v1/routes').then((value) => ({ value, error: '' })).catch((cause) => ({ value: [] as Route[], error: cause instanceof Error ? cause.message : String(cause) })),
        api<WorkflowPackCatalogue>('/api/v1/workflow-packs').then((value) => ({ value, error: '' })).catch((cause) => ({ value: { packs: [], failures: [] } as WorkflowPackCatalogue, error: cause instanceof Error ? cause.message : String(cause) })),
      ]);
      const storedUi = readUiState(window.localStorage);
      uiPersistenceError = storedUi.error;
      projects = orderByRecentProjects(loadedProjects, storedUi.state.recentProjectIds);
      routes = routeResult.value;
      routeCatalogueError = routeResult.error;
      workflowPacks = workflowResult.value.packs;
      workflowPackFailures = workflowResult.value.failures;
      workflowCatalogueError = workflowResult.error;
      try { contextModuleCatalogue = await api<ContextModuleCatalogue>('/api/v1/context-modules'); }
      catch (cause) { contextModuleCatalogueError = cause instanceof Error ? cause.message : String(cause); }
      modelOverrides = Object.fromEntries(routes.map((route) => [route.provider_code, route.model_code]));
      const firstRoute = routes.find((route) => route.qualified && route.provider_code === 'anthropic') ?? routes.find((route) => route.qualified && route.provider_code === 'openai');
      if (firstRoute) {
        selectedRoute = `${firstRoute.provider_code}:${firstRoute.model_code}`;
        routeMode = firstRoute.provider_code as 'anthropic' | 'openai';
      }
      const initialProject = projects.find((item) => item.project_id === storedUi.state.lastProjectId) ?? projects[0];
      if (initialProject) await openProject(initialProject.project_id);
      await refreshGlobalActivity();
      activityTimer = setInterval(() => { void refreshGlobalActivity(); }, 10_000);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally {
      loading = false;
      uiStateReady = true;
      await restorePendingViewPosition();
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
      projects = orderByRecentProjects(await api<Project[]>('/api/v1/projects'), readUiState(window.localStorage).state.recentProjectIds);
      await openProject(result.project_id);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    } finally { working = false; }
  }

  async function openProject(projectId: string) {
    const switchingProjects = project?.project_id !== projectId;
    if (switchingProjects && uiStateReady) flushUiState();
    uiStateSuspended = true;
    const storedUi = readUiState(window.localStorage);
    if (storedUi.error) uiPersistenceError = storedUi.error;
    const restored = storedUi.state.projects[projectId] ?? null;
    if (switchingProjects) {
      closeRunEventStream();
      closeRunEventStream = () => {};
      selectedRunId = '';
      runEvents = [];
      runs = [];
      runError = '';
    }
    try {
      project = await api<Project>(`/api/v1/projects/${projectId}`);
      try {
        episodes = await api<Episode[]>(`/api/v1/projects/${projectId}/episodes`);
        episodeError = '';
        currentEpisode = restored?.currentEpisodeId ? episodes.find((episode) => episode.slice_id === restored.currentEpisodeId) ?? null : switchingProjects ? null : episodes.find((episode) => episode.slice_id === currentEpisode?.slice_id) ?? null;
      } catch (cause) {
        episodes = [];
        currentEpisode = null;
        episodeError = cause instanceof Error ? cause.message : String(cause);
      }
      selectedSession = restored && project.sessions?.some((session) => session.role_session_id === restored.selectedSession) ? restored.selectedSession : '';
      selectedParents = new Set(restored?.selectedParentIds ?? []);
      allTrailheads = [];
      trailheadPage = null;
      trailheadsLoading = false;
      trailheadError = '';
      preview = null;
      pendingBatchRequest = null;
      prompt = restored?.prompt ?? '';
      explicitSelections = restored?.explicitSelections ?? [];
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
      routeMode = restored?.routeMode ?? routeMode;
      executionProfile = restored?.executionProfile ?? 'compiled-fresh';
      continuationSource = null;
      workspacePath = restored?.workspacePath ?? '';
      workspaceAuthority = restored?.workspaceAuthority ?? 'read-only';
      claudeShell = restored?.claudeShell ?? false;
      effort = restored?.effort ?? effort;
      modelOverrides = { ...modelOverrides, ...(restored?.modelOverrides ?? {}) };
      selectedRunId = restored?.selectedRunId ?? '';
      selected = null;
      const fallbackInvocationId = project.invocations?.[project.invocations.length - 1]?.invocation_id ?? '';
      const restoredInvocationId = restored?.selectedInvocationId ?? '';
      const invocationId = restoredInvocationId || fallbackInvocationId;
      if (invocationId) {
        await inspect(invocationId);
        const inspectedInvocationId = (selected as Invocation | null)?.invocation_id ?? '';
        if (restoredInvocationId && inspectedInvocationId !== restoredInvocationId && fallbackInvocationId && fallbackInvocationId !== restoredInvocationId) await inspect(fallbackInvocationId);
      }
      await refreshRuns(projectId);
      viewScrollY = restored?.scrollY ?? 0;
      pendingScrollY = viewScrollY;
    } finally {
      uiStateSuspended = false;
    }
    if (!loading) {
      await restorePendingViewPosition();
      persistCurrentProjectUiState();
    }
  }

  const durableRunEventTypes = [
    'durable-run.declared',
    'durable-run.admitted',
    'durable-run.started',
    'durable-run.resumed',
    'durable-run.progress',
    'durable-run.resource',
    'durable-run.waiting',
    'durable-run.continued',
    'durable-run.stop-requested',
    'durable-run.stopped',
    'durable-run.failed',
    'durable-run.completed',
    'durable-run-unit.started',
    'durable-run-unit.completed',
    'durable-run-unit.refused',
    'durable-run-unit.failed',
    'durable-run-unit.requeued',
    'durable-run-artifact.recorded',
    'durable-run-artifact.removed',
    'durable-run-attempt.orphaned',
    'durable-run-attempt.adopted',
  ];

  async function refreshRuns(projectId = project?.project_id ?? '') {
    if (!projectId) return;
    runsLoading = true;
    try {
      const current = await api<DurableRun[]>(`/api/v1/projects/${projectId}/runs`);
      if (project?.project_id !== projectId) return;
      runs = current;
      runError = '';
      const chosen = current.some((run) => run.run_id === selectedRunId)
        ? selectedRunId
        : current.find((run) => ['running', 'waiting', 'stopping'].includes(run.state))?.run_id ?? current[0]?.run_id ?? '';
      if (chosen && chosen !== selectedRunId) await selectRun(chosen);
      else if (!chosen) {
        closeRunEventStream();
        closeRunEventStream = () => {};
        selectedRunId = '';
        runEvents = [];
      }
    } catch (cause) {
      runError = cause instanceof Error ? cause.message : String(cause);
    } finally { runsLoading = false; }
  }

  async function refreshRun(runId: string) {
    try {
      const current = await api<DurableRun>(`/api/v1/runs/${encodeURIComponent(runId)}`);
      if (!runs.some((run) => run.run_id === runId)) runs = [current, ...runs];
      else runs = runs.map((run) => run.run_id === runId ? current : run);
      runError = '';
    } catch (cause) {
      runError = cause instanceof Error ? cause.message : String(cause);
    }
  }

  async function selectRun(runId: string) {
    const generation = ++runSelectionGeneration;
    closeRunEventStream();
    closeRunEventStream = () => {};
    selectedRunId = runId;
    runEvents = [];
    runError = '';
    try {
      const history = await api<DurableRunEvent[]>(`/api/v1/runs/${encodeURIComponent(runId)}/events?after=0&limit=1000`);
      if (generation !== runSelectionGeneration || selectedRunId !== runId) return;
      runEvents = history;
      const cursor = history[history.length - 1]?.seq ?? 0;
      const stream = new EventSource(`/api/v1/runs/${encodeURIComponent(runId)}/events/stream?after=${cursor}`);
      const receive = (raw: Event) => {
        try {
          const event = JSON.parse((raw as MessageEvent<string>).data) as DurableRunEvent;
          if (event.seq > (runEvents[runEvents.length - 1]?.seq ?? 0)) runEvents = [...runEvents, event].slice(-1000);
          runError = '';
          void refreshRun(runId);
        } catch (cause: unknown) {
          console.warn('Lattice durable-run event was malformed:', cause);
          runError = 'A live run event was unreadable; durable status remains available through refresh.';
        }
      };
      for (const eventType of durableRunEventTypes) stream.addEventListener(eventType, receive);
      stream.onerror = () => { runError = 'Live run updates disconnected; durable state remains available through refresh.'; };
      closeRunEventStream = () => stream.close();
    } catch (cause) {
      if (generation === runSelectionGeneration) runError = cause instanceof Error ? cause.message : String(cause);
    }
  }

  async function stopSelectedRun() {
    if (!selectedRun) return;
    runWorking = true;
    runError = '';
    try {
      await post(`/api/v1/runs/${encodeURIComponent(selectedRun.run_id)}/stop`, {});
      await refreshRun(selectedRun.run_id);
    } catch (cause) { runError = cause instanceof Error ? cause.message : String(cause); }
    finally { runWorking = false; }
  }

  async function startSelectedRun() {
    if (!selectedRun) return;
    const runId = selectedRun.run_id;
    runWorking = true;
    runError = '';
    try {
      if (selectedRun.state === 'declared') await post(`/api/v1/runs/${encodeURIComponent(runId)}/admit`, {});
      await post(`/api/v1/runs/${encodeURIComponent(runId)}/start`, { idempotency_key: `cockpit-start-${crypto.randomUUID()}` });
      await refreshRun(runId);
    } catch (cause) { runError = cause instanceof Error ? cause.message : String(cause); }
    finally { runWorking = false; }
  }

  async function resumeSelectedRun() {
    if (!selectedRun) return;
    const runId = selectedRun.run_id;
    runWorking = true;
    runError = '';
    try {
      await post(`/api/v1/runs/${encodeURIComponent(runId)}/resume`, { idempotency_key: `cockpit-resume-${crypto.randomUUID()}` });
      await refreshRun(runId);
    } catch (cause) { runError = cause instanceof Error ? cause.message : String(cause); }
    finally { runWorking = false; }
  }

  async function useRunArtifact(artifactId: string) {
    if (!project) return;
    runWorking = true;
    runError = '';
    try {
      const block = await post<{ block_id: string }>(`/api/v1/run-artifacts/${encodeURIComponent(artifactId)}/context-blocks`, {});
      explicitSelections = [...explicitSelections, { block_id: block.block_id, plane: 'knowledge', label: `run-artifact:${artifactId.slice(-8)}`, disposition: 'selected', derived_from_block_ids: [] }];
      resetPreview();
      document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
    } catch (cause) { runError = cause instanceof Error ? cause.message : String(cause); }
    finally { runWorking = false; }
  }

  function resetWorkspaceSurface() {
    workspaceSurfaceRoot = '';
    workspaceDirectory = null;
    workspaceFile = null;
    workspaceSearchHits = [];
    workspaceSearchOutcome = null;
    workspaceReviewStatus = null;
    workspaceDiff = null;
    workspaceSurfaceLoading = '';
    workspaceSurfaceError = '';
    workspaceFileReceipt = '';
  }

  async function workspacePost<T>(path: string, value: Record<string, unknown>): Promise<T> {
    if (!workspacePath.trim()) throw new Error('Choose an absolute workspace path first.');
    workspaceSurfaceRoot = workspacePath;
    return post<T>(path, { workspace: workspacePath.trim(), ...value });
  }

  async function openWorkspaceDirectory(path = '') {
    workspaceSurfaceLoading = 'directory';
    workspaceSurfaceError = '';
    try {
      workspaceDirectory = await workspacePost<WorkspaceDirectory>('/api/v1/workspaces/directory', { path });
    } catch (cause) { workspaceSurfaceError = cause instanceof Error ? cause.message : String(cause); }
    finally { workspaceSurfaceLoading = ''; }
  }

  async function openWorkspaceFile(path: string, line: number | null = null) {
    workspaceSurfaceLoading = 'file';
    workspaceSurfaceError = '';
    workspaceFileReceipt = '';
    try {
      workspaceFile = await workspacePost<WorkspaceFileView>('/api/v1/workspaces/file', { path, start_line: Math.max(1, (line ?? 1) - 20), line_count: 240 });
    } catch (cause) { workspaceSurfaceError = cause instanceof Error ? cause.message : String(cause); }
    finally { workspaceSurfaceLoading = ''; }
  }

  async function searchWorkspace() {
    const query = workspaceSearchQuery.trim();
    const mode = workspaceSearchMode;
    if (!query) return;
    workspaceSurfaceLoading = 'search';
    workspaceSurfaceError = '';
    workspaceSearchHits = [];
    workspaceSearchOutcome = null;
    try {
      workspaceSearchHits = await workspacePost<WorkspaceSearchHit[]>('/api/v1/workspaces/search', { query, mode, limit: 200 });
      workspaceSearchOutcome = { query, mode, state: 'success' };
    } catch (cause) {
      workspaceSearchOutcome = { query, mode, state: 'failure' };
      workspaceSurfaceError = cause instanceof Error ? cause.message : String(cause);
    }
    finally { workspaceSurfaceLoading = ''; }
  }

  async function refreshWorkspaceReview() {
    workspaceSurfaceLoading = 'review';
    workspaceSurfaceError = '';
    try {
      workspaceReviewStatus = await workspacePost<WorkspaceReviewStatus>('/api/v1/workspaces/review/status', {});
    } catch (cause) { workspaceSurfaceError = cause instanceof Error ? cause.message : String(cause); }
    finally { workspaceSurfaceLoading = ''; }
  }

  async function loadWorkspaceDiff() {
    workspaceSurfaceLoading = 'diff';
    workspaceSurfaceError = '';
    try {
      workspaceDiff = await workspacePost<WorkspaceDiff>('/api/v1/workspaces/review/diff', { view: workspaceDiffView, revision: workspaceDiffView === 'revision' ? workspaceDiffRevision.trim() : null });
    } catch (cause) { workspaceSurfaceError = cause instanceof Error ? cause.message : String(cause); }
    finally { workspaceSurfaceLoading = ''; }
  }

  async function useWorkspaceFile() {
    if (!project || !contextModuleCatalogue || !workspaceFile) return;
    workspaceSurfaceLoading = 'context';
    workspaceSurfaceError = '';
    try {
      const source = `${workspacePath.replace(/\/+$/, '')}/${workspaceFile.path}`;
      const moduleId = `workspace-file.${crypto.randomUUID().replaceAll('-', '')}`;
      const definition = buildContextModuleDefinition(contextModuleCatalogue, { moduleId, mode: 'manual', sourceKind: 'file', source });
      const result = await post<ContextModuleResolution>('/api/v1/context-modules/resolve', {
        project_id: project.project_id,
        definition,
        expected_sha256: workspaceFile.content_sha256,
        expected_byte_count: workspaceFile.byte_count,
      });
      explicitSelections = [...explicitSelections, { block_id: result.block.block_id, plane: 'knowledge', label: `file:${workspaceFile.path}`, disposition: 'selected', derived_from_block_ids: [] }];
      workspaceFileReceipt = result.receipt.receipt_sha256;
      resetPreview();
      await tick();
      document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
    } catch (cause) { workspaceSurfaceError = cause instanceof Error ? cause.message : String(cause); }
    finally { workspaceSurfaceLoading = ''; }
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
    const generation = ++inspectGeneration;
    error = '';
    try {
      const invocation = await api<Invocation>(`/api/v1/invocations/${id}`);
      if (generation !== inspectGeneration) return;
      selected = invocation;
      neighborhood = null;
      selectedNavigation = null;
      selectedSlateSlice = null;
      neighborhoodError = '';
      navigationError = '';
      slateSliceError = '';
      selectedExcerpt = null;
      selectionProofLimit = '';
      armedTransition = '';
      const [neighborhoodResult, navigationResult] = await Promise.allSettled([
        api<{ nodes: InvocationSummary[]; edges: Array<Record<string, unknown>>; coverage: { state: 'complete' | 'partial'; requested_depth: number; completed_depth: number; node_limit: number; edge_limit: number } }>(`/api/v1/invocations/${id}/neighborhood?depth=2`),
        api<InvocationNavigation>(`/api/v1/invocations/${id}/navigation`),
      ]);
      if (generation !== inspectGeneration) return;
      if (neighborhoodResult.status === 'fulfilled') neighborhood = neighborhoodResult.value;
      else neighborhoodError = neighborhoodResult.reason instanceof Error ? neighborhoodResult.reason.message : String(neighborhoodResult.reason);
      if (navigationResult.status === 'fulfilled') selectedNavigation = navigationResult.value;
      else navigationError = navigationResult.reason instanceof Error ? navigationResult.reason.message : String(navigationResult.reason);
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
    if (!selected || !project) return false;
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
      return true;
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); return false; }
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
    const article = anchor?.closest('article');
    // Keep the exact selection alive while the operator uses its action menu.
    // Selecting a different response or making another in-response selection
    // still replaces it deterministically.
    if (!article?.closest('.response-card') || !selection?.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (!article.contains(range.startContainer) || !article.contains(range.endContainer)) { selectedExcerpt = null; selectionProofLimit = ''; return; }
    const text = selection?.toString() ?? '';
    const source = selected?.output_block?.content ?? '';
    if (!text.trim() || !source) { selectedExcerpt = null; selectionProofLimit = ''; return; }
    const fullRange = document.createRange();
    fullRange.selectNodeContents(article);
    const prefixRange = document.createRange();
    prefixRange.selectNodeContents(article);
    prefixRange.setEnd(range.startContainer, range.startOffset);
    const resolution = resolveExactSelection(source, fullRange.toString(), text, prefixRange.toString().length);
    if (resolution.kind === 'unavailable') { selectedExcerpt = null; selectionProofLimit = resolution.reason; return; }
    selectedExcerpt = resolution;
    selectionProofLimit = '';
  }

  async function graftSelectedExcerpt(action: ContextAction) {
    if (!project || !selected?.output_block_id || !selectedExcerpt) return;
    const intent = contextActionIntent(action, routeMode, 'selected excerpt');
    working = true;
    error = '';
    try {
      const block = await post<{ block_id: string }>('/api/v1/context-blocks/excerpts', {
        project_id: project.project_id,
        source_block_id: selected.output_block_id,
        start_byte: selectedExcerpt.startByte,
        end_byte: selectedExcerpt.endByte,
      });
      if (intent.sideQuest && !await beginSideQuest(intent.route, { type: 'context-block', id: block.block_id })) return;
      explicitSelections = [...explicitSelections, { block_id: block.block_id, plane: 'knowledge', label: `graft:${block.block_id.slice(-8)}`, disposition: 'selected', derived_from_block_ids: [selected.output_block_id] }];
      if (intent.promptSeed) prompt = intent.promptSeed;
      selectedExcerpt = null;
      resetPreview();
      document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  async function graftResponseBlock(action: ContextAction) {
    if (!selected?.output_block_id) return;
    const intent = contextActionIntent(action, routeMode, 'response block');
    if (intent.sideQuest && !await beginSideQuest(intent.route, { type: 'context-block', id: selected.output_block_id })) return;
    explicitSelections = [...explicitSelections, { block_id: selected.output_block_id, plane: 'knowledge', label: `response:${selected.output_block_id.slice(-8)}`, disposition: 'selected', derived_from_block_ids: [] }];
    if (intent.promptSeed) prompt = intent.promptSeed;
    resetPreview();
    document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
  }

  async function graftArtifact(artifactId: string, action: ContextAction) {
    const intent = contextActionIntent(action, routeMode, 'artifact');
    working = true;
    error = '';
    try {
      const block = await post<{ block_id: string }>(`/api/v1/artifacts/${artifactId}/context-blocks`, {});
      if (intent.sideQuest && !await beginSideQuest(intent.route, { type: 'artifact', id: artifactId })) return;
      explicitSelections = [...explicitSelections, { block_id: block.block_id, plane: 'knowledge', label: `artifact:${artifactId.slice(-8)}`, disposition: 'selected', derived_from_block_ids: [] }];
      if (intent.promptSeed) prompt = intent.promptSeed;
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
      selectedNavigation = await api<InvocationNavigation>(`/api/v1/invocations/${selected.invocation_id}/navigation`);
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  async function openSlateSlice(sliceId: string) {
    error = '';
    selectedSlateSlice = null;
    slateSliceError = '';
    try { selectedSlateSlice = await api<LiveSlice>(`/api/v1/slices/${sliceId}`); }
    catch (cause) { slateSliceError = cause instanceof Error ? cause.message : String(cause); }
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

  async function loadTrailheads(reset = false) {
    if (!project || trailheadsLoading || (!reset && !trailheadPage?.next_before)) return;
    trailheadsLoading = true;
    trailheadError = '';
    try {
      const cursor = reset ? null : trailheadPage?.next_before;
      const query = cursor ? `&before_created_at=${encodeURIComponent(cursor.created_at)}&before_invocation_id=${encodeURIComponent(cursor.invocation_id)}` : '';
      const page = await api<TrailheadPage>(`/api/v1/projects/${project.project_id}/trailheads?limit=50${query}`);
      const known = new Set((reset ? [] : allTrailheads).map((item) => item.invocation_id));
      allTrailheads = [...(reset ? [] : allTrailheads), ...page.items.filter((item) => !known.has(item.invocation_id))];
      trailheadPage = page;
    } catch (cause) {
      trailheadError = cause instanceof Error ? cause.message : String(cause);
    } finally { trailheadsLoading = false; }
  }

  async function resumeTrailhead(invocationId: string) {
    selectedParents = new Set([invocationId]);
    await inspect(invocationId);
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

  async function resolveContextModule() {
    if (!project || !contextModuleCatalogue) return;
    working = true;
    error = '';
    try {
      const definition = buildContextModuleDefinition(contextModuleCatalogue, { moduleId: contextModuleId, mode: contextModuleMode, sourceKind: contextModuleSourceKind, source: contextModuleSource });
      const result = await post<ContextModuleResolution>('/api/v1/context-modules/resolve', { project_id: project.project_id, definition });
      contextModuleReceipt = result.receipt.receipt_sha256;
      if (contextModuleMode === 'manual') {
        explicitSelections = [...explicitSelections, { block_id: result.block.block_id, plane: 'knowledge', label: `module:${contextModuleId}`, disposition: 'selected', derived_from_block_ids: [] }];
        resetPreview();
      } else {
        await openProject(project.project_id);
      }
      contextModuleId = '';
      contextModuleSource = '';
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  async function removeContextDefault(moduleId: string) {
    if (!project) return;
    working = true;
    error = '';
    try {
      await api(`/api/v1/projects/${project.project_id}/context-defaults/${encodeURIComponent(moduleId)}`, { method: 'DELETE' });
      await openProject(project.project_id);
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
    finally { working = false; }
  }

  onDestroy(() => {
    flushUiState();
    closeRunEventStream();
    if (activityTimer !== null) clearInterval(activityTimer);
  });
  boot();
</script>

<svelte:head><meta name="description" content="Lattice conversational lattice" /></svelte:head>
<svelte:window on:mouseup={captureResponseSelection} on:scroll={captureViewPosition} on:beforeunload={flushUiState} on:keydown={handleGlobalKeydown} />

{#if loading}
  <main class="center-state"><div class="pulse"></div><p>Opening the lattice…</p></main>
{:else}
  <div class="shell">
    <header>
      <div class="mark">L</div>
      <div><h1>Lattice</h1><p>One consequential edge at a time</p></div>
      <button class="activity-status" class:notice={Boolean(activityNotice)} class:failed={Boolean(globalActivityError)} title={globalActivityError || 'Open projects, active work, and cockpit commands'} on:click={() => { paletteOpen = true; activityNotice = ''; }}>
        {#if globalActivityError}activity unavailable{:else if activityNotice}{activityNotice}{:else if activeGlobalRuns.length}{activeGlobalRuns.length} active · ⌘K{:else}quiet · ⌘K{/if}
      </button>
      <div class="route-status">{routeCatalogueError ? `Provider catalogue unavailable · ${routeCatalogueError}` : activeRoute ? `${activeRoute.official_cli} · ${activeRoute.model_code}` : 'No qualified route'}</div>
    </header>

    {#if error}<div class="error"><span>Execution boundary</span>{error}<button on:click={() => error = ''}>×</button></div>{/if}

    <div class="workspace">
      <ProjectRail
        {projects}
        {project}
        bind:title
        bind:purpose
        {working}
        {workflowCatalogueError}
        {workflowPacks}
        {workflowPackFailures}
        {workflowSelectorValues}
        onOpenProject={openProject}
        onCreateProject={createProject}
        onSetWorkflowSelectorValue={setWorkflowSelectorValue}
        onCreateWorkflowProject={createWorkflowProject}
      />

      <main class="conversation">
        {#if project}
          <div class="project-heading">
            <div><div class="eyebrow">Current project</div><h2>{project.title}</h2><p>{project.purpose}</p></div>
            <div class="trailheads">
              {#each trailheads as trail}
                <button title="Open trailhead" class:chosen={selectedParents.has(trail.invocation_id)} on:click={() => toggleParent(trail.invocation_id)}>{trail.state.slice(0, 1).toUpperCase()}</button>
              {/each}
              <details class="trailhead-catalogue" on:toggle={(event) => event.currentTarget.open && trailheadPage === null && loadTrailheads(true)}>
                <summary>all branches</summary>
                <div class="trailhead-menu">
                  {#if trailheadsLoading && !allTrailheads.length}<small>Loading branch tips…</small>{:else if trailheadError}<small class="terminal-failure">Branch catalogue unavailable: {trailheadError}</small>{:else if trailheadPage && !allTrailheads.length}<small>No branch tips yet.</small>{/if}
                  {#each allTrailheads as trail}<button class:chosen={selectedParents.has(trail.invocation_id)} on:click={() => resumeTrailhead(trail.invocation_id)}><code>{trail.invocation_id.slice(-8)}</code><span>{trail.state}</span></button>{/each}
                  {#if trailheadPage?.has_older}<button class="quiet" disabled={trailheadsLoading} on:click={() => loadTrailheads(false)}>{trailheadsLoading ? 'Loading…' : 'Older branches'}</button>{/if}
                </div>
              </details>
            </div>
          </div>
          {#if uiPersistenceError}<p class="surface-failure">{uiPersistenceError} The current draft remains available only while this tab stays open.</p>{/if}
          {#if selectedNavigation}<nav class="lineage-breadcrumb" aria-label="Invocation concern navigation"><code>{selectedNavigation.invocation.invocation_id.slice(-8)}</code>{#each selectedNavigation.episodes as episode}<span>›</span><button on:click={() => currentEpisode = episodes.find((item) => item.slice_id === episode.slice_id) ?? episode}>{episode.name}</button>{/each}<span>›</span><strong>{selectedNavigation.project.title}</strong>{#each selectedNavigation.slate_topics as topic}<span>›</span><button class="slate" on:click={() => openSlateSlice(topic.slice_id)}>{topic.name}</button>{/each}</nav>{:else if navigationError}<p class="surface-failure">Concern navigation unavailable: {navigationError}</p>{:else if currentEpisode}<nav class="episode-breadcrumb" aria-label="Current episode">{#if parentEpisode}<button on:click={() => currentEpisode = parentEpisode}>{parentEpisode.name}</button><span>›</span>{/if}<strong>{currentEpisode.name}</strong><small>{currentEpisode.items.length} member{currentEpisode.items.length === 1 ? '' : 's'}</small></nav>{/if}
          {#if episodeError}<p class="surface-failure">Episodes unavailable: {episodeError}</p>{/if}

          {#if project.workflow}
            <WorkflowControl
              workflow={project.workflow}
              roles={project.roles ?? []}
              {activeWorkflowPack}
              {workflowPackCurrent}
              {nextWorkflowTransitions}
              {working}
              {blackboardContents}
              {blackboardLoading}
              {armedAutomation}
              {armedTransition}
              {armedCommand}
              {transitionValues}
              {commandReviews}
              onCreateCurrentWorkflowProject={createCurrentWorkflowProject}
              onRevealBlackboard={revealBlackboard}
              {hasAutomationGrant}
              onSetAutomationGrant={setAutomationGrant}
              onRetryTrigger={retryTrigger}
              onRefreshWorkflow={refreshWorkflow}
              onSetTransitionValue={setTransitionValue}
              onAdvanceWorkflow={advanceWorkflow}
              {commandValue}
              onSetCommandValue={setCommandValue}
              onRunWorkflowCommand={runWorkflowCommand}
            />
          {/if}

          <ResponsePane
            {selected}
            {activeAttemptIds}
            {liveProgress}
            {liveProgressState}
            {responseHtml}
            {working}
            {selectedNativeEligible}
            {currentEpisode}
            {parentEpisode}
            {selectedExcerpt}
            {selectionProofLimit}
            {terminalFailure}
            onCancelActive={cancelActiveAttempts}
            onBeginSideQuest={beginSideQuest}
            onGraftResponse={graftResponseBlock}
            onConfigureNative={configureNativeContinuation}
            onReturnToParent={returnToParentEpisode}
            onGraftExcerpt={graftSelectedExcerpt}
          />

          <RunSurface
            {runs}
            run={selectedRun}
            events={runEvents}
            loading={runsLoading}
            error={runError}
            working={runWorking}
            canStart={selectedRunHasDurableLaunch}
            canResume={selectedRunHasDurableLaunch}
            controlBlocker={runControlBlocker}
            onSelect={selectRun}
            onRefresh={() => refreshRuns()}
            onStart={startSelectedRun}
            onStop={stopSelectedRun}
            onResume={resumeSelectedRun}
            onUseArtifact={useRunArtifact}
          />

          <WorkspaceSurface
            {workspacePath}
            directory={workspaceDirectory}
            file={workspaceFile}
            searchHits={workspaceSearchHits}
            searchOutcome={workspaceSearchOutcome}
            reviewStatus={workspaceReviewStatus}
            diff={workspaceDiff}
            loading={workspaceSurfaceLoading}
            error={workspaceSurfaceError}
            selectedFileReceipt={workspaceFileReceipt}
            bind:searchQuery={workspaceSearchQuery}
            bind:searchMode={workspaceSearchMode}
            bind:diffView={workspaceDiffView}
            bind:diffRevision={workspaceDiffRevision}
            onOpenDirectory={openWorkspaceDirectory}
            onOpenFile={openWorkspaceFile}
            onSearch={searchWorkspace}
            onRefreshReview={refreshWorkspaceReview}
            onLoadDiff={loadWorkspaceDiff}
            onUseFile={useWorkspaceFile}
          />

          <ConversationHistory
            {project}
            selectedInvocationId={selected?.invocation_id ?? null}
            {selectedParents}
            {working}
            onLoadOlder={loadOlderInvocations}
            onToggleParent={toggleParent}
            onInspect={inspect}
          />

          <ComposerPane
            {project}
            {installedRoutes}
            {routeChoices}
            bind:routeMode
            bind:executionProfile
            {continuationMode}
            {continuationSource}
            bind:effort
            {modelOverrides}
            bind:workspacePath
            bind:workspaceAuthority
            bind:claudeShell
            bind:prompt
            {selectedParents}
            {explicitSelections}
            bind:selectedSession
            {working}
            {preview}
            onChooseRoute={chooseRoute}
            onChooseFreshProfile={() => { executionProfile = 'compiled-fresh'; continuationSource = null; resetPreview(); }}
            onChooseWorkspaceProfile={chooseWorkspaceProfile}
            onResetPreview={resetPreview}
            onSetModelOverride={setModelOverride}
            onCompareSelected={compareSelected}
            onPrepareReciprocalCrossFeed={prepareReciprocalCrossFeed}
            onExtractSelectedPath={extractSelectedPath}
            onPrepare={prepare}
            onExecutePreparedBatch={executePreparedBatch}
          />
        {:else}
          <div class="empty-call standalone"><span>No project yet</span><h3>Create the first operational thread.</h3></div>
        {/if}
      </main>

      <ContextInspector
        {preview}
        {selected}
        {working}
        {artifactContents}
        {neighborhood}
        {neighborhoodError}
        {comparison}
        {selectedSlateSlice}
        {slateSliceError}
        {contextModuleCatalogueError}
        {contextModuleCatalogue}
        {project}
        bind:contextModuleId
        bind:contextModuleSourceKind
        bind:contextModuleSource
        bind:contextModuleMode
        {contextModuleReceipt}
        bind:slateConcern
        bind:slateLabel
        {slateTagReceipt}
        bind:pinRoleName
        bind:pinRoleInstructions
        bind:pinRoleSessionLabel
        {pinRoleReceipt}
        {extractedTemplateId}
        onLoadArtifact={loadArtifact}
        onGraftArtifact={graftArtifact}
        onInspect={inspect}
        onRemoveContextDefault={removeContextDefault}
        onResolveContextModule={resolveContextModule}
        onTagSelectedToSlate={tagSelectedToSlate}
        onPinSelectedAsRole={pinSelectedAsRole}
      />
    </div>
  </div>
{/if}

<CommandPalette open={paletteOpen} commands={paletteCommands} onClose={() => paletteOpen = false} onChoose={executePaletteCommand} />
