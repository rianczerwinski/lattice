export const UI_STATE_STORAGE_KEY = 'lattice.ui-state.v1';

export type PersistedExecutionProfile = 'compiled-fresh' | 'workspace-agent';

export type ProjectUiState = {
  prompt: string;
  selectedSession: string;
  selectedRunId: string;
  selectedInvocationId: string;
  selectedParentIds: string[];
  explicitSelections: Array<Record<string, unknown>>;
  currentEpisodeId: string;
  routeMode: 'anthropic' | 'openai' | 'both';
  executionProfile: PersistedExecutionProfile;
  workspacePath: string;
  workspaceAuthority: 'read-only' | 'workspace-write';
  claudeShell: boolean;
  effort: 'low' | 'medium' | 'high' | 'xhigh' | 'max';
  modelOverrides: Record<string, string>;
  scrollY: number;
  updatedAt: number;
};

export type UiState = {
  version: 1;
  lastProjectId: string;
  recentProjectIds: string[];
  projects: Record<string, ProjectUiState>;
};

type StoragePort = Pick<Storage, 'getItem' | 'setItem'>;
export type UiStateResult = { state: UiState; error: string };

const MAX_RECENT_PROJECTS = 12;

function emptyState(): UiState {
  return { version: 1, lastProjectId: '', recentProjectIds: [], projects: {} };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function exactString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function normalizeProjectState(value: unknown): ProjectUiState | null {
  if (!isRecord(value)) return null;
  const routeMode = ['anthropic', 'openai', 'both'].includes(String(value.routeMode)) ? value.routeMode as ProjectUiState['routeMode'] : 'anthropic';
  const executionProfile = value.executionProfile === 'workspace-agent' ? 'workspace-agent' : 'compiled-fresh';
  const workspaceAuthority = value.workspaceAuthority === 'workspace-write' ? 'workspace-write' : 'read-only';
  const effort = ['low', 'medium', 'high', 'xhigh', 'max'].includes(String(value.effort)) ? value.effort as ProjectUiState['effort'] : 'high';
  const selections = Array.isArray(value.explicitSelections) ? value.explicitSelections.filter(isRecord) : [];
  const overrides = isRecord(value.modelOverrides)
    ? Object.fromEntries(Object.entries(value.modelOverrides).filter((entry): entry is [string, string] => typeof entry[1] === 'string'))
    : {};
  return {
    prompt: exactString(value.prompt),
    selectedSession: exactString(value.selectedSession),
    selectedRunId: exactString(value.selectedRunId),
    selectedInvocationId: exactString(value.selectedInvocationId),
    selectedParentIds: stringList(value.selectedParentIds),
    explicitSelections: selections,
    currentEpisodeId: exactString(value.currentEpisodeId),
    routeMode,
    executionProfile,
    workspacePath: exactString(value.workspacePath),
    workspaceAuthority,
    claudeShell: value.claudeShell === true,
    effort,
    modelOverrides: overrides,
    scrollY: typeof value.scrollY === 'number' && Number.isFinite(value.scrollY) && value.scrollY >= 0 ? value.scrollY : 0,
    updatedAt: typeof value.updatedAt === 'number' && Number.isFinite(value.updatedAt) ? value.updatedAt : 0,
  };
}

export function readUiState(storage: StoragePort): UiStateResult {
  try {
    const raw = storage.getItem(UI_STATE_STORAGE_KEY);
    if (!raw) return { state: emptyState(), error: '' };
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value) || value.version !== 1 || !isRecord(value.projects)) {
      return { state: emptyState(), error: 'Saved workspace state has an unsupported shape; this tab will use fresh view state.' };
    }
    const projects = Object.fromEntries(Object.entries(value.projects).flatMap(([projectId, candidate]) => {
      const normalized = normalizeProjectState(candidate);
      return normalized ? [[projectId, normalized]] : [];
    }));
    return {
      state: {
        version: 1,
        lastProjectId: exactString(value.lastProjectId),
        recentProjectIds: stringList(value.recentProjectIds).slice(0, MAX_RECENT_PROJECTS),
        projects,
      },
      error: '',
    };
  } catch (cause: unknown) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    return { state: emptyState(), error: `Saved workspace state could not be read: ${detail}` };
  }
}

export function writeProjectUiState(storage: StoragePort, projectId: string, projectState: ProjectUiState): UiStateResult {
  const loaded = readUiState(storage);
  const recentProjectIds = [projectId, ...loaded.state.recentProjectIds.filter((item) => item !== projectId)].slice(0, MAX_RECENT_PROJECTS);
  const state: UiState = {
    ...loaded.state,
    lastProjectId: projectId,
    recentProjectIds,
    projects: { ...loaded.state.projects, [projectId]: normalizeProjectState(projectState) ?? projectState },
  };
  try {
    storage.setItem(UI_STATE_STORAGE_KEY, JSON.stringify(state));
    return { state, error: '' };
  } catch (cause: unknown) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    return { state: loaded.state, error: `Draft and view state could not be saved: ${detail}` };
  }
}

export function orderByRecentProjects<T extends { project_id: string }>(projects: T[], recentProjectIds: string[]): T[] {
  const rank = new Map(recentProjectIds.map((projectId, index) => [projectId, index]));
  return projects
    .map((project, index) => ({ project, index, rank: rank.get(project.project_id) ?? Number.POSITIVE_INFINITY }))
    .sort((left, right) => left.rank - right.rank || left.index - right.index)
    .map(({ project }) => project);
}
