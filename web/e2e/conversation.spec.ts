import { expect, test, type Page, type Route as BrowserRoute } from '@playwright/test';

type InvocationRecord = {
  invocation_id: string;
  project_id: string;
  state: 'succeeded';
  actor_kind: 'direct';
  role_session_id: null;
  manifest_sha256: string;
  output_block_id: string;
  terminal_kind: 'succeeded';
  created_at: string;
  completed_at: string;
  response: string;
  route: Record<string, unknown>;
  concern_refs: string[];
};

type EpisodeRecord = {
  slice_id: string;
  episode_id: string;
  concern_ref: string;
  parent_episode_id: string | null;
  name: string;
  items: Array<{ object_id: string; object_type: string }>;
  coverage: { state: 'complete' };
};

function json(route: BrowserRoute, value: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
}

async function installDeterministicLattice(page: Page, options: { secondaryUnavailable?: boolean; workflowUnavailable?: boolean; nativeApproval?: boolean; lostChildAcknowledgement?: boolean; uncertainChildEffect?: boolean; limitedChildContext?:boolean; onChildContinue?: (request:Record<string,unknown>)=>void; lostChildCreationAcknowledgement?: boolean; onChildCreation?: (key:string)=>void; onChildCreationRequest?: (request:Record<string,unknown>)=>void } = {}) {
  let project: Record<string, unknown> | null = null;
  const invocations: InvocationRecord[] = [];
  const episodes: EpisodeRecord[] = [];
  const slateTopics = new Map<string, { slice_id: string; external_ref: string; name: string; scope: 'global'; items: Array<{ object_id: string; object_type: string }> }>();
  const blockOwner = new Map<string, string>();
  let child: { conversation_id:string;title:string;owning_task_ref:string;revision:number;parent_conversation_id:null;route:Record<string,unknown>;rounds:Array<{round_id:string;ordinal:number;state:string;request:{prompt:string};response?:string;executions:[]}>;pending:{round_id:string;ordinal:number;state:string}|null;has_more:false;event_cursor:number;steering:Array<{steering_id:string;content:string}>;checkpoint:{checkpoint_id:string;content:{artifacts:[]}}|null } | null = null;
  const childReceipts=new Map<string,unknown>();
  let loseAcknowledgement=options.lostChildAcknowledgement ?? false;
  let loseCreationAcknowledgement=options.lostChildCreationAcknowledgement ?? false;
  const creationReceipts=new Map<string,unknown>();
  let sequence = 0;
  let secondaryUnavailable = options.secondaryUnavailable ?? false;
  let nativeDecision: 'allow' | 'deny' | null = null;
  let nativeDecisionUnavailable = false;

  const routes = [
    { provider_code: 'anthropic', model_code: 'claude-sonnet-4-6', official_cli: 'claude', installed: true, qualified: true, operational: true, proof_limits: ['provider-owned system context remains opaque'], qualified_profiles: ['compiled-fresh', 'workspace-agent', 'native-continuation'] },
    { provider_code: 'openai', model_code: 'gpt-5.6-sol', official_cli: 'codex', installed: true, qualified: true, operational: true, proof_limits: ['provider-owned system context remains opaque'], qualified_profiles: ['compiled-fresh', 'workspace-agent', 'native-continuation'] },
  ];

  const summary = (item: InvocationRecord) => ({ invocation_id: item.invocation_id, project_id: item.project_id, actor_kind: item.actor_kind, role_session_id: item.role_session_id, state: item.state, manifest_sha256: item.manifest_sha256, output_block_id: item.output_block_id, terminal_kind: item.terminal_kind, created_at: item.created_at, completed_at: item.completed_at });
  const projectView = () => ({
    ...project,
    roles: [],
    sessions: [],
    context_defaults: [],
    workflow: null,
    invocations: invocations.map(summary),
    invocation_page: { has_older: false, next_before: null },
    trailheads: invocations.slice(-3).reverse().map(summary),
    workflow_templates: [],
    workflow_templates_partial: false,
  });
  const invocationView = (item: InvocationRecord) => ({
    ...summary(item),
    route: item.route,
    response_schema: { type: 'object' },
    actor_binding: { kind: 'direct' },
    compiled_prompt: `compiled:${item.response}`,
    manifest_verification: { valid: true },
    manifest: {
      invocation_id: item.invocation_id,
      manifest_sha256: item.manifest_sha256,
      segments: [{ block_id: `prompt_${item.invocation_id}`, plane: 'current_prompt', label: 'current prompt', disposition: 'selected', sha256: '1'.repeat(64), status: 'active', semantic_type: 'human-prompt' }],
      omitted: [],
      execution: item.route,
      response_contract: { type: 'object' },
      capabilities: item.route,
      provider_boundary: { proof_limits: ['provider-owned system context remains opaque'] },
      compilation: { byte_count: item.response.length, compiled_prompt_sha256: '2'.repeat(64) },
    },
    output_block: { content: item.response },
    attempts: [{ state: options.nativeApproval ? 'running' : 'succeeded', runtime_receipt: { native_session: null }, ...(options.nativeApproval ? { native_approvals: {
      attempt_id: 'attempt_native_fixture', invocation_id: item.invocation_id, pending_count: nativeDecision ? 0 : 1, closed_reason: null,
      requests: [{ request_id: 'approval_native_fixture', request_sha256: 'a'.repeat(64), state: nativeDecision ? 'released-to-adapter' : 'awaiting-operator',
        binding: { attempt_id: 'attempt_native_fixture', invocation_id: item.invocation_id, manifest_sha256: item.manifest_sha256, action: { tool_use_id: 'tool_native_fixture', tool_name: 'Read', tool_input: { file_path: '/planted/private.txt', offset: 17, limit: 9 }, context: { description: '<img src=x onerror=alert(1)> Read a planted source' } } },
        decision: nativeDecision ? { decision: nativeDecision, note: '' } : null, release: nativeDecision ? { delivery_claim: 'released-to-adapter' } : null, withdrawal: null }],
    } } : {}) }],
    artifacts: [],
    edges: [],
  });
  const navigation = (item: InvocationRecord) => ({
    contract_version: 'LatticeInvocationNavigation/1.0',
    invocation: { invocation_id: item.invocation_id },
    episodes: episodes.filter((episode) => episode.items.some((member) => member.object_type === 'invocation' && member.object_id === item.invocation_id)),
    project: { project_id: item.project_id, title: String(project?.title ?? '') },
    slate_topics: [...slateTopics.values()].filter((topic) => topic.items.some((member) => (member.object_type === 'invocation' && member.object_id === item.invocation_id) || (member.object_type === 'context-block' && blockOwner.get(member.object_id) === item.invocation_id))).map(({ items: _items, ...topic }) => topic),
  });

  await page.route('**/api/**', async (intercepted) => {
    const request = intercepted.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const body = request.postData() ? request.postDataJSON() as Record<string, any> : {};

    if (path === '/api/v1/conversations' && request.method()==='GET') return json(intercepted,child ? [child] : []);
    if (path === '/api/v1/conversations' && request.method()==='POST') {
      options.onChildCreationRequest?.(body);
      const key=String(body.idempotency_key);
      const retained=creationReceipts.get(key);
      if (retained) return json(intercepted,retained);
      options.onChildCreation?.(key);
      child={conversation_id:'conversation_browser',title:String(body.title),owning_task_ref:String(body.owning_task_ref),revision:1,parent_conversation_id:null,route:body.route as Record<string,unknown>,rounds:[],pending:null,has_more:false,event_cursor:1,steering:[],checkpoint:null};
      const receipt={conversation_id:child.conversation_id,revision:1};
      creationReceipts.set(key,receipt);
      if (loseCreationAcknowledgement) { loseCreationAcknowledgement=false; return intercepted.abort('connectionreset'); }
      return json(intercepted,receipt);
    }
    if (path.startsWith('/api/v1/conversations/conversation_browser') && child) {
      const action=path.split('/')[5];
      if (!action) return json(intercepted,child);
      if (action==='observations') return json(intercepted,{items:[],has_more:false,cursor:0});
      if (action==='recovery-preview') {
        const selected=body.context_selection?.omitted_round_ids?.includes('round_1') && body.context_selection.reason?.trim();
        return json(intercepted,{revision:child.revision,preview_sha256:(selected?'b':'a').repeat(64),mode:'portable',maximum_remaining_loss:'Unexposed provider computation cannot be recovered.',
          ...(options.limitedChildContext ? {available_context:{rounds:[{round_id:'round_1',ordinal:1}],observations:[],artifacts:[],blocks:[]},required_confirmations:selected?[{code:'context.operator-selection',detail:{selection:body.context_selection},reason_sha256:'c'.repeat(64)}]:[]} : {}),
          blockers:options.limitedChildContext && !selected ? [{code:'provider_context_limit',detail:'Select smaller retained context before continuing.'}] : options.uncertainChildEffect && !body.effect_dispositions?.length ? [{code:'uncertain_mutating_effect',detail:'Inspect the unconfirmed action.',effects:[{attempt_id:'attempt_effect',tool_id:'effect:0',request:{name:'Bash',input:{command:'write an external record'}}}]}] : []});
      }
      const retainedReceipt=childReceipts.get(String(body.idempotency_key));
      if (retainedReceipt) return json(intercepted,retainedReceipt);
      if (body.expected_revision!==child.revision) return json(intercepted,{detail:'conversation revision changed'},409);
      child.revision+=1;
      if (action==='send') {
        const ordinal=child.rounds.length+1;
        const round={round_id:`round_${ordinal}`,ordinal,state:ordinal===1?'completed':'active',request:{prompt:String(body.prompt)},executions:[] as []};
        child.rounds.push(ordinal===1 ? {...round,response:'First retained answer.\n\n'+Array.from({length:80},(_,i)=>`Retained paragraph ${i}.`).join('\n\n')} : round);
        child.pending=ordinal===1?null:{round_id:round.round_id,ordinal,state:'active'};
        child.checkpoint={checkpoint_id:'checkpoint_first',content:{artifacts:[]}};
      }
      if (action==='steer') child.steering.push({steering_id:'steering_one',content:String(body.content)});
      if (action==='interrupt' && child.pending) { child.pending.state='needs_attention';child.rounds.at(-1)!.state='needs_attention'; }
      if (action==='continue') {
        if (options.limitedChildContext && (!body.context_selection?.omitted_round_ids?.includes('round_1') || !body.confirmed_reason_sha256s?.includes('c'.repeat(64)))) return json(intercepted,{detail:'exact context selection was not confirmed'},409);
        options.onChildContinue?.(body);
        child.rounds.at(-1)!.state='completed';child.rounds.at(-1)!.response='Recovered using the saved conversation.';child.pending=null;child.steering=[];
      }
      const receipt={conversation_id:child.conversation_id,revision:child.revision,event_cursor:child.revision};
      childReceipts.set(String(body.idempotency_key),receipt);
      if (action==='send' && loseAcknowledgement) { loseAcknowledgement=false;return intercepted.abort('connectionreset'); }
      return json(intercepted,receipt);
    }

    if (path === '/api/v1/attempts/attempt_native_fixture/native-approvals/approval_native_fixture/decision') {
      if (nativeDecisionUnavailable) return json(intercepted, { detail: 'planted decision service outage' }, 503);
      if (body.request_sha256 !== 'a'.repeat(64) || !['allow', 'deny'].includes(body.decision)) return json(intercepted, { detail: 'wrong action binding' }, 409);
      nativeDecision = body.decision as 'allow' | 'deny';
      return json(intercepted, { state: 'decided' });
    }

    if (path === '/api/session') return json(intercepted, { csrf: 'browser-acceptance' });
    if (path === '/api/health') return json(intercepted, { status: 'ok', startup_recovery_failures: [], durable_run_startup_reconciliation: { adopted_attempt_ids: [], orphaned_attempt_ids: [], processes_proven_dead: [] } });
    if (path === '/api/v1/runs' || path === '/api/v1/invocation-activity') return json(intercepted, path === '/api/v1/runs' ? [] : { items: [], project_cursors: {}, activity_cursors: {}, recent_has_more: {} });
    if (path === '/api/v1/routes') return json(intercepted, routes);
    if (path === '/api/v1/workflow-packs') return options.workflowUnavailable ? json(intercepted, { detail: 'planted workflow catalogue outage' }, 503) : json(intercepted, { packs: [], failures: [] });
    if (path === '/api/v1/context-modules') return json(intercepted, { contract_version: 'LatticeContextModuleCatalogue/1.0', modes: ['manual', 'project_default', 'workflow_required'], sources: [] });
    if (path === '/api/v1/projects' && request.method() === 'GET') return json(intercepted, project ? [project] : []);
    if (path === '/api/v1/projects' && request.method() === 'POST') {
      project = { project_id: 'project_browser_acceptance', title: body.title, purpose: body.purpose, workflow_pack: null, created_at: '20260824T100000' };
      return json(intercepted, project);
    }
    if (path === '/api/v1/invocation-batches/preview') {
      return json(intercepted, {
        contract_version: 'InvocationBatchPreviewV1', batch_id: body.batch_id, project_id: 'project_browser_acceptance', request_sha256: '3'.repeat(64), preview_sha256: '4'.repeat(64), confirmation_reasons: [],
        members: body.specifications.map((spec: Record<string, any>) => ({ spec_id: spec.spec_id, assembly_sha256: '5'.repeat(64), compiled_prompt_preview: spec.assembly.prompt, parent_deltas: [], preflight: { invocation_id: null, preflight_sha256: '6'.repeat(64), segments: [{ block_id: 'prompt_preview', plane: 'current_prompt', label: 'current prompt', disposition: 'selected', sha256: '7'.repeat(64), status: 'active', semantic_type: 'human-prompt' }], omitted: [], execution: spec.route, response_contract: { type: 'object' }, capabilities: spec.route, provider_boundary: { proof_limits: ['provider-owned system context remains opaque'] }, compilation: { byte_count: spec.assembly.prompt.length, compiled_prompt_sha256: '8'.repeat(64) } } })),
      });
    }
    if (path === '/api/v1/invocation-batches' && request.method() === 'POST') {
      const created = body.specifications.map((spec: Record<string, any>) => {
        sequence += 1;
        const provider = String(spec.route.provider);
        const invocation: InvocationRecord = {
          invocation_id: `invocation_browser_${sequence.toString().padStart(4, '0')}`,
          project_id: 'project_browser_acceptance', state: 'succeeded', actor_kind: 'direct', role_session_id: null,
          manifest_sha256: sequence.toString(16).padStart(64, '0'), output_block_id: `block_browser_${sequence.toString().padStart(4, '0')}`, terminal_kind: 'succeeded',
          created_at: `20260824T1000${sequence.toString().padStart(2, '0')}`, completed_at: `20260824T1000${sequence.toString().padStart(2, '0')}`,
          response: `${provider === 'anthropic' ? 'Claude' : 'Codex'} answer ${sequence}: exact selected material`, route: spec.route, concern_refs: spec.concern_refs ?? [],
        };
        invocations.push(invocation);
        blockOwner.set(invocation.output_block_id, invocation.invocation_id);
        for (const concern of invocation.concern_refs) {
          const episode = episodes.find((candidate) => candidate.concern_ref === concern);
          if (episode) episode.items.push({ object_type: 'invocation', object_id: invocation.invocation_id });
        }
        return invocation;
      });
      return json(intercepted, { batch_id: body.batch_id, project_id: 'project_browser_acceptance', receipt_sha256: '9'.repeat(64), invocation_ids: created.map((item) => item.invocation_id), members: created.map((item, index) => ({ spec_id: body.specifications[index].spec_id, invocation_id: item.invocation_id, state: 'succeeded', attempts: [] })), execution_attempts: [] });
    }
    if (path === '/api/v1/episodes' && request.method() === 'POST') {
      sequence += 1;
      const episode: EpisodeRecord = { slice_id: `slice_episode_${sequence}`, episode_id: `episode_${sequence}`, concern_ref: `lattice:episode_${sequence}`, parent_episode_id: body.parent_episode_id ?? null, name: body.name, items: [{ object_type: body.anchor_type, object_id: body.anchor_id }], coverage: { state: 'complete' } };
      episodes.push(episode);
      return json(intercepted, episode);
    }
    if (path === '/api/v1/context-blocks/excerpts' && request.method() === 'POST') {
      sequence += 1;
      const blockId = `block_excerpt_${sequence}`;
      blockOwner.set(blockId, blockOwner.get(body.source_block_id) ?? '');
      return json(intercepted, { block_id: blockId });
    }
    if ((path.match(/^\/api\/v1\/invocations\/[^/]+\/tags$/) || path.match(/^\/api\/v1\/context-blocks\/[^/]+\/tags$/)) && request.method() === 'POST') {
      const sourceId = decodeURIComponent(path.split('/')[4]);
      const sourceType = path.includes('/invocations/') ? 'invocation' : 'context-block';
      let topic = slateTopics.get(body.concern_ref);
      if (!topic) {
        topic = { slice_id: `slice_slate_${slateTopics.size + 1}`, external_ref: body.concern_ref, name: body.label, scope: 'global', items: [] };
        slateTopics.set(body.concern_ref, topic);
      }
      if (!topic.items.some((item) => item.object_type === sourceType && item.object_id === sourceId)) topic.items.push({ object_type: sourceType, object_id: sourceId });
      return json(intercepted, { edge_id: `edge_${sequence += 1}`, slice: { ...topic, coverage: { state: 'complete' } } });
    }
    const projectMatch = path.match(/^\/api\/v1\/projects\/([^/]+)$/);
    if (projectMatch && request.method() === 'GET') return json(intercepted, projectView());
    if (path.match(/^\/api\/v1\/projects\/[^/]+\/episodes$/)) return secondaryUnavailable ? json(intercepted, { detail: 'planted episode outage' }, 503) : json(intercepted, episodes);
    if (path.match(/^\/api\/v1\/projects\/[^/]+\/trailheads$/)) return json(intercepted, { items: invocations.slice().reverse().map(summary), has_older: false, next_before: null });
    const invocationMatch = path.match(/^\/api\/v1\/invocations\/([^/]+)$/);
    if (invocationMatch) {
      const item = invocations.find((candidate) => candidate.invocation_id === invocationMatch[1]);
      return item ? json(intercepted, invocationView(item)) : json(intercepted, { detail: 'not found' }, 404);
    }
    const neighborhoodMatch = path.match(/^\/api\/v1\/invocations\/([^/]+)\/neighborhood$/);
    if (neighborhoodMatch) return secondaryUnavailable ? json(intercepted, { detail: 'planted graph outage' }, 503) : json(intercepted, { nodes: invocations.map(summary), edges: [], coverage: { state: 'complete', requested_depth: 2, completed_depth: 2, node_limit: 200, edge_limit: 1000 } });
    const navigationMatch = path.match(/^\/api\/v1\/invocations\/([^/]+)\/navigation$/);
    if (navigationMatch) {
      const item = invocations.find((candidate) => candidate.invocation_id === navigationMatch[1]);
      if (secondaryUnavailable) return json(intercepted, { detail: 'planted navigation outage' }, 503);
      return item ? json(intercepted, navigation(item)) : json(intercepted, { detail: 'not found' }, 404);
    }
    const sliceMatch = path.match(/^\/api\/v1\/slices\/([^/]+)$/);
    if (sliceMatch) {
      const topic = [...slateTopics.values()].find((candidate) => candidate.slice_id === sliceMatch[1]);
      return topic ? json(intercepted, { ...topic, coverage: { state: 'complete' } }) : json(intercepted, { detail: 'not found' }, 404);
    }
    return json(intercepted, { detail: `unhandled browser fixture route: ${request.method()} ${path}` }, 501);
  });
  return { setSecondaryUnavailable(value: boolean) { secondaryUnavailable = value; }, setNativeDecisionUnavailable(value: boolean) { nativeDecisionUnavailable = value; } };
}

test('native approval browser: exact action, visible failure, denial, and honest delivery state', async ({ page }) => {
  const fixture = await installDeterministicLattice(page, { nativeApproval: true });
  await page.goto('/');
  await page.getByPlaceholder('New project').fill('Native approval fixture');
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  await page.locator('.composer textarea').fill('Exercise the planted permission surface.');
  await page.getByRole('button', { name: 'Send to anthropic' }).click();
  const surface = page.getByRole('region', { name: 'Native tool permissions' });
  await expect(surface).toContainText('Read · Needs your permission');
  await expect(surface).toContainText('/planted/private.txt');
  await expect(surface.locator('img')).toHaveCount(0);
  await surface.getByText('Exact arguments and binding').click();
  await expect(surface).toContainText('"offset": 17');
  await expect(surface).toContainText('"limit": 9');
  await surface.screenshot({ path: test.info().outputPath('native-approval-pending.png') });
  fixture.setNativeDecisionUnavailable(true);
  await surface.getByRole('button', { name: 'Allow this action' }).click();
  await expect(surface.getByRole('alert')).toContainText('planted decision service outage');
  await expect(surface.getByRole('button', { name: 'Deny this action' })).toBeEnabled();
  fixture.setNativeDecisionUnavailable(false);
  const sent = page.waitForRequest((request) => request.url().includes('/native-approvals/') && request.method() === 'POST');
  await surface.getByRole('button', { name: 'Deny this action' }).click();
  expect((await sent).postDataJSON()).toEqual({ request_sha256: 'a'.repeat(64), decision: 'deny' });
  await expect(surface).toContainText('Denied; released to the adapter. This record does not confirm tool execution.');
  await expect(surface.getByRole('button', { name: 'Allow this action' })).toHaveCount(0);
  await surface.screenshot({ path: test.info().outputPath('native-approval-denied.png') });
});

test('browser acceptance: freestyle one-edge lattice stays conversational and source-bound', async ({ page }) => {
  await installDeterministicLattice(page);
  await page.goto('/');

  await page.getByPlaceholder('New project').fill('Browser lattice');
  await page.getByPlaceholder('What is this for?').fill('Synthetic cockpit acceptance');
  await page.getByRole('button', { name: 'Create' }).click();
  await expect(page.getByText('Say what should happen next.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Claude' })).toBeVisible();
  await expect(page.getByText('Direct conversation')).toBeAttached();

  await page.locator('.composer textarea').fill('Begin directly without a role.');
  await page.getByRole('button', { name: 'Send to anthropic' }).click();
  await expect(page.locator('.response-card article')).toContainText('Claude answer 1');

  await page.locator('.response-card article').evaluate((article) => {
    const walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
    let node: Node | null = walker.nextNode();
    while (node && !node.textContent?.includes('exact selected material')) node = walker.nextNode();
    if (!node?.textContent) throw new Error('selection fixture text was not rendered');
    const start = node.textContent.indexOf('exact selected material');
    const range = document.createRange();
    range.setStart(node, start);
    range.setEnd(node, start + 'exact selected material'.length);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    window.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  });
  const exactActions = page.locator('details.context-actions.selected-text');
  await expect(exactActions.getByText(/Use “exact selected material”/)).toBeVisible();
  await exactActions.getByText(/Use “exact selected material”/).click();
  for (const label of ['Side quest', 'Ask Claude', 'Ask Codex', 'Ask both', 'Bring here', 'Compare']) await expect(exactActions.getByRole('button', { name: label, exact: true })).toBeVisible();
  await exactActions.getByRole('button', { name: 'Ask Codex', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Codex', exact: true })).toHaveClass(/active/);
  await expect(page.getByText('1 exact graft selected')).toBeVisible();

  await page.locator('.composer textarea').fill('Investigate this exact excerpt.');
  await page.getByRole('button', { name: 'Send to openai' }).click();
  await expect(page.locator('.response-card article')).toContainText('Codex answer');
  await expect(page.locator('.lineage-breadcrumb')).toContainText('Side quest');

  await page.getByRole('button', { name: 'both', exact: true }).click();
  await page.locator('.composer textarea').fill('Fan out this side quest.');
  await page.getByRole('button', { name: 'Send to both' }).click();
  await expect(page.locator('.response-card article')).toContainText('Codex answer 7');

  await page.getByRole('button', { name: /Return to Main trail/ }).click();
  await expect(page.getByText('1 parent')).toBeVisible();
  await page.getByRole('button', { name: 'Claude', exact: true }).click();
  await page.locator('.composer textarea').fill('Bring the useful result back.');
  await page.getByRole('button', { name: 'Send to anthropic' }).click();
  await expect(page.locator('.response-card article')).toContainText('Claude answer');

  await page.getByText('Tag into a live Slate topic').click();
  await page.getByLabel('Stable Slate identity').fill('browser-acceptance');
  await page.getByLabel('Visible topic label').fill('Browser acceptance topic');
  await page.getByRole('button', { name: 'Tag invocation' }).click();
  await expect(page.getByText(/Live slice updated/)).toBeVisible();
  await page.locator('.lineage-breadcrumb').getByRole('button', { name: 'Browser acceptance topic' }).click();
  await expect(page.locator('.slate-slice')).toContainText('complete');
  await expect(page.locator('.slate-slice')).toContainText('invocation');

  await page.getByText('all branches').click();
  await expect(page.locator('.trailhead-menu button').first()).toBeVisible();
  await page.locator('.trailhead-menu button').last().click();
  await expect(page.getByText('1 parent')).toBeVisible();

  await expect(page.locator('.workflow-control')).toHaveCount(0);
  await expect(page.locator('.role-list')).toHaveCount(0);
  await expect(page.locator('.error')).toHaveCount(0);
});

test('browser regression: secondary surface outages do not erase the conversation', async ({ page }) => {
  await installDeterministicLattice(page, { secondaryUnavailable: true, workflowUnavailable: true });
  await page.goto('/');

  await page.getByPlaceholder('New project').fill('Degraded but usable');
  await page.getByRole('button', { name: 'Create' }).click();
  await expect(page.getByText(/Workflow catalogue unavailable: planted workflow catalogue outage/)).toBeAttached();
  await expect(page.getByText(/Episodes unavailable: planted episode outage/)).toBeVisible();

  await page.locator('.composer textarea').fill('Keep the primary conversation alive.');
  await page.getByRole('button', { name: 'Send to anthropic' }).click();

  await expect(page.locator('.response-card article')).toContainText('Claude answer 1');
  await expect(page.getByText(/Concern navigation unavailable: planted navigation outage/)).toBeVisible();
  await expect(page.getByText('Local graph unavailable')).toBeVisible();
  await expect(page.locator('.composer textarea')).toBeVisible();
  await expect(page.locator('.error')).toHaveCount(0);
});

test('browser regression: the latest project choice wins overlapping loads', async ({ page }) => {
  const projects = [
    { project_id: 'project_a', title: 'Project Alpha', purpose: 'Fast project', workflow_pack: null, created_at: '20260824T100000' },
    { project_id: 'project_b', title: 'Project Beta', purpose: 'Slow project', workflow_pack: null, created_at: '20260824T100001' },
  ];
  const projectView = (projectId: string) => ({
    ...projects.find((item) => item.project_id === projectId),
    roles: [], sessions: [], context_defaults: [], workflow: null, invocations: [],
    invocation_page: { has_older: false, next_before: null }, trailheads: [], workflow_templates: [], workflow_templates_partial: false,
  });
  await page.route('**/api/**', async (intercepted) => {
    const request = intercepted.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/session') return json(intercepted, { csrf: 'browser-race' });
    if (path === '/api/health') return json(intercepted, { status: 'ok', startup_recovery_failures: [], durable_run_startup_reconciliation: { adopted_attempt_ids: [], orphaned_attempt_ids: [], processes_proven_dead: [] } });
    if (path === '/api/v1/projects') return json(intercepted, projects);
    if (path === '/api/v1/routes') return json(intercepted, []);
    if (path === '/api/v1/workflow-packs') return json(intercepted, { packs: [], failures: [] });
    if (path === '/api/v1/context-modules') return json(intercepted, { contract_version: 'LatticeContextModuleCatalogue/1.0', modes: ['manual', 'project_default', 'workflow_required'], sources: [] });
    if (path === '/api/v1/runs') return json(intercepted, []);
    if (path === '/api/v1/invocation-activity') return json(intercepted, { items: [], project_cursors: {}, activity_cursors: {}, recent_has_more: {} });
    const projectMatch = path.match(/^\/api\/v1\/projects\/(project_[ab])$/);
    if (projectMatch) {
      if (projectMatch[1] === 'project_b') await new Promise((resolve) => setTimeout(resolve, 250));
      return json(intercepted, projectView(projectMatch[1]));
    }
    if (path.match(/^\/api\/v1\/projects\/project_[ab]\/episodes$/) || path.match(/^\/api\/v1\/projects\/project_[ab]\/runs$/)) return json(intercepted, []);
    return json(intercepted, { detail: `unhandled race fixture route: ${request.method()} ${path}` }, 501);
  });

  await page.goto('/');
  await expect(page.locator('.project-heading h2')).toHaveText('Project Alpha');
  await page.getByRole('button', { name: /Project Beta/ }).click();
  await page.waitForTimeout(25);
  await page.getByRole('button', { name: /Project Alpha/ }).click();
  await page.waitForTimeout(350);

  await expect(page.locator('.project-heading h2')).toHaveText('Project Alpha');
  await expect(page.getByRole('button', { name: /Project Alpha/ })).toHaveClass(/active/);
  await expect(page.getByRole('button', { name: /Project Beta/ })).not.toHaveClass(/active/);
});


test('durable child: saved draft and reading position survive reload, then interruption continues', async ({page}) => {
  await installDeterministicLattice(page);
  await page.goto('/');
  await page.getByPlaceholder('New project').fill('Durable browser project');
  await page.getByRole('button',{name:'Create',exact:true}).click();
  await page.getByLabel('Child conversation title').fill('Relata pilot child');
  await page.getByLabel('Owning task reference').fill('task:catalog');
  await page.getByRole('button',{name:'Create with current route'}).click();
  await page.getByLabel('Next request',{exact:true}).fill('First operator request');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  await page.getByLabel('Next request',{exact:true}).fill('Unsubmitted durable draft');
  const region=page.getByRole('region',{name:'Saved conversation rounds'});
  await region.evaluate(element=>{element.scrollTop=320;element.dispatchEvent(new Event('scroll'));});
  await page.reload();
  await expect(page.getByRole('heading',{name:'Relata pilot child',exact:true})).toBeVisible();
  await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('Unsubmitted durable draft');
  await expect.poll(()=>region.evaluate(element=>element.scrollTop)).toBe(320);
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await expect(page.getByRole('button',{name:'Interrupt',exact:true})).toBeVisible();
  await page.getByLabel('Steer this conversation').fill('Preserve the architectural north star.');
  await page.getByRole('button',{name:'Steer next round'}).click();
  await expect(page.getByText('Queued steering: Preserve the architectural north star.')).toBeVisible();
  await page.getByRole('button',{name:'Interrupt',exact:true}).click();
  await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'Continue',exact:true}).click();
  await expect(page.getByText('Recovered using the saved conversation.')).toBeVisible();
  await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
});


test('durable child: explicit context selection survives reload and confirms the exact recovery', async ({page}) => {
  const admitted:Array<Record<string,unknown>>=[];
  await installDeterministicLattice(page,{limitedChildContext:true,onChildContinue:request=>admitted.push(request)});
  await page.goto('/');
  await page.getByPlaceholder('New project').fill('Context selection project');
  await page.getByRole('button',{name:'Create',exact:true}).click();
  await page.getByLabel('Child conversation title').fill('Selected history child');
  await page.getByRole('button',{name:'Create with current route'}).click();
  await page.getByLabel('Next request',{exact:true}).fill('First saved request');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await page.getByLabel('Next request',{exact:true}).fill('Second pending request');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await page.getByRole('button',{name:'Interrupt',exact:true}).click();
  await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeDisabled();
  await page.getByText('Recovery context and route',{exact:true}).click();
  await page.getByLabel('Include completed round 1',{exact:true}).uncheck();
  await page.getByLabel('Reason for context selection',{exact:true}).fill('Use the pending request while keeping earlier saved work.');
  await page.reload();
  await page.getByText('Recovery context and route',{exact:true}).click();
  await expect(page.getByLabel('Include completed round 1',{exact:true})).not.toBeChecked();
  await expect(page.getByLabel('Reason for context selection',{exact:true})).toHaveValue('Use the pending request while keeping earlier saved work.');
  await page.getByRole('button',{name:'Review recovery changes'}).click();
  await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeDisabled();
  await page.getByLabel('Use this explicit context selection; retain omitted material in saved history.',{exact:true}).check();
  await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'Continue',exact:true}).click();
  await expect(page.getByText('Recovered using the saved conversation.')).toBeVisible();
  await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  expect(admitted).toHaveLength(1);
  expect(admitted[0].context_selection).toMatchObject({omitted_round_ids:['round_1'],reason:'Use the pending request while keeping earlier saved work.'});
  expect(admitted[0].expected_preview_sha256).toBe('b'.repeat(64));
  expect(admitted[0].confirmed_reason_sha256s).toEqual(['c'.repeat(64)]);
});

test('durable child: fresh operator discovers children beyond the first page and filters by owner', async ({page}) => {
  await installDeterministicLattice(page);
  await page.goto('/');
  await page.getByPlaceholder('New project').fill('Paged child project');
  await page.getByRole('button',{name:'Create',exact:true}).click();
  await page.getByLabel('Child conversation title').fill('Needle child');
  await page.getByLabel('Owning task reference').fill('task:needle');
  await page.getByRole('button',{name:'Create with current route'}).click();
  const queries:Array<Record<string,string>>=[];
  const many=Array.from({length:500},(_,i)=>({conversation_id:`conversation_${String(i).padStart(4,'0')}`,title:`Other child ${i}`,owning_task_ref:'task:other',revision:1,parent_conversation_id:null}));
  const needle={conversation_id:'conversation_browser',title:'Needle child',owning_task_ref:'task:needle',revision:1,parent_conversation_id:null};
  await page.route('**/api/v1/conversations?**', async route=>{
    const query=new URL(route.request().url()).searchParams;
    queries.push(Object.fromEntries(query));
    const owner=query.get('owning_task_ref');
    return json(route,owner==='task:needle' || query.get('after')==='conversation_0499' ? [needle] : many);
  });
  await page.getByRole('button',{name:'Invocation workspace',exact:true}).click();
  await page.reload();
  const navigation=page.getByRole('navigation',{name:'Saved child conversations'});
  await expect(navigation.getByRole('button',{name:'Needle child'})).toHaveCount(0);
  await page.getByRole('button',{name:'More conversations'}).click();
  await navigation.getByRole('button',{name:'Needle child'}).click();
  await expect(page.getByRole('heading',{name:'Needle child',exact:true})).toBeVisible();
  expect(queries.some(q=>q.after==='conversation_0499')).toBe(true);
  await page.getByLabel('Filter children by owning task').fill('task:needle');
  await page.getByRole('button',{name:'Filter children'}).click();
  await expect(navigation.getByRole('button',{name:'Needle child'})).toHaveCount(1);
  await expect(page.getByRole('button',{name:'Previous conversations'})).toHaveCount(0);
  expect(queries.at(-1)).toMatchObject({owning_task_ref:'task:needle',after:''});
});

test('durable child: lost acknowledgement survives reload without duplicating the request', async ({page}) => {
  await installDeterministicLattice(page,{lostChildAcknowledgement:true});
  await page.goto('/');
  await page.getByPlaceholder('New project').fill('Lost acknowledgement project');
  await page.getByRole('button',{name:'Create',exact:true}).click();
  await page.getByLabel('Child conversation title').fill('Recover the receipt');
  await page.getByRole('button',{name:'Create with current route'}).click();
  await page.getByLabel('Next request',{exact:true}).fill('Exactly one request');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await expect(page.locator('.durable-conversations [role="alert"]')).toBeVisible();
  await page.reload();
  await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('Exactly one request');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('');
  await expect(page.locator('.durable-conversations .rounds article')).toHaveCount(1);
});

test('durable child: uncertain action requires an explicit disposition before continuing', async ({page}) => {
  await installDeterministicLattice(page,{uncertainChildEffect:true});
  await page.goto('/');
  await page.getByPlaceholder('New project').fill('Uncertain effect project');
  await page.getByRole('button',{name:'Create',exact:true}).click();
  await page.getByLabel('Child conversation title').fill('Inspect effects');
  await page.getByRole('button',{name:'Create with current route'}).click();
  await page.getByLabel('Next request',{exact:true}).fill('First request');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await page.getByLabel('Next request',{exact:true}).fill('Second request');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await page.getByRole('button',{name:'Interrupt',exact:true}).click();
  await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeDisabled();
  await page.getByRole('combobox',{name:'Outcome',exact:true}).selectOption('do_not_repeat');
  await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeDisabled();
  await page.getByLabel('Evidence or instruction').fill('The external result cannot be established. Do not repeat the write.');
  await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'Continue',exact:true}).click();
  await expect(page.getByText('Recovered using the saved conversation.')).toBeVisible();
});

test('durable child: lost creation acknowledgement retries the same request after reload', async ({page}) => {
  const created:string[]=[];
  const requests:Record<string,unknown>[]=[];
  await installDeterministicLattice(page,{lostChildCreationAcknowledgement:true,onChildCreation:key=>created.push(key),onChildCreationRequest:request=>requests.push(request)});
  await page.goto('/');
  await page.getByPlaceholder('New project').fill('Creation recovery project');
  await page.getByRole('button',{name:'Create',exact:true}).click();
  await page.getByLabel('Child conversation title').fill('Once-created child');
  await page.getByRole('button',{name:'Create with current route'}).click();
  await expect(page.locator('.durable-conversations [role="alert"]')).toBeVisible();
  await page.reload();
  await expect(page.getByLabel('Child conversation title')).toHaveValue('Once-created child');
  await page.getByRole('button',{name:'Create with current route'}).click();
  await expect(page.getByRole('heading',{name:'Once-created child',exact:true})).toBeVisible();
  expect(created).toHaveLength(1);
  expect(requests).toHaveLength(2);
  expect(requests[1]).toEqual(requests[0]);
});

test('durable child: late observations cannot enter a different selected child', async ({page}) => {
  await installDeterministicLattice(page);
  let releaseA!:()=>void;
  const heldA=new Promise<void>(resolve=>{releaseA=resolve;});
  let startedA=false;
  const child=(id:string)=>({conversation_id:`conversation_${id}`,title:`Child ${id.toUpperCase()}`,owning_task_ref:'task:race',revision:1,parent_conversation_id:null,route:{model:'fixture-model'},rounds:[{round_id:`round_${id}`,ordinal:1,state:'active',request:{prompt:`Request ${id}`},executions:[]}],pending:{round_id:`round_${id}`,ordinal:1,state:'active',attempt_ids:[`attempt_${id}`]},has_more:false,event_cursor:1,steering:[],checkpoint:null});
  await page.route('**/api/v1/conversations**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if (path==='/api/v1/conversations') return json(route,[child('a'),child('b')]);
    const id=path.includes('conversation_a')?'a':'b';
    if (path.endsWith('/observations')) {
      if (id==='a') { startedA=true;await heldA; }
      return json(route,{items:[{seq:id==='a'?1:2,subject_id:`attempt_${id}`,event_type:'attempt.chunk',payload:{preview:id==='a'?'Stale child A activity.':'Only current child B activity.'}}],cursor:2,has_more:false});
    }
    return json(route,child(id));
  });
  try {
    await page.goto('/');
    await page.getByPlaceholder('New project').fill('Child activity race');
    await page.getByRole('button',{name:'Create',exact:true}).click();
    await page.getByRole('button',{name:/Child A/}).click();
    await expect.poll(()=>startedA).toBe(true);
    await page.getByRole('button',{name:/Child B/}).click();
    const partial=page.locator('.durable-conversations details pre');
    await expect(partial).toContainText('Only current child B activity.');
    const response=page.waitForResponse(value=>value.url().includes('/conversation_a/observations'));
    releaseA();await (await response).finished();
    await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
    await expect(page.getByRole('heading',{name:'Child B',exact:true})).toBeVisible();
    await expect(partial).not.toContainText('Stale child A activity.');
  } finally { releaseA(); }
});

test('durable child: context follows its execution, rejects stale results and surfaces inspection failure', async ({page}) => {
  await installDeterministicLattice(page);
  let release!:()=>void;
  const held=new Promise<void>(resolve=>{release=resolve;});
  let started=false;
  let failB=false;
  const child=(id:string)=>({conversation_id:`conversation_context_${id}`,title:`Context child ${id}`,owning_task_ref:'task:context',revision:1,parent_conversation_id:null,route:{model:`model-${id}`},rounds:[{round_id:`round_${id}`,ordinal:1,state:'completed',request:{prompt:`Request ${id}`},response:`Answer ${id}`,executions:[{invocation_id:`invocation_context_${id}`,attempts:[]}]}],pending:null,has_more:false,event_cursor:1,steering:[],checkpoint:null});
  await page.route('**/api/v1/conversations**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if (path==='/api/v1/conversations') return json(route,[child('a'),child('b'),{...child('empty'),rounds:[]}]);
    return json(route,path.endsWith('conversation_context_empty')?{...child('empty'),rounds:[]}:child(path.endsWith('_a')?'a':'b'));
  });
  await page.route('**/api/v1/invocations/invocation_context_*',async route=>{
    const id=new URL(route.request().url()).pathname.endsWith('_a')?'a':'b';
    if (id==='a') {started=true;await held;}
    if (id==='b' && failB) return json(route,{detail:'planted inspection outage'},503);
    return json(route,{invocation_id:`invocation_context_${id}`,compiled_prompt:`Exact context ${id}`,manifest_verification:{valid:true},manifest:{segments:[],omitted:[],execution:{model:`model-${id}`},response_contract:{},capabilities:{},provider_boundary:{proof_limits:[]}},attempts:[],artifacts:[]});
  });
  try {
    await page.goto('/');
    await page.getByPlaceholder('New project').fill('Context binding');
    await page.getByRole('button',{name:'Create',exact:true}).click();
    await page.getByRole('button',{name:/Context child a/}).click();
    await expect.poll(()=>started).toBe(true);
    await page.getByRole('button',{name:/Context child b/}).click();
    const inspector=page.getByRole('complementary',{name:'Conversation execution context'});
    await inspector.getByText('Execution route',{exact:true}).click();
    await expect(inspector).toContainText('model-b');
    await expect(page.locator('.route-status')).toHaveText('Conversation · model-b');
    const response=page.waitForResponse(value=>value.url().endsWith('/invocation_context_a'));
    release();await (await response).finished();
    await expect(inspector).not.toContainText('model-a');
    await page.getByRole('button',{name:/Context child empty/}).click();
    await expect(inspector).toContainText('no execution to inspect');
    await expect(inspector).not.toContainText('model-b');
    failB=true;
    await page.getByRole('button',{name:/Context child b/}).click();
    await expect(inspector.getByRole('alert')).toContainText('planted inspection outage');
    await expect(inspector).not.toContainText('Manifest reconstructed');
    failB=false;
    await inspector.getByRole('button',{name:'Retry inspection'}).click();
    await expect(inspector).toContainText('Manifest reconstructed');
    await page.reload();
    await expect(inspector).toContainText('invocation_context_b');
  } finally {release();}
});
