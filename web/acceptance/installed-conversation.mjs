#!/usr/bin/env node
import { chromium, expect } from '@playwright/test';

async function stdinJson() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

const input = await stdinJson();
if (!input || typeof input.base_url !== 'string' || typeof input.bootstrap_token !== 'string' || typeof input.run_id !== 'string') throw new Error('installed browser acceptance stdin is malformed');
const markers = {
  root: `BROWSER-ROOT-${input.run_id}`,
  excerpt: `BROWSER-EXCERPT-${input.run_id}`,
  side: `BROWSER-SIDE-${input.run_id}`,
  fanout: `BROWSER-FANOUT-${input.run_id}`,
  crossfeed: `BROWSER-XFEED-${input.run_id}`,
  join: `BROWSER-JOIN-${input.run_id}`,
  dormant: `BROWSER-DORMANT-${input.run_id}`,
};
const title = `Installed browser acceptance ${input.run_id}`;
const slateIdentity = `lattice-browser-acceptance-${input.run_id.toLowerCase()}`;
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const context = await browser.newContext();
const page = await context.newPage();
const providerTimeoutMs = input.timeout_ms ?? 1_800_000;
page.setDefaultTimeout(Math.min(providerTimeoutMs, 30_000));

function checkpoint(name) {
  process.stderr.write(`[browser-acceptance] ${name}\n`);
}

async function waitForMarker(marker) {
  await expect(page.locator('.response-card article')).toContainText(marker, { timeout: providerTimeoutMs });
  await expect(page.locator('.terminal-failure')).toHaveCount(0);
}

async function selectNewestParents(count) {
  const rows = page.locator('.history-row');
  await expect(rows.nth(count - 1)).toBeVisible();
  for (const checkbox of await page.getByLabel('Select as parent').all()) {
    if (await checkbox.isChecked()) await checkbox.uncheck();
  }
  for (let index = 0; index < count; index += 1) {
    const checkbox = rows.nth(index).getByLabel('Select as parent');
    await checkbox.check();
  }
  await expect(page.locator('.parent-count')).toContainText(`${count} parent`);
}

try {
  await page.goto(`${input.base_url.replace(/\/$/, '')}/#operator-bootstrap=${encodeURIComponent(input.bootstrap_token)}`, { waitUntil: 'networkidle' });
  await page.getByPlaceholder('New project').fill(title);
  await page.getByPlaceholder('What is this for?').fill('Synthetic installed-browser qualification; no personal corpus.');
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  await expect(page.getByText('Say what should happen next.')).toBeVisible();
  checkpoint('blank project created');

  await page.getByRole('button', { name: 'Claude', exact: true }).click();
  await page.locator('.composer textarea').fill(`Reply in one short sentence containing exact markers ${markers.root} and ${markers.excerpt}.`);
  await page.getByRole('button', { name: 'Send to anthropic', exact: true }).click();
  checkpoint('Claude root dispatched');
  await waitForMarker(markers.excerpt);
  checkpoint('Claude root completed');

  await page.locator('.response-card article').evaluate((article, phrase) => {
    const walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node && !node.textContent?.includes(phrase)) node = walker.nextNode();
    if (!node?.textContent) throw new Error(`exact excerpt marker was not rendered: ${phrase}`);
    const start = node.textContent.indexOf(phrase);
    const range = document.createRange();
    range.setStart(node, start);
    range.setEnd(node, start + phrase.length);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    article.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  }, markers.excerpt);
  const selectedActions = page.locator('details.context-actions.selected-text');
  await expect(selectedActions).toBeVisible();
  await selectedActions.locator('summary').click();
  await expect(selectedActions.getByRole('button', { name: 'Ask Codex', exact: true })).toBeVisible();
  await selectedActions.getByRole('button', { name: 'Ask Codex', exact: true }).click();
  await expect(page.getByText('1 exact graft selected')).toBeVisible();
  await page.locator('.composer textarea').fill(`Investigate the exact graft and reply with marker ${markers.side}.`);
  await page.getByRole('button', { name: 'Send to openai', exact: true }).click();
  checkpoint('Codex side quest dispatched');
  await waitForMarker(markers.side);
  checkpoint('Codex side quest completed');

  await page.getByRole('button', { name: 'both', exact: true }).click();
  await page.locator('.composer textarea').fill(`Answer independently and include marker ${markers.fanout}.`);
  await page.getByRole('button', { name: 'Send to both', exact: true }).click();
  checkpoint('fan-out dispatched');
  await waitForMarker(markers.fanout);
  checkpoint('fan-out completed');

  await selectNewestParents(2);
  await page.locator('.composer textarea').fill(`Critique the other provider's selected answer and include marker ${markers.crossfeed}.`);
  await page.getByRole('button', { name: 'Reciprocal cross-feed', exact: true }).click();
  checkpoint('reciprocal cross-feed dispatched');
  await waitForMarker(markers.crossfeed);
  checkpoint('reciprocal cross-feed completed');

  await selectNewestParents(2);
  await page.getByRole('button', { name: 'Codex', exact: true }).click();
  await page.locator('.composer textarea').fill(`Join the selected cross-fed branches and include marker ${markers.join}.`);
  await page.getByRole('button', { name: 'Inspect context', exact: true }).click();
  await expect(page.locator('.truth-status')).toContainText('Ready for one-action send');
  await expect(page.getByText('Next invocation receives')).toBeVisible();
  await page.getByRole('button', { name: 'Send to openai', exact: true }).click();
  checkpoint('fan-in dispatched');
  await waitForMarker(markers.join);
  checkpoint('fan-in completed');

  const oldest = page.locator('.history-row').last();
  await oldest.locator('button').click();
  const dormantCheckbox = oldest.getByLabel('Select as parent');
  if (!(await dormantCheckbox.isChecked())) await dormantCheckbox.check();
  await page.getByRole('button', { name: 'Claude', exact: true }).click();
  await page.locator('.composer textarea').fill(`Resume this dormant trailhead and include marker ${markers.dormant}.`);
  await page.getByRole('button', { name: 'Send to anthropic', exact: true }).click();
  checkpoint('dormant branch dispatched');
  await waitForMarker(markers.dormant);
  checkpoint('dormant branch completed');

  await page.getByText('Tag into a live Slate topic').click();
  await page.getByLabel('Stable Slate identity').fill(slateIdentity);
  await page.getByLabel('Visible topic label').fill(`Lattice browser acceptance ${input.run_id}`);
  await page.getByRole('button', { name: 'Tag invocation', exact: true }).click();
  await expect(page.getByText(/Live slice updated/)).toBeVisible();
  await expect(page.locator('.truth-status')).toContainText('Manifest reconstructed');

  process.stdout.write(JSON.stringify({ run_id: input.run_id, project_title: title, slate_external_ref: `slate:${slateIdentity}`, markers }));
} catch (error) {
  const buttons = await page.locator('button').evaluateAll((items) => items.filter((item) => item.offsetParent !== null).map((item) => ({ text: item.textContent?.trim() ?? '', disabled: item.disabled })));
  const visibleErrors = await page.locator('.surface-failure, .error, .terminal-failure').allTextContents();
  const prompt = await page.locator('.composer textarea').count() ? await page.locator('.composer textarea').inputValue() : null;
  process.stderr.write(`${JSON.stringify({ phase: 'browser-journey-failed', url: page.url(), prompt, visible_errors: visibleErrors, visible_buttons: buttons })}\n`);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
