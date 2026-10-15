<script lang="ts">
  import { onDestroy, tick } from 'svelte';
  import { api, beginSession, extractWorkflow, post } from './api';
  import { batchIdempotencyKey, reciprocalCrossFeedParents, resolveExactSelection } from './invocationBatch';
  import { contextActionIntent } from './contextActions';
  import type { ContextAction } from './contextActions';
  import { buildContextModuleDefinition } from './contextModules';
  import { applyModelOverride, buildRoutePayload } from './executionRoutes';
  import { activeAttemptIds as activityAttemptIds, activityFailureForInvocation, activityForInvocation, attentionActivityTransitions, memberForInvocation, mergeActivitySnapshots, projectActivityBadges, projectReplayCursor, terminalActivityTransitions } from './invocationActivity';
  import type { PaletteCommand } from './commandPalette';
  import type { BatchPreview, ContextModuleCatalogue, ContextModuleResolution, ContextModuleSourceKind, DurableRun, DurableRunEvent, Episode, Invocation, InvocationActivitySnapshot, InvocationBatch, InvocationNavigation, InvocationSummary, LatticeSearchHit, LiveSlice, Project, Route, TrailheadPage, WorkflowCommand, WorkflowPack, WorkflowPackCatalogue, WorkflowTemplate, WorkflowTransition, WorkspaceDiff, WorkspaceDirectory, WorkspaceFileView, WorkspaceReviewStatus, WorkspaceSearchHit } from './types';
  import { copyText, invocationExportPayload } from './copyExport';
  import { notifyOperator } from './operatorNotifications';
  import { nextPrompt, previousPrompt, recordPrompt } from './promptHistory';
  import { recoveryNotice, type RecoveryNotice } from './recoveryStatus';
  import { resolveSelector, workflowProjectTitle } from './workflowPresentation';
  import { orderByRecentProjects, readUiState, writeProjectUiState, type ProjectUiState } from './uiPersistence';
  import { reconcileSubmittedComposer } from './submissionState';
  import ConversationHistory from './components/ConversationHistory.svelte';
  import DurableConversations from './components/DurableConversations.svelte';
  let durableSelection: { conversationId: string; invocationId: string | null; model: string | null } | null = null;
  $: durableConversationSelected = durableSelection !== null;
  import ConversationInspector from './components/ConversationInspector.svelte';
  import ComposerPane from './components/ComposerPane.svelte';
  import CommandPalette from './components/CommandPalette.svelte';
  import ContextInspector from './components/ContextInspector.svelte';
  import ProjectRail from './components/ProjectRail.svelte';
  import ResponsePane from './components/ResponsePane.svelte';
  import NativeApprovals from './components/NativeApprovals.svelte';
  import type { NativeApproval } from './types';
  import RunSurface from './components/RunSurface.svelte';
  import WorkspaceSurface from './components/WorkspaceSurface.svelte';
  import WorkflowControl from './components/WorkflowControl.svelte';

  type WorkspaceSearchOutcome = {
    query: string;
    mode: 'text' | 'path' | 'symbol';
    state: 'success' | 'failure';
  };

  let projects: Project[] = [];
  let approvalBusy: string[] = [];
  let approvalErrors: Record<string, string> = {};
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
  let projectOpenGeneration = 0;
  let selectedNavigation: InvocationNavigation | null = null;
  let selectedSlateSlice: LiveSlice | null = null;
  let navigationError = '';
  let neighborhoodError = '';
  let slateSliceError = '';
  let comparison: Record<string, unknown> | null = null;
  let neighborhood: { nodes: InvocationSummary[]; edges: Array<Record<string, unknown>>; coverage: { state: 'complete' | 'partial'; requested_depth: number; completed_depth: number; node_limit: number; edge_limit: number } } | null = null;
  let loading = true;
  let working = false;
  let projectOpening = false;
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
  let invocationActivity: InvocationActivitySnapshot = { items: [], project_cursors: {}, activity_cursors: {}, recent_has_more: {} };
  let invocationActivityRefreshError = '';
  let invocationActivityStreamError = '';
  let activitySeenCursors: Record<string, number> = {};
  let activityProgress: Record<string, string[]> = {};
  let activityProgressState: Record<string, string> = {};
  let activityEventSequences: Record<string, number> = {};
  let activityAttemptInvocations: Record<string, string> = {};
  let closeInvocationActivityStream: () => void = () => {};
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
  let promptHistory: string[] = [];
  let promptHistoryCursor: number | null = null;
  let liveDraftBeforeHistory = '';
  let recovery: RecoveryNotice | null = null;
  let copyReceipt = '';
  let conversationSearchHits: LatticeSearchHit[] = [];
  let conversationSearchError = '';

  $: selectedRole = project?.roles?.find((role) => role.role_id === project?.sessions?.find((session) => session.role_session_id === selectedSession)?.role_id);
  $: installedRoutes = routes.filter((route) => route.qualified && (!selectedRole || selectedRole.route_policy.kind === 'operator-selectable' || (selectedRole.route_policy.kind === 'model' && selectedRole.route_policy.provider === route.provider_code && selectedRole.route_policy.harness === route.official_cli && (!selectedRole.route_policy.allowed_models?.length || selectedRole.route_policy.allowed_models.includes(route.model_code)))));
  $: if (installedRoutes.length && !installedRoutes.some((route) => `${route.provider_code}:${route.model_code}` === selectedRoute)) selectedRoute = `${installedRoutes[0].provider_code}:${installedRoutes[0].model_code}`;
  $: if (!installedRoutes.length && selectedRole) selectedRoute = '';
  $: activeRoute = installedRoutes.find((route) => `${route.provider_code}:${route.model_code}` === selectedRoute);
  $: routeChoices = routeMode === 'both'
    ? ['anthropic', 'openai'].map((provider) => installedRoutes.find((route) => route.provider_code === provider)).filter((route): route is Route => Boolean(route))
    : [installedRoutes.find((route) => route.provider_code === routeMode)].filter((route): route is Route => Boolean(route));
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
  $: activeProviderActivities = invocationActivity.items.filter((activity) => !activity.terminal || activity.needs_attention);
  $: selectedProviderActivity = activityForInvocation(invocationActivity.items, selected?.invocation_id);
  $: selectedApprovalRequests = selected?.attempts.flatMap((attempt) => attempt.native_approvals?.requests ?? []) ?? [];
  $: selectedActiveAttemptIds = activityAttemptIds(selectedProviderActivity, selected?.invocation_id);
  $: selectedActivityFailure = activityFailureForInvocation(selectedProviderActivity, selected?.invocation_id);
  $: selectedLiveProgress = selectedActiveAttemptIds.flatMap((attemptId) => activityProgress[attemptId] ?? []).join('\n').slice(-100_000);
  $: selectedLiveProgressState = selectedActiveAttemptIds.map((attemptId) => activityProgressState[attemptId]).filter(Boolean).at(-1) ?? (selectedActiveAttemptIds.length ? `${selectedActiveAttemptIds.length} provider journal${selectedActiveAttemptIds.length === 1 ? '' : 's'} connected` : '');
  $: activitySurfaceError = [globalActivityError, invocationActivityRefreshError, invocationActivityStreamError].filter(Boolean).join(' · ');
  $: projectActivity = projectActivityBadges(invocationActivity, activitySeenCursors, project?.project_id ?? '');
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

  function currentProjectUiState(selectedInvocationId = selected?.invocation_id ?? ''): ProjectUiState | null {
    if (!project) return null;
    return {
      prompt,
      selectedSession,
      selectedRunId,
      selectedInvocationId,
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
      lastSeenActivityCursor: Math.max(activitySeenCursors[project.project_id] ?? 0, invocationActivity.activity_cursors[project.project_id] ?? 0),
      promptHistory,
      updatedAt: Date.now(),
    };
  }

  function persistCurrentProjectUiState(selectedInvocationId?: string) {
    const state = currentProjectUiState(selectedInvocationId);
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
      { id: 'send', label: 'Send current prompt', detail: 'Preview and execute unless confirmation is required', keywords: ['submit'], shortcut: '⌘⏎', disabled: !project || !prompt.trim() },
      { id: 'side-quest', label: 'Side quest from this response', detail: 'Open a child episode from the selected invocation', disabled: !selected },
      { id: 'return-parent', label: 'Return to parent episode', detail: 'Bring selected work back to the parent trail', disabled: !currentEpisode?.parent_episode_id },
      { id: 'compare', label: 'Compare selected parents', detail: 'Diff two selected invocations', disabled: selectedParents.size !== 2 },
      { id: 'stop-run', label: 'Stop selected run', detail: 'Request a safe interruption', disabled: !selectedRunHasDurableLaunch || !['running', 'waiting'].includes(selectedRun?.state ?? '') },
      { id: 'resume-run', label: 'Resume selected run', detail: 'Continue from the last durable checkpoint', disabled: !selectedRunHasDurableLaunch || !['stopped', 'failed'].includes(selectedRun?.state ?? '') },
      { id: 'retry-selected', label: 'Retry or edit previous prompt', detail: 'Restore the original prompt for another send', disabled: !selected },
      { id: 'copy-response', label: 'Copy response', detail: 'Put the exact selected response on the clipboard', disabled: !selected?.output_block },
      { id: 'export-invocation', label: 'Export selected invocation', detail: 'Download prompt, response, and manifest JSON', disabled: !selected },
      { id: 'open-diff', label: 'Open repository diff', detail: 'Review unstaged, staged, or revision changes', disabled: !workspacePath },
      ...conversationSearchHits.map((hit) => ({ id: `search:${hit.kind}:${hit.object_id}`, label: `Open ${hit.kind} · ${hit.object_id.slice(-8)}`, detail: hit.preview, keywords: ['search', hit.kind] })),
      { id: 'route:anthropic', label: 'Route next invocation to Claude', detail: 'Use the current qualified Anthropic route', keywords: ['provider'], disabled: !installedRoutes.some((route) => route.provider_code === 'anthropic') },
      { id: 'route:openai', label: 'Route next invocation to Codex', detail: 'Use the current qualified OpenAI route', keywords: ['provider'], disabled: !installedRoutes.some((route) => route.provider_code === 'openai') },
      { id: 'route:both', label: 'Route next invocation to both providers', detail: 'Create one atomic multi-provider batch', keywords: ['claude', 'codex'], disabled: !['anthropic', 'openai'].every((provider) => installedRoutes.some((route) => route.provider_code === provider)) },
      ...activeProviderActivities.map((activity) => ({ id: `activity:${activity.activity_id}`, label: `Open model work · ${activity.members.map((member) => member.spec_id).join(' + ')}`, detail: `${activity.state} · ${projects.find((item) => item.project_id === activity.project_id)?.title ?? activity.project_id}`, keywords: ['activity', 'provider', 'model', activity.state] })),
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
    if (commandId === 'open-workspace' || commandId === 'open-diff') {
      await openWorkspaceDirectory(workspaceDirectory?.path === '.' ? '' : workspaceDirectory?.path ?? '');
      if (commandId === 'open-diff') await loadWorkspaceDiff();
      await tick();
      const surface = document.querySelector<HTMLDetailsElement>('.workspace-surface');
      if (surface) { surface.open = true; surface.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
      return;
    }
    if (commandId === 'send') { await prepare(true); return; }
    if (commandId === 'side-quest') { await beginSideQuest(); return; }
    if (commandId === 'return-parent') { returnToParentEpisode(); return; }
    if (commandId === 'compare') { await compareSelected(); return; }
    if (commandId === 'stop-run') { await stopSelectedRun(); return; }
    if (commandId === 'resume-run') { await resumeSelectedRun(); return; }
    if (commandId === 'retry-selected') { await retrySelected(); return; }
    if (commandId === 'copy-response') { await copySelectedResponse(); return; }
    if (commandId === 'export-invocation') { exportSelectedInvocation(); return; }
    if (commandId.startsWith('search:')) {
      const payload = commandId.slice('search:'.length);
      const sep = payload.indexOf(':');
      const kind = payload.slice(0, sep);
      const objectId = payload.slice(sep + 1);
      const hit = conversationSearchHits.find((item) => item.kind === kind && item.object_id === objectId);
      if (!hit) return;
      if (hit.kind === 'run') await selectRun(hit.object_id);
      else if (hit.invocation_id) await inspect(hit.invocation_id);
      else if (hit.kind === 'invocation') await inspect(hit.object_id);
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
    if (commandId.startsWith('activity:')) {
      const activity = invocationActivity.items.find((item) => item.activity_id === commandId.slice('activity:'.length));
      if (!activity) return;
      if (project?.project_id !== activity.project_id) await openProject(activity.project_id);
      const foreground = activity.members.find((member) => member.dispatch_failure)?.invocation_id
        ?? activity.members.find((member) => member.attempts.some((attempt) => ['leased', 'running'].includes(attempt.state)))?.invocation_id
        ?? activity.members.at(-1)?.invocation_id;
      if (foreground) await inspect(foreground);
      markProjectActivitySeen(activity.project_id);
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

  function adoptInvocationActivitySnapshot(incoming: InvocationActivitySnapshot) {
    const previous = invocationActivity;
    const next = mergeActivitySnapshots(previous, incoming);
    if (activityInitialized) {
      const attention = attentionActivityTransitions(previous, next)[0];
      const terminal = terminalActivityTransitions(previous, next)[0];
      if (attention) {
        activityNotice = `${projects.find((item) => item.project_id === attention.project_id)?.title ?? 'Model work'} needs attention`;
        void notifyOperator('Lattice needs attention', activityNotice, { visible: document.visibilityState === 'visible' });
      } else if (terminal) {
        activityNotice = `${projects.find((item) => item.project_id === terminal.project_id)?.title ?? 'Model work'} ${terminal.state}`;
        void notifyOperator('Lattice work finished', activityNotice, { visible: document.visibilityState === 'visible' });
      }
    }
    invocationActivity = next;
    const selectedId = selected?.invocation_id;
    const selectedMember = memberForInvocation(activityForInvocation(next.items, selectedId), selectedId);
    if (selectedId && selectedMember && ['succeeded', 'failed', 'interrupted'].includes(selectedMember.state) && (selected?.state !== selectedMember.state || selected?.output_block_id !== selectedMember.output_block_id)) {
      void inspect(selectedId);
    }
    if (project) markProjectActivitySeen(project.project_id);
  }

  async function refreshGlobalActivity() {
    const [runResult, invocationResult] = await Promise.allSettled([
      api<DurableRun[]>('/api/v1/runs?limit=200'),
      api<InvocationActivitySnapshot>('/api/v1/invocation-activity?limit=20'),
    ]);
    const failures: string[] = [];
    if (runResult.status === 'fulfilled') {
      if (activityInitialized) {
        const priorStates = new Map(globalRuns.map((run) => [run.run_id, run.state]));
        const changed = runResult.value.find((run) => ['completed', 'failed'].includes(run.state) && priorStates.has(run.run_id) && priorStates.get(run.run_id) !== run.state);
        if (changed) activityNotice = `${changed.name} ${changed.state}`;
      }
      globalRuns = runResult.value;
    } else failures.push(runResult.reason instanceof Error ? runResult.reason.message : String(runResult.reason));
    if (invocationResult.status === 'fulfilled') {
      adoptInvocationActivitySnapshot(invocationResult.value);
      invocationActivityRefreshError = '';
    } else {
      failures.push(invocationResult.reason instanceof Error ? invocationResult.reason.message : String(invocationResult.reason));
    }
    globalActivityError = failures.join(' · ');
    activityInitialized = true;
  }

  async function refreshProjectInvocationActivity(projectId: string) {
    try {
      const incoming = await api<InvocationActivitySnapshot>(`/api/v1/invocation-activity?project_id=${encodeURIComponent(projectId)}&limit=20`);
      if (project?.project_id !== projectId) return;
      adoptInvocationActivitySnapshot(incoming);
      invocationActivityRefreshError = '';
    } catch (cause) {
      if (project?.project_id !== projectId) return;
      invocationActivityRefreshError = cause instanceof Error ? cause.message : String(cause);
    }
  }

  function markProjectActivitySeen(projectId: string) {
    const cursor = invocationActivity.activity_cursors[projectId] ?? 0;
    if (cursor <= (activitySeenCursors[projectId] ?? 0)) return;
    activitySeenCursors = { ...activitySeenCursors, [projectId]: cursor };
    if (project?.project_id === projectId) scheduleUiPersistence();
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
      activitySeenCursors = Object.fromEntries(Object.entries(storedUi.state.projects).map(([projectId, state]) => [projectId, state.lastSeenActivityCursor]));
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
      await refreshGlobalActivity();
      try {
        recovery = recoveryNotice(await api<Parameters<typeof recoveryNotice>[0]>('/api/health'));
      } catch (cause: unknown) {
        console.warn('Lattice recovery status was unavailable:', cause);
        recovery = null;
      }
      const linkedProject = new URLSearchParams(window.location.search).get('project');
      const initialProject = projects.find((item) => item.project_id === (linkedProject ?? storedUi.state.lastProjectId)) ?? projects[0];
      if (initialProject) await openProject(initialProject.project_id);
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
    const generation = ++projectOpenGeneration;
    const switchingProjects = project?.project_id !== projectId;
    if (switchingProjects && uiStateReady) flushUiState();
    if (switchingProjects) projectOpening = true;
    uiStateSuspended = true;
    const storedUi = readUiState(window.localStorage);
    if (storedUi.error) uiPersistenceError = storedUi.error;
    const restored = storedUi.state.projects[projectId] ?? null;
    if (switchingProjects) {
      closeRunEventStream();
      closeRunEventStream = () => {};
      closeInvocationActivityStream();
      closeInvocationActivityStream = () => {};
      selectedRunId = '';
      runEvents = [];
      runs = [];
      runError = '';
    }
    try {
      const openedProject = await api<Project>(`/api/v1/projects/${projectId}`);
      if (generation !== projectOpenGeneration) return;
      project = openedProject;
      try {
        const openedEpisodes = await api<Episode[]>(`/api/v1/projects/${projectId}/episodes`);
        if (generation !== projectOpenGeneration) return;
        episodes = openedEpisodes;
        episodeError = '';
        currentEpisode = restored?.currentEpisodeId ? episodes.find((episode) => episode.slice_id === restored.currentEpisodeId) ?? null : switchingProjects ? null : episodes.find((episode) => episode.slice_id === currentEpisode?.slice_id) ?? null;
      } catch (cause) {
        if (generation !== projectOpenGeneration) return;
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
      promptHistory = restored?.promptHistory ?? [];
      promptHistoryCursor = null;
      liveDraftBeforeHistory = restored?.prompt ?? '';
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
        if (generation !== projectOpenGeneration) return;
        const inspectedInvocationId = (selected as Invocation | null)?.invocation_id ?? '';
        if (restoredInvocationId && inspectedInvocationId !== restoredInvocationId && fallbackInvocationId && fallbackInvocationId !== restoredInvocationId) await inspect(fallbackInvocationId);
      }
      await refreshRuns(projectId);
      if (generation !== projectOpenGeneration) return;
      await refreshProjectInvocationActivity(projectId);
      if (generation !== projectOpenGeneration) return;
      markProjectActivitySeen(projectId);
      watchProjectInvocationActivity(projectId, projectReplayCursor(invocationActivity, projectId));
      viewScrollY = restored?.scrollY ?? 0;
      pendingScrollY = viewScrollY;
    } finally {
      if (generation === projectOpenGeneration) {
        uiStateSuspended = false;
        projectOpening = false;
      }
    }
    if (generation !== projectOpenGeneration) return;
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

  const invocationActivityEventTypes = [
    'invocation-batch.sealed',
    'invocation-batch.dispatch-failed',
    'invocation-batch.recovery-failed',
    'attempt.leased',
    'attempt.running',
    'attempt.chunk',
    'attempt.succeeded',
    'attempt.failed',
    'attempt.interrupted',
    'attempt.orphaned',
    'native-approval.requested',
    'native-approval.decided',
    'native-approval.released',
    'native-approval.withdrawn',
    'native-approval.closed',
  ];

  async function refreshProjectInvocations(projectId: string) {
    const page = await api<{ items: InvocationSummary[]; has_older: boolean; next_before: { created_at: string; invocation_id: string } | null }>(`/api/v1/projects/${encodeURIComponent(projectId)}/invocations?limit=250`);
    if (project?.project_id !== projectId) return;
    project = { ...project, invocations: page.items, invocation_page: { has_older: page.has_older, next_before: page.next_before } };
  }

  function activityMemberForAttempt(attemptId: string) {
    for (const activity of invocationActivity.items) {
      for (const member of activity.members) {
        if (member.attempts.some((attempt) => attempt.attempt_id === attemptId)) return { activity, member };
      }
    }
    return null;
  }

  async function refreshProjectProviderSurface(projectId: string) {
    await refreshProjectInvocationActivity(projectId);
    try {
      await refreshProjectInvocations(projectId);
    } catch (cause) {
      invocationActivityRefreshError = cause instanceof Error ? cause.message : String(cause);
    }
  }

  async function settleInvocationActivity(projectId: string, invocationId: string | null) {
    await refreshProjectProviderSurface(projectId);
    if (project?.project_id === projectId && invocationId && selected?.invocation_id === invocationId) await inspect(invocationId);
    if (project?.project_id === projectId) markProjectActivitySeen(projectId);
  }

  function watchProjectInvocationActivity(projectId: string, after: number) {
    closeInvocationActivityStream();
    invocationActivityStreamError = '';
    const stream = new EventSource(`/api/v1/projects/${encodeURIComponent(projectId)}/events/stream?after=${after}`);
    const receive = (raw: Event) => {
      try {
        const event = JSON.parse((raw as MessageEvent<string>).data) as { seq: number; event_type: string; subject_type: string; subject_id: string; payload: Record<string, unknown> };
        if (project?.project_id !== projectId) return;
        if (event.subject_type === 'attempt') {
          if (event.seq <= (activityEventSequences[event.subject_id] ?? 0)) return;
          activityEventSequences = { ...activityEventSequences, [event.subject_id]: event.seq };
        }
        if (event.subject_type === 'attempt' && typeof event.payload.invocation_id === 'string') {
          activityAttemptInvocations = { ...activityAttemptInvocations, [event.subject_id]: event.payload.invocation_id };
        }
        const resolved = activityMemberForAttempt(event.subject_id);
        if (event.event_type === 'attempt.chunk' && typeof event.payload.preview === 'string') {
          const label = resolved?.member.spec_id ?? event.subject_id.slice(-8);
          const line = `${label} · ${String(event.payload.channel ?? 'output')} · ${event.payload.preview}`;
          activityProgress = { ...activityProgress, [event.subject_id]: [...(activityProgress[event.subject_id] ?? []), line].slice(-200) };
          activityProgressState = { ...activityProgressState, [event.subject_id]: 'Receiving provider journal' };
        } else if (event.event_type === 'attempt.running') {
          activityProgressState = { ...activityProgressState, [event.subject_id]: 'Provider is working' };
        } else if (['attempt.succeeded', 'attempt.failed', 'attempt.interrupted', 'attempt.orphaned'].includes(event.event_type)) {
          activityProgressState = { ...activityProgressState, [event.subject_id]: event.event_type.replace('attempt.', 'Provider ') };
          void settleInvocationActivity(projectId, resolved?.member.invocation_id ?? activityAttemptInvocations[event.subject_id] ?? null);
        } else if (['invocation-batch.dispatch-failed', 'invocation-batch.recovery-failed'].includes(event.event_type)) {
          void settleInvocationActivity(projectId, event.subject_id);
        } else if (event.event_type.startsWith('native-approval.')) {
          activityProgressState = { ...activityProgressState, [event.subject_id]: event.event_type === 'native-approval.requested' ? 'Waiting for your permission' : 'Permission state updated' };
          void settleInvocationActivity(projectId, resolved?.member.invocation_id ?? activityAttemptInvocations[event.subject_id] ?? null);
        } else if (['invocation-batch.sealed', 'attempt.leased'].includes(event.event_type)) {
          void refreshProjectProviderSurface(projectId);
        }
        invocationActivityStreamError = '';
      } catch (cause: unknown) {
        console.warn('Lattice invocation activity event was malformed:', cause);
        invocationActivityStreamError = 'A live provider event was unreadable; durable state remains available through refresh.';
      }
    };
    for (const eventType of invocationActivityEventTypes) stream.addEventListener(eventType, receive);
    stream.onopen = () => { invocationActivityStreamError = ''; };
    stream.onerror = () => { invocationActivityStreamError = 'Live provider updates disconnected; durable state remains available through refresh.'; };
    closeInvocationActivityStream = () => stream.close();
  }

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
      if (project?.project_id !== projectId) return;
      runError = cause instanceof Error ? cause.message : String(cause);
    } finally {
      if (project?.project_id === projectId) runsLoading = false;
    }
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

  function handlePromptHistory(direction: 'previous' | 'next', event: KeyboardEvent) {
    const field = event.currentTarget;
    if (!(field instanceof HTMLTextAreaElement)) return;
    if (direction === 'previous' && field.selectionStart !== 0) return;
    if (direction === 'next' && field.selectionStart !== field.value.length) return;
    event.preventDefault();
    if (direction === 'previous') {
      if (promptHistoryCursor === null) liveDraftBeforeHistory = prompt;
      const older = previousPrompt(promptHistory, promptHistoryCursor);
      if (!older) return;
      prompt = older.prompt;
      promptHistoryCursor = older.cursor;
    } else {
      const newer = nextPrompt(promptHistory, promptHistoryCursor, liveDraftBeforeHistory);
      prompt = newer.prompt;
      promptHistoryCursor = newer.cursor;
    }
    resetPreview();
  }

  async function retrySelected() {
    if (!selected) return;
    const promptSegment = selected.manifest.segments.find((segment) => segment.plane === 'current_prompt');
    if (promptSegment) {
      try {
        const block = await api<{ content: string }>(`/api/v1/context-blocks/${encodeURIComponent(promptSegment.block_id)}`);
        prompt = block.content;
      } catch (cause) {
        error = cause instanceof Error ? cause.message : String(cause);
      }
    }
    if (['failed', 'interrupted'].includes(selected.state)) {
      selectedParents = new Set(selected.manifest.segments.filter((segment) => segment.plane === 'ancestry' && typeof segment.producing_invocation_id === 'string').map((segment) => String(segment.producing_invocation_id)));
    } else {
      selectedParents = new Set([selected.invocation_id]);
    }
    resetPreview();
    await tick();
    document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
  }

  async function copySelectedResponse() {
    if (!selected?.output_block?.content) return;
    try {
      await copyText(selected.output_block.content, navigator.clipboard);
      copyReceipt = 'Response copied.';
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
    }
  }

  function exportSelectedInvocation() {
    if (!selected) return;
    const payload = invocationExportPayload(selected);
    const blob = new Blob([payload], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${selected.invocation_id}.json`;
    link.click();
    URL.revokeObjectURL(url);
    copyReceipt = 'Invocation exported.';
  }

  async function openFileLink(path: string, line: number | null) {
    workspaceSearchQuery = path;
    await openWorkspaceFile(path, line);
  }

  async function searchConversation(query: string) {
    if (!project || query.trim().length < 2) {
      conversationSearchHits = [];
      conversationSearchError = '';
      return;
    }
    try {
      const result = await api<{ items: LatticeSearchHit[] }>(`/api/v1/projects/${encodeURIComponent(project.project_id)}/search?q=${encodeURIComponent(query.trim())}`);
      conversationSearchHits = result.items;
      conversationSearchError = '';
    } catch (cause) {
      conversationSearchError = cause instanceof Error ? cause.message : String(cause);
    }
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
    const submittedUiState = currentProjectUiState();
    working = true;
    error = '';
    try {
      const batch = await post<InvocationBatch>('/api/v1/invocation-batches', {
        ...pendingBatchRequest,
        idempotency_key: batchIdempotencyKey(String(pendingBatchRequest.batch_id)),
        expected_preview_sha256: preview.preview_sha256,
        confirmed_reason_sha256s: confirmedReasonSha256s,
      });
      promptHistory = recordPrompt(promptHistory, submittedUiState?.prompt ?? prompt);
      promptHistoryCursor = null;
      const foregroundInvocationId = batch.invocation_ids.at(-1);
      const clearedUiState = submittedUiState ? { ...submittedUiState, prompt: '', explicitSelections: [], selectedParentIds: foregroundInvocationId ? [foregroundInvocationId] : submittedUiState.selectedParentIds, selectedInvocationId: foregroundInvocationId ?? submittedUiState.selectedInvocationId, updatedAt: Date.now() } : null;
      if (project?.project_id === projectId) {
        preview = null;
        pendingBatchRequest = null;
        if (submittedUiState) {
          const reconciled = reconcileSubmittedComposer(
            { prompt, explicitSelections, selectedParentIds: [...selectedParents] },
            submittedUiState,
            foregroundInvocationId,
          );
          prompt = reconciled.prompt;
          explicitSelections = reconciled.explicitSelections;
          selectedParents = new Set(reconciled.selectedParentIds);
        }
      } else if (clearedUiState) {
        const persisted = writeProjectUiState(window.localStorage, projectId, clearedUiState, { activate: false });
        if (persisted.error) uiPersistenceError = persisted.error;
      }
      await refreshProjectProviderSurface(projectId);
      if (project?.project_id === projectId && foregroundInvocationId) await inspect(foregroundInvocationId);
      await tick();
      document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus();
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : String(cause);
      if (project?.project_id === projectId) error = detail;
      else activityNotice = `${projects.find((item) => item.project_id === projectId)?.title ?? 'Background model work'} did not launch`;
    } finally { working = false; }
  }

  async function cancelActiveAttempts() {
    if (!selectedActiveAttemptIds.length) return;
    error = '';
    try {
      await Promise.all(selectedActiveAttemptIds.map((attemptId) => post(`/api/v1/attempts/${attemptId}/cancel`, {})));
      activityProgressState = Object.fromEntries(Object.entries(activityProgressState).concat(selectedActiveAttemptIds.map((attemptId) => [attemptId, 'Cancellation requested; waiting for the terminal receipt'])));
    } catch (cause) { error = cause instanceof Error ? cause.message : String(cause); }
  }

  async function decideNativeApproval(request: NativeApproval, decision: 'allow' | 'deny') {
    if (approvalBusy.includes(request.request_id)) return;
    const projectId = project?.project_id;
    approvalBusy = [...approvalBusy, request.request_id];
    approvalErrors = { ...approvalErrors, [request.request_id]: '' };
    try {
      await post(`/api/v1/attempts/${encodeURIComponent(request.binding.attempt_id)}/native-approvals/${encodeURIComponent(request.request_id)}/decision`, { request_sha256: request.request_sha256, decision });
      if (projectId) await settleInvocationActivity(projectId, request.binding.invocation_id);
    } catch (cause: unknown) {
      approvalErrors = { ...approvalErrors, [request.request_id]: cause instanceof Error ? cause.message : String(cause) };
    } finally {
      approvalBusy = approvalBusy.filter((id) => id !== request.request_id);
    }
  }

  async function cancelApprovalAttempt(attemptId: string) {
    try {
      await post(`/api/v1/attempts/${encodeURIComponent(attemptId)}/cancel`, {});
      if (project) await settleInvocationActivity(project.project_id, selected?.invocation_id ?? null);
    } catch (cause: unknown) { error = cause instanceof Error ? cause.message : String(cause); }
  }

  async function inspect(id: string) {
    const generation = ++inspectGeneration;
    error = '';
    selectedExcerpt = null;
    selectionProofLimit = '';
    armedTransition = '';
    try {
      const invocation = await api<Invocation>(`/api/v1/invocations/${id}`);
      if (generation !== inspectGeneration) return;
      const [neighborhoodResult, navigationResult] = await Promise.allSettled([
        api<{ nodes: InvocationSummary[]; edges: Array<Record<string, unknown>>; coverage: { state: 'complete' | 'partial'; requested_depth: number; completed_depth: number; node_limit: number; edge_limit: number } }>(`/api/v1/invocations/${id}/neighborhood?depth=2`),
        api<InvocationNavigation>(`/api/v1/invocations/${id}/navigation`),
      ]);
      if (generation !== inspectGeneration) return;
      selected = invocation;
      neighborhood = null;
      selectedNavigation = null;
      selectedSlateSlice = null;
      neighborhoodError = '';
      navigationError = '';
      slateSliceError = '';
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
    closeInvocationActivityStream();
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
      <button class="activity-status" class:notice={Boolean(activityNotice)} class:failed={Boolean(activitySurfaceError)} title={activitySurfaceError || 'Open projects, active work, and cockpit commands'} on:click={() => { paletteOpen = true; activityNotice = ''; }}>
        {#if activitySurfaceError}activity delayed{:else if activityNotice}{activityNotice}{:else if activeGlobalRuns.length + activeProviderActivities.length}{activeGlobalRuns.length + activeProviderActivities.length} open · ⌘K{:else}quiet · ⌘K{/if}
      </button>
      <div class="route-status">{durableSelection ? `Conversation · ${durableSelection.model ?? 'Loading route…'}` : routeCatalogueError ? `Provider catalogue unavailable · ${routeCatalogueError}` : activeRoute ? `${activeRoute.official_cli} · ${activeRoute.model_code}` : 'No qualified route'}</div>
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
        activityBadges={projectActivity}
        onOpenProject={openProject}
        onCreateProject={createProject}
        onSetWorkflowSelectorValue={setWorkflowSelectorValue}
        onCreateWorkflowProject={createWorkflowProject}
      />

      <main class="conversation">
        {#if projectOpening}
          <div class="empty-call standalone"><span>Opening project</span><h3>Restoring its exact conversation state.</h3></div>
        {:else if project}
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
          {#if recovery}
            <div class="recovery-banner">
              <strong>{recovery.title}</strong>
              <p>What survived</p>
              <ul>{#each recovery.survived as item}<li>{item}</li>{/each}</ul>
              {#if recovery.mustRepeat.length}
                <p>What must be repeated</p>
                <ul>{#each recovery.mustRepeat as item}<li>{item}</li>{/each}</ul>
              {/if}
              <button class="quiet" on:click={() => recovery = null}>Dismiss</button>
            </div>
          {/if}
          {#if uiPersistenceError}<p class="surface-failure">{uiPersistenceError} The current draft remains available only while this tab stays open.</p>{/if}
          {#if copyReceipt}<p class="muted">{copyReceipt}</p>{/if}
          {#if conversationSearchError}<p class="surface-failure">Search unavailable: {conversationSearchError}</p>{/if}
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

          {#key project.project_id}
            <DurableConversations projectId={project.project_id} getRoute={() => {
              if (routeChoices.length !== 1) throw new Error('Choose one provider in the current route before creating a child conversation.');
              return routePayload(routeChoices[0]);
            }} getContextBlockIds={() => explicitSelections.filter(s=>s.disposition===undefined || s.disposition==='selected').map(s=>String(s.block_id))} onSelection={(selection) => { durableSelection = selection; }} />
          {/key}
          {#if !durableConversationSelected}
          <ResponsePane
            {selected}
            activeAttemptIds={selectedActiveAttemptIds}
            liveProgress={selectedLiveProgress}
            liveProgressState={selectedLiveProgressState}
            activityFailure={selectedActivityFailure}
            {working}
            {selectedNativeEligible}
            {currentEpisode}
            {parentEpisode}
            {selectedExcerpt}
            {selectionProofLimit}
            {terminalFailure}
            onRetrySelected={retrySelected}
            onCopyResponse={copySelectedResponse}
            onExportInvocation={exportSelectedInvocation}
            onOpenFileLink={openFileLink}
            onCancelActive={cancelActiveAttempts}
            onBeginSideQuest={beginSideQuest}
            onGraftResponse={graftResponseBlock}
            onConfigureNative={configureNativeContinuation}
            onReturnToParent={returnToParentEpisode}
            onGraftExcerpt={graftSelectedExcerpt}
          />

          <NativeApprovals requests={selectedApprovalRequests} workspace={String(selected?.route.workspace ?? '')} busy={approvalBusy} errors={approvalErrors} onDecide={decideNativeApproval} onCancel={cancelApprovalAttempt} onRefresh={inspect} />

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
            onPromptHistory={handlePromptHistory}
          />
          {/if}
        {:else}
          <div class="empty-call standalone"><span>No project yet</span><h3>Create the first operational thread.</h3></div>
        {/if}
      </main>

      {#if projectOpening}
        <aside class="inspector"><div class="section-label">Context truth</div><p class="muted">Available when the project has finished opening.</p></aside>
      {:else if durableConversationSelected}
        <ConversationInspector invocationId={durableSelection?.invocationId ?? null} />
      {:else}
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
      {/if}
    </div>
  </div>
{/if}

<CommandPalette open={paletteOpen} commands={paletteCommands} onClose={() => { paletteOpen = false; conversationSearchHits = []; }} onChoose={executePaletteCommand} onQuery={searchConversation} />
