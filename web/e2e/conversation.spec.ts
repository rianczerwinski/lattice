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

async function installDeterministicLattice(page: Page, options: { secondaryUnavailable?: boolean; workflowUnavailable?: boolean } = {}) {
  let project: Record<string, unknown> | null = null;
  const invocations: InvocationRecord[] = [];
  const episodes: EpisodeRecord[] = [];
  const slateTopics = new Map<string, { slice_id: string; external_ref: string; name: string; scope: 'global'; items: Array<{ object_id: string; object_type: string }> }>();
  const blockOwner = new Map<string, string>();
  let sequence = 0;
  let secondaryUnavailable = options.secondaryUnavailable ?? false;

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
    attempts: [{ state: 'succeeded', runtime_receipt: { native_session: null } }],
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

    if (path === '/api/session') return json(intercepted, { csrf: 'browser-acceptance' });
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
  return { setSecondaryUnavailable(value: boolean) { secondaryUnavailable = value; } };
}

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
  await expect(page.locator('.response-card article')).toContainText('Codex answer');

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
