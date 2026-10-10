# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: conversation.spec.ts >> durable child: uncertain action requires an explicit disposition before continuing
- Location: e2e/conversation.spec.ts:444:1

# Error details

```
Error: expect(locator).toBeEnabled() failed

Locator:  getByRole('button', { name: 'Continue', exact: true })
Expected: enabled
Received: disabled
Timeout:  5000ms

Call log:
  - Expect "toBeEnabled" with timeout 5000ms
  - waiting for getByRole('button', { name: 'Continue', exact: true })
    14 × locator resolved to <button disabled>Continue</button>
       - unexpected value "disabled"

```

```yaml
- button "Continue" [disabled]
```

# Test source

```ts
  360 |     invocation_page: { has_older: false, next_before: null }, trailheads: [], workflow_templates: [], workflow_templates_partial: false,
  361 |   });
  362 |   await page.route('**/api/**', async (intercepted) => {
  363 |     const request = intercepted.request();
  364 |     const path = new URL(request.url()).pathname;
  365 |     if (path === '/api/session') return json(intercepted, { csrf: 'browser-race' });
  366 |     if (path === '/api/health') return json(intercepted, { status: 'ok', startup_recovery_failures: [], durable_run_startup_reconciliation: { adopted_attempt_ids: [], orphaned_attempt_ids: [], processes_proven_dead: [] } });
  367 |     if (path === '/api/v1/projects') return json(intercepted, projects);
  368 |     if (path === '/api/v1/routes') return json(intercepted, []);
  369 |     if (path === '/api/v1/workflow-packs') return json(intercepted, { packs: [], failures: [] });
  370 |     if (path === '/api/v1/context-modules') return json(intercepted, { contract_version: 'LatticeContextModuleCatalogue/1.0', modes: ['manual', 'project_default', 'workflow_required'], sources: [] });
  371 |     if (path === '/api/v1/runs') return json(intercepted, []);
  372 |     if (path === '/api/v1/invocation-activity') return json(intercepted, { items: [], project_cursors: {}, activity_cursors: {}, recent_has_more: {} });
  373 |     const projectMatch = path.match(/^\/api\/v1\/projects\/(project_[ab])$/);
  374 |     if (projectMatch) {
  375 |       if (projectMatch[1] === 'project_b') await new Promise((resolve) => setTimeout(resolve, 250));
  376 |       return json(intercepted, projectView(projectMatch[1]));
  377 |     }
  378 |     if (path.match(/^\/api\/v1\/projects\/project_[ab]\/episodes$/) || path.match(/^\/api\/v1\/projects\/project_[ab]\/runs$/)) return json(intercepted, []);
  379 |     return json(intercepted, { detail: `unhandled race fixture route: ${request.method()} ${path}` }, 501);
  380 |   });
  381 | 
  382 |   await page.goto('/');
  383 |   await expect(page.locator('.project-heading h2')).toHaveText('Project Alpha');
  384 |   await page.getByRole('button', { name: /Project Beta/ }).click();
  385 |   await page.waitForTimeout(25);
  386 |   await page.getByRole('button', { name: /Project Alpha/ }).click();
  387 |   await page.waitForTimeout(350);
  388 | 
  389 |   await expect(page.locator('.project-heading h2')).toHaveText('Project Alpha');
  390 |   await expect(page.getByRole('button', { name: /Project Alpha/ })).toHaveClass(/active/);
  391 |   await expect(page.getByRole('button', { name: /Project Beta/ })).not.toHaveClass(/active/);
  392 | });
  393 | 
  394 | 
  395 | test('durable child: saved draft and reading position survive reload, then interruption continues', async ({page}) => {
  396 |   await installDeterministicLattice(page);
  397 |   await page.goto('/');
  398 |   await page.getByPlaceholder('New project').fill('Durable browser project');
  399 |   await page.getByRole('button',{name:'Create',exact:true}).click();
  400 |   await page.getByLabel('Child conversation title').fill('Relata pilot child');
  401 |   await page.getByLabel('Owning task reference').fill('task:catalog');
  402 |   await page.getByRole('button',{name:'Create with current route'}).click();
  403 |   await page.getByLabel('Next request',{exact:true}).fill('First operator request');
  404 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  405 |   await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  406 |   await page.getByLabel('Next request',{exact:true}).fill('Unsubmitted durable draft');
  407 |   const region=page.getByRole('region',{name:'Saved conversation rounds'});
  408 |   await region.evaluate(element=>{element.scrollTop=320;element.dispatchEvent(new Event('scroll'));});
  409 |   await page.reload();
  410 |   await expect(page.getByRole('heading',{name:'Relata pilot child',exact:true})).toBeVisible();
  411 |   await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('Unsubmitted durable draft');
  412 |   await expect.poll(()=>region.evaluate(element=>element.scrollTop)).toBe(320);
  413 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  414 |   await expect(page.getByRole('button',{name:'Interrupt',exact:true})).toBeVisible();
  415 |   await page.getByLabel('Steer this conversation').fill('Preserve the architectural north star.');
  416 |   await page.getByRole('button',{name:'Steer next round'}).click();
  417 |   await expect(page.getByText('Queued steering: Preserve the architectural north star.')).toBeVisible();
  418 |   await page.getByRole('button',{name:'Interrupt',exact:true}).click();
  419 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeEnabled();
  420 |   await page.getByRole('button',{name:'Continue',exact:true}).click();
  421 |   await expect(page.getByText('Recovered using the saved conversation.')).toBeVisible();
  422 |   await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  423 | });
  424 | 
  425 | 
  426 | test('durable child: lost acknowledgement survives reload without duplicating the request', async ({page}) => {
  427 |   await installDeterministicLattice(page,{lostChildAcknowledgement:true});
  428 |   await page.goto('/');
  429 |   await page.getByPlaceholder('New project').fill('Lost acknowledgement project');
  430 |   await page.getByRole('button',{name:'Create',exact:true}).click();
  431 |   await page.getByLabel('Child conversation title').fill('Recover the receipt');
  432 |   await page.getByRole('button',{name:'Create with current route'}).click();
  433 |   await page.getByLabel('Next request',{exact:true}).fill('Exactly one request');
  434 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  435 |   await expect(page.locator('.durable-conversations [role="alert"]')).toBeVisible();
  436 |   await page.reload();
  437 |   await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  438 |   await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('Exactly one request');
  439 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  440 |   await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('');
  441 |   await expect(page.locator('.durable-conversations .rounds article')).toHaveCount(1);
  442 | });
  443 | 
  444 | test('durable child: uncertain action requires an explicit disposition before continuing', async ({page}) => {
  445 |   await installDeterministicLattice(page,{uncertainChildEffect:true});
  446 |   await page.goto('/');
  447 |   await page.getByPlaceholder('New project').fill('Uncertain effect project');
  448 |   await page.getByRole('button',{name:'Create',exact:true}).click();
  449 |   await page.getByLabel('Child conversation title').fill('Inspect effects');
  450 |   await page.getByRole('button',{name:'Create with current route'}).click();
  451 |   await page.getByLabel('Next request',{exact:true}).fill('First request');
  452 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  453 |   await page.getByLabel('Next request',{exact:true}).fill('Second request');
  454 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  455 |   await page.getByRole('button',{name:'Interrupt',exact:true}).click();
  456 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeDisabled();
  457 |   await page.getByRole('combobox',{name:'Outcome',exact:true}).selectOption('do_not_repeat');
  458 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeDisabled();
  459 |   await page.getByLabel('Evidence or instruction').fill('The external result cannot be established. Do not repeat the write.');
> 460 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeEnabled();
      |                                                                       ^ Error: expect(locator).toBeEnabled() failed
  461 |   await page.getByRole('button',{name:'Continue',exact:true}).click();
  462 |   await expect(page.getByText('Recovered using the saved conversation.')).toBeVisible();
  463 | });
  464 | 
```