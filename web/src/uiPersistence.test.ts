import { describe, expect, it } from 'vitest';
import { orderByRecentProjects, readUiState, UI_STATE_STORAGE_KEY, writeProjectUiState, type ProjectUiState } from './uiPersistence';

function projectState(overrides: Partial<ProjectUiState> = {}): ProjectUiState {
  return {
    prompt: 'unfinished exact draft',
    selectedSession: 'session_1',
    selectedRunId: 'run_1',
    selectedInvocationId: 'invocation_1',
    selectedParentIds: ['parent_1'],
    explicitSelections: [{ block_id: 'block_1', disposition: 'selected' }],
    currentEpisodeId: 'episode_1',
    routeMode: 'both',
    executionProfile: 'workspace-agent',
    workspacePath: '/workspace',
    workspaceAuthority: 'workspace-write',
    claudeShell: true,
    effort: 'xhigh',
    modelOverrides: { anthropic: 'claude-exact' },
    scrollY: 413,
    updatedAt: 42,
    ...overrides,
  };
}

describe('per-project UI persistence contract', () => {
  it('round-trips an unfinished draft and its exact composition state', () => {
    const values = new Map<string, string>();
    const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };

    expect(writeProjectUiState(storage, 'project_1', projectState()).error).toBe('');

    const restored = readUiState(storage).state;
    expect(restored.lastProjectId).toBe('project_1');
    expect(restored.projects.project_1).toEqual(projectState());
  });

  it('keeps recent-project ordering stable without duplicating a reopened project', () => {
    const values = new Map<string, string>();
    const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
    writeProjectUiState(storage, 'project_a', projectState());
    writeProjectUiState(storage, 'project_b', projectState());
    const recent = writeProjectUiState(storage, 'project_a', projectState()).state.recentProjectIds;

    expect(recent).toEqual(['project_a', 'project_b']);
    expect(orderByRecentProjects([{ project_id: 'project_b' }, { project_id: 'project_c' }, { project_id: 'project_a' }], recent).map((item) => item.project_id)).toEqual(['project_a', 'project_b', 'project_c']);
  });

  it('refuses corrupt storage as fresh state and reports why persistence is unavailable', () => {
    const storage = {
      getItem: () => '{not-json',
      setItem: () => { throw new DOMException('quota full', 'QuotaExceededError'); },
    };

    expect(readUiState(storage).error).toContain('could not be read');
    expect(readUiState(storage).state.projects).toEqual({});
    expect(writeProjectUiState(storage, 'project_1', projectState()).error).toContain('could not be saved');
  });

  it('downgrades unrestorable native continuation state to a fresh execution profile', () => {
    const raw = projectState() as unknown as Record<string, unknown>;
    raw.executionProfile = 'native-continuation';
    const storage = {
      getItem: () => JSON.stringify({ version: 1, lastProjectId: 'project_1', recentProjectIds: ['project_1'], projects: { project_1: raw } }),
      setItem: (_key: string, _value: string) => undefined,
    };

    expect(readUiState(storage).state.projects.project_1.executionProfile).toBe('compiled-fresh');
  });

  it('uses one namespaced browser key', () => {
    expect(UI_STATE_STORAGE_KEY).toBe('lattice.ui-state.v1');
  });
});
