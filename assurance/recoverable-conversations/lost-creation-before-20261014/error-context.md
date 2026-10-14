# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: conversation.spec.ts >> durable child: lost creation acknowledgement retries the same request after reload
- Location: e2e/conversation.spec.ts:474:1

# Error details

```
Error: expect(locator).toHaveValue(expected) failed

Locator:  getByLabel('Child conversation title')
Expected: "Once-created child"
Received: ""
Timeout:  5000ms

Call log:
  - Expect "toHaveValue" with timeout 5000ms
  - waiting for getByLabel('Child conversation title')
    13 × locator resolved to <input required="" class="svelte-1n6hf1b" placeholder="New child conversation" aria-label="Child conversation title"/>
       - unexpected value ""

```

```yaml
- textbox "Child conversation title":
  - /placeholder: New child conversation
```

# Test source

```ts
  384 |       if (projectMatch[1] === 'project_b') await new Promise((resolve) => setTimeout(resolve, 250));
  385 |       return json(intercepted, projectView(projectMatch[1]));
  386 |     }
  387 |     if (path.match(/^\/api\/v1\/projects\/project_[ab]\/episodes$/) || path.match(/^\/api\/v1\/projects\/project_[ab]\/runs$/)) return json(intercepted, []);
  388 |     return json(intercepted, { detail: `unhandled race fixture route: ${request.method()} ${path}` }, 501);
  389 |   });
  390 | 
  391 |   await page.goto('/');
  392 |   await expect(page.locator('.project-heading h2')).toHaveText('Project Alpha');
  393 |   await page.getByRole('button', { name: /Project Beta/ }).click();
  394 |   await page.waitForTimeout(25);
  395 |   await page.getByRole('button', { name: /Project Alpha/ }).click();
  396 |   await page.waitForTimeout(350);
  397 | 
  398 |   await expect(page.locator('.project-heading h2')).toHaveText('Project Alpha');
  399 |   await expect(page.getByRole('button', { name: /Project Alpha/ })).toHaveClass(/active/);
  400 |   await expect(page.getByRole('button', { name: /Project Beta/ })).not.toHaveClass(/active/);
  401 | });
  402 | 
  403 | 
  404 | test('durable child: saved draft and reading position survive reload, then interruption continues', async ({page}) => {
  405 |   await installDeterministicLattice(page);
  406 |   await page.goto('/');
  407 |   await page.getByPlaceholder('New project').fill('Durable browser project');
  408 |   await page.getByRole('button',{name:'Create',exact:true}).click();
  409 |   await page.getByLabel('Child conversation title').fill('Relata pilot child');
  410 |   await page.getByLabel('Owning task reference').fill('task:catalog');
  411 |   await page.getByRole('button',{name:'Create with current route'}).click();
  412 |   await page.getByLabel('Next request',{exact:true}).fill('First operator request');
  413 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  414 |   await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  415 |   await page.getByLabel('Next request',{exact:true}).fill('Unsubmitted durable draft');
  416 |   const region=page.getByRole('region',{name:'Saved conversation rounds'});
  417 |   await region.evaluate(element=>{element.scrollTop=320;element.dispatchEvent(new Event('scroll'));});
  418 |   await page.reload();
  419 |   await expect(page.getByRole('heading',{name:'Relata pilot child',exact:true})).toBeVisible();
  420 |   await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('Unsubmitted durable draft');
  421 |   await expect.poll(()=>region.evaluate(element=>element.scrollTop)).toBe(320);
  422 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  423 |   await expect(page.getByRole('button',{name:'Interrupt',exact:true})).toBeVisible();
  424 |   await page.getByLabel('Steer this conversation').fill('Preserve the architectural north star.');
  425 |   await page.getByRole('button',{name:'Steer next round'}).click();
  426 |   await expect(page.getByText('Queued steering: Preserve the architectural north star.')).toBeVisible();
  427 |   await page.getByRole('button',{name:'Interrupt',exact:true}).click();
  428 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeEnabled();
  429 |   await page.getByRole('button',{name:'Continue',exact:true}).click();
  430 |   await expect(page.getByText('Recovered using the saved conversation.')).toBeVisible();
  431 |   await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  432 | });
  433 | 
  434 | 
  435 | test('durable child: lost acknowledgement survives reload without duplicating the request', async ({page}) => {
  436 |   await installDeterministicLattice(page,{lostChildAcknowledgement:true});
  437 |   await page.goto('/');
  438 |   await page.getByPlaceholder('New project').fill('Lost acknowledgement project');
  439 |   await page.getByRole('button',{name:'Create',exact:true}).click();
  440 |   await page.getByLabel('Child conversation title').fill('Recover the receipt');
  441 |   await page.getByRole('button',{name:'Create with current route'}).click();
  442 |   await page.getByLabel('Next request',{exact:true}).fill('Exactly one request');
  443 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  444 |   await expect(page.locator('.durable-conversations [role="alert"]')).toBeVisible();
  445 |   await page.reload();
  446 |   await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  447 |   await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('Exactly one request');
  448 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  449 |   await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('');
  450 |   await expect(page.locator('.durable-conversations .rounds article')).toHaveCount(1);
  451 | });
  452 | 
  453 | test('durable child: uncertain action requires an explicit disposition before continuing', async ({page}) => {
  454 |   await installDeterministicLattice(page,{uncertainChildEffect:true});
  455 |   await page.goto('/');
  456 |   await page.getByPlaceholder('New project').fill('Uncertain effect project');
  457 |   await page.getByRole('button',{name:'Create',exact:true}).click();
  458 |   await page.getByLabel('Child conversation title').fill('Inspect effects');
  459 |   await page.getByRole('button',{name:'Create with current route'}).click();
  460 |   await page.getByLabel('Next request',{exact:true}).fill('First request');
  461 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  462 |   await page.getByLabel('Next request',{exact:true}).fill('Second request');
  463 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  464 |   await page.getByRole('button',{name:'Interrupt',exact:true}).click();
  465 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeDisabled();
  466 |   await page.getByRole('combobox',{name:'Outcome',exact:true}).selectOption('do_not_repeat');
  467 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeDisabled();
  468 |   await page.getByLabel('Evidence or instruction').fill('The external result cannot be established. Do not repeat the write.');
  469 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeEnabled();
  470 |   await page.getByRole('button',{name:'Continue',exact:true}).click();
  471 |   await expect(page.getByText('Recovered using the saved conversation.')).toBeVisible();
  472 | });
  473 | 
  474 | test('durable child: lost creation acknowledgement retries the same request after reload', async ({page}) => {
  475 |   const created:string[]=[];
  476 |   await installDeterministicLattice(page,{lostChildCreationAcknowledgement:true,onChildCreation:key=>created.push(key)});
  477 |   await page.goto('/');
  478 |   await page.getByPlaceholder('New project').fill('Creation recovery project');
  479 |   await page.getByRole('button',{name:'Create',exact:true}).click();
  480 |   await page.getByLabel('Child conversation title').fill('Once-created child');
  481 |   await page.getByRole('button',{name:'Create with current route'}).click();
  482 |   await expect(page.locator('.durable-conversations [role="alert"]')).toBeVisible();
  483 |   await page.reload();
> 484 |   await expect(page.getByLabel('Child conversation title')).toHaveValue('Once-created child');
      |                                                             ^ Error: expect(locator).toHaveValue(expected) failed
  485 |   await page.getByRole('button',{name:'Create with current route'}).click();
  486 |   await expect(page.getByRole('heading',{name:'Once-created child',exact:true})).toBeVisible();
  487 |   expect(created).toHaveLength(1);
  488 | });
  489 | 
```