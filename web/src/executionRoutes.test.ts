import { describe, expect, it } from 'vitest';
import { applyModelOverride, buildRoutePayload, type RouteDraft } from './executionRoutes';
import type { Invocation, Route } from './types';

const codex: Route = { provider_code: 'openai', model_code: 'gpt-5.6-sol', official_cli: 'codex', installed: true, qualified: true, operational: true, proof_limits: [], qualified_profiles: ['compiled-fresh', 'workspace-agent', 'native-continuation'] };
const claude: Route = { provider_code: 'anthropic', model_code: 'claude-opus-5', official_cli: 'claude', installed: true, qualified: true, operational: true, proof_limits: [], qualified_profiles: ['compiled-fresh', 'workspace-agent', 'native-continuation'] };
const baseline: RouteDraft = { executionProfile: 'compiled-fresh', continuationMode: 'resume', continuationSource: null, workspacePath: '', workspaceAuthority: 'read-only', claudeShell: false, effort: 'high' };

describe('execution route construction', () => {
  it('keeps fresh conversation free of ambient workspace authority', () => {
    expect(buildRoutePayload(codex, baseline)).toMatchObject({ execution_profile: 'compiled-fresh', tools: [], workspace: null, sandbox: 'read-only', network_policy: 'disabled' });
  });

  it('makes provider-specific workspace authority exact and visible', () => {
    const draft = { ...baseline, executionProfile: 'workspace-agent' as const, workspacePath: '/project', workspaceAuthority: 'workspace-write' as const, claudeShell: true };
    expect(buildRoutePayload(codex, draft)).toMatchObject({ tools: ['shell'], permissions: [], sandbox: 'workspace-write', network_policy: 'disabled' });
    expect(buildRoutePayload(claude, draft)).toMatchObject({ tools: ['Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash'], permissions: ['Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash'], sandbox: 'provider-permissions', network_policy: 'provider_managed' });
  });

  it('rejects a hidden workspace and a cross-provider native continuation', () => {
    expect(() => buildRoutePayload(codex, { ...baseline, executionProfile: 'workspace-agent' })).toThrow('absolute project folder');
    const source = { invocation_id: 'invocation_source', route: { provider: 'anthropic', model: 'claude-opus-5', harness: 'claude', execution_profile: 'workspace-agent', workspace: '/project' } } as unknown as Invocation;
    expect(() => buildRoutePayload(codex, { ...baseline, executionProfile: 'native-continuation', continuationSource: source })).toThrow('provider, model, and harness');
  });

  it('binds native continuation to the exact visible parent without trusting a supplied session id', () => {
    const source = { invocation_id: 'invocation_source', route: { provider: 'openai', model: 'gpt-5.6-sol', harness: 'codex', execution_profile: 'workspace-agent', workspace: '/project', tools: ['shell'], sandbox: 'read-only' } } as unknown as Invocation;
    expect(buildRoutePayload(codex, { ...baseline, executionProfile: 'native-continuation', continuationMode: 'fork', continuationSource: source })).toMatchObject({ execution_profile: 'native-continuation', continuation_mode: 'fork', native_parent_invocation_id: 'invocation_source', native_session_id: null, workspace: '/project' });
  });

  it('makes an explicit advanced model choice part of the route bytes', () => {
    const configured = applyModelOverride(codex, 'gpt-5.6-codex');
    expect(buildRoutePayload(configured, baseline)).toMatchObject({ provider: 'openai', model: 'gpt-5.6-codex', harness: 'codex' });
    expect(applyModelOverride(codex, '   ')).toBe(codex);
  });
});
