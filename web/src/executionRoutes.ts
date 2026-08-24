import type { Invocation, Route } from './types';

export type ExecutionProfile = 'compiled-fresh' | 'workspace-agent' | 'native-continuation';
export type ContinuationMode = 'resume' | 'fork';
export type WorkspaceAuthority = 'read-only' | 'workspace-write';
export type Effort = 'low' | 'medium' | 'high' | 'xhigh' | 'max';

export type RouteDraft = {
  executionProfile: ExecutionProfile;
  continuationMode: ContinuationMode;
  continuationSource: Invocation | null;
  workspacePath: string;
  workspaceAuthority: WorkspaceAuthority;
  claudeShell: boolean;
  effort: Effort;
};

export function buildRoutePayload(route: Route, draft: RouteDraft): Record<string, unknown> {
  if (draft.executionProfile === 'native-continuation') {
    if (!draft.continuationSource) throw new Error('Choose a provider-backed response to continue or fork.');
    const source = draft.continuationSource.route;
    if (source.provider !== route.provider_code || source.model !== route.model_code || source.harness !== route.official_cli) throw new Error('Native continuation must use the source response’s provider, model, and harness.');
    return {
      ...source,
      execution_profile: 'native-continuation',
      session_policy: 'visible_native_continuation',
      continuation_mode: draft.continuationMode,
      native_parent_invocation_id: draft.continuationSource.invocation_id,
      native_session_id: null,
      effort: draft.effort,
    };
  }
  if (draft.executionProfile === 'workspace-agent') {
    if (!draft.workspacePath.trim()) throw new Error('Workspace execution requires an absolute project folder.');
    const claudeTools = ['Read', 'Glob', 'Grep', ...(draft.workspaceAuthority === 'workspace-write' ? ['Edit', 'Write'] : []), ...(draft.claudeShell ? ['Bash'] : [])];
    return {
      provider: route.provider_code,
      model: route.model_code,
      harness: route.official_cli,
      effort: draft.effort,
      execution_profile: 'workspace-agent',
      session_policy: 'fresh',
      continuation_mode: null,
      native_parent_invocation_id: null,
      native_session_id: null,
      tools: route.provider_code === 'anthropic' ? claudeTools : ['shell'],
      permissions: route.provider_code === 'anthropic' ? claudeTools : [],
      workspace: draft.workspacePath.trim(),
      sandbox: route.provider_code === 'anthropic' ? 'provider-permissions' : draft.workspaceAuthority,
      approval_policy: 'never',
      network_policy: route.provider_code === 'anthropic' && draft.claudeShell ? 'provider_managed' : 'disabled',
    };
  }
  return {
    provider: route.provider_code,
    model: route.model_code,
    harness: route.official_cli,
    effort: draft.effort,
    execution_profile: 'compiled-fresh',
    session_policy: 'fresh',
    continuation_mode: null,
    native_parent_invocation_id: null,
    native_session_id: null,
    tools: [],
    permissions: [],
    workspace: null,
    sandbox: 'read-only',
    approval_policy: 'never',
    network_policy: 'disabled',
  };
}
