# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: conversation.spec.ts >> durable child: late observations cannot enter a different selected child
- Location: e2e/conversation.spec.ts:494:1

# Error details

```
Error: expect(locator).not.toContainText(expected) failed

Locator: locator('.durable-conversations details pre')
Expected substring: not "Stale child A activity."
Received string: "Stale child A activity.Only current child B activity."
Timeout: 5000ms

Call log:
  - Expect "not toContainText" with timeout 5000ms
  - waiting for locator('.durable-conversations details pre')
    14 × locator resolved to <pre class="svelte-1n6hf1b">Stale child A activity.Only current child B activ…</pre>
       - unexpected value "Stale child A activity.Only current child B activity."

```

```yaml
- text: Stale child A activity.Only current child B activity.
```

# Test source

```ts
  423 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  424 |   await expect(page.getByRole('button',{name:'Interrupt',exact:true})).toBeVisible();
  425 |   await page.getByLabel('Steer this conversation').fill('Preserve the architectural north star.');
  426 |   await page.getByRole('button',{name:'Steer next round'}).click();
  427 |   await expect(page.getByText('Queued steering: Preserve the architectural north star.')).toBeVisible();
  428 |   await page.getByRole('button',{name:'Interrupt',exact:true}).click();
  429 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeEnabled();
  430 |   await page.getByRole('button',{name:'Continue',exact:true}).click();
  431 |   await expect(page.getByText('Recovered using the saved conversation.')).toBeVisible();
  432 |   await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  433 | });
  434 | 
  435 | 
  436 | test('durable child: lost acknowledgement survives reload without duplicating the request', async ({page}) => {
  437 |   await installDeterministicLattice(page,{lostChildAcknowledgement:true});
  438 |   await page.goto('/');
  439 |   await page.getByPlaceholder('New project').fill('Lost acknowledgement project');
  440 |   await page.getByRole('button',{name:'Create',exact:true}).click();
  441 |   await page.getByLabel('Child conversation title').fill('Recover the receipt');
  442 |   await page.getByRole('button',{name:'Create with current route'}).click();
  443 |   await page.getByLabel('Next request',{exact:true}).fill('Exactly one request');
  444 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  445 |   await expect(page.locator('.durable-conversations [role="alert"]')).toBeVisible();
  446 |   await page.reload();
  447 |   await expect(page.getByText('First retained answer.',{exact:false})).toBeVisible();
  448 |   await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('Exactly one request');
  449 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  450 |   await expect(page.getByLabel('Next request',{exact:true})).toHaveValue('');
  451 |   await expect(page.locator('.durable-conversations .rounds article')).toHaveCount(1);
  452 | });
  453 | 
  454 | test('durable child: uncertain action requires an explicit disposition before continuing', async ({page}) => {
  455 |   await installDeterministicLattice(page,{uncertainChildEffect:true});
  456 |   await page.goto('/');
  457 |   await page.getByPlaceholder('New project').fill('Uncertain effect project');
  458 |   await page.getByRole('button',{name:'Create',exact:true}).click();
  459 |   await page.getByLabel('Child conversation title').fill('Inspect effects');
  460 |   await page.getByRole('button',{name:'Create with current route'}).click();
  461 |   await page.getByLabel('Next request',{exact:true}).fill('First request');
  462 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  463 |   await page.getByLabel('Next request',{exact:true}).fill('Second request');
  464 |   await page.getByRole('button',{name:'Send',exact:true}).click();
  465 |   await page.getByRole('button',{name:'Interrupt',exact:true}).click();
  466 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeDisabled();
  467 |   await page.getByRole('combobox',{name:'Outcome',exact:true}).selectOption('do_not_repeat');
  468 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeDisabled();
  469 |   await page.getByLabel('Evidence or instruction').fill('The external result cannot be established. Do not repeat the write.');
  470 |   await expect(page.getByRole('button',{name:'Continue',exact:true})).toBeEnabled();
  471 |   await page.getByRole('button',{name:'Continue',exact:true}).click();
  472 |   await expect(page.getByText('Recovered using the saved conversation.')).toBeVisible();
  473 | });
  474 | 
  475 | test('durable child: lost creation acknowledgement retries the same request after reload', async ({page}) => {
  476 |   const created:string[]=[];
  477 |   const requests:Record<string,unknown>[]=[];
  478 |   await installDeterministicLattice(page,{lostChildCreationAcknowledgement:true,onChildCreation:key=>created.push(key),onChildCreationRequest:request=>requests.push(request)});
  479 |   await page.goto('/');
  480 |   await page.getByPlaceholder('New project').fill('Creation recovery project');
  481 |   await page.getByRole('button',{name:'Create',exact:true}).click();
  482 |   await page.getByLabel('Child conversation title').fill('Once-created child');
  483 |   await page.getByRole('button',{name:'Create with current route'}).click();
  484 |   await expect(page.locator('.durable-conversations [role="alert"]')).toBeVisible();
  485 |   await page.reload();
  486 |   await expect(page.getByLabel('Child conversation title')).toHaveValue('Once-created child');
  487 |   await page.getByRole('button',{name:'Create with current route'}).click();
  488 |   await expect(page.getByRole('heading',{name:'Once-created child',exact:true})).toBeVisible();
  489 |   expect(created).toHaveLength(1);
  490 |   expect(requests).toHaveLength(2);
  491 |   expect(requests[1]).toEqual(requests[0]);
  492 | });
  493 | 
  494 | test('durable child: late observations cannot enter a different selected child', async ({page}) => {
  495 |   await installDeterministicLattice(page);
  496 |   let releaseA!:()=>void;
  497 |   const heldA=new Promise<void>(resolve=>{releaseA=resolve;});
  498 |   let startedA=false;
  499 |   const child=(id:string)=>({conversation_id:`conversation_${id}`,title:`Child ${id.toUpperCase()}`,owning_task_ref:'task:race',revision:1,parent_conversation_id:null,route:{model:'fixture-model'},rounds:[{round_id:`round_${id}`,ordinal:1,state:'active',request:{prompt:`Request ${id}`},executions:[]}],pending:{round_id:`round_${id}`,ordinal:1,state:'active',attempt_ids:[`attempt_${id}`]},has_more:false,event_cursor:1,steering:[],checkpoint:null});
  500 |   await page.route('**/api/v1/conversations**',async route=>{
  501 |     const path=new URL(route.request().url()).pathname;
  502 |     if (path==='/api/v1/conversations') return json(route,[child('a'),child('b')]);
  503 |     const id=path.includes('conversation_a')?'a':'b';
  504 |     if (path.endsWith('/observations')) {
  505 |       if (id==='a') { startedA=true;await heldA; }
  506 |       return json(route,{items:[{seq:id==='a'?1:2,subject_id:`attempt_${id}`,event_type:'attempt.chunk',payload:{preview:id==='a'?'Stale child A activity.':'Only current child B activity.'}}],cursor:2,has_more:false});
  507 |     }
  508 |     return json(route,child(id));
  509 |   });
  510 |   try {
  511 |     await page.goto('/');
  512 |     await page.getByPlaceholder('New project').fill('Child activity race');
  513 |     await page.getByRole('button',{name:'Create',exact:true}).click();
  514 |     await page.getByRole('button',{name:/Child A/}).click();
  515 |     await expect.poll(()=>startedA).toBe(true);
  516 |     await page.getByRole('button',{name:/Child B/}).click();
  517 |     const partial=page.locator('.durable-conversations details pre');
  518 |     await expect(partial).toContainText('Only current child B activity.');
  519 |     const response=page.waitForResponse(value=>value.url().includes('/conversation_a/observations'));
  520 |     releaseA();await (await response).finished();
  521 |     await page.evaluate(()=>new Promise<void>(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>resolve()))));
  522 |     await expect(page.getByRole('heading',{name:'Child B',exact:true})).toBeVisible();
> 523 |     await expect(partial).not.toContainText('Stale child A activity.');
      |                               ^ Error: expect(locator).not.toContainText(expected) failed
  524 |   } finally { releaseA(); }
  525 | });
  526 | 
```