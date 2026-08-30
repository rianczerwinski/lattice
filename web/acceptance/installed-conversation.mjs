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
page.setDefaultTimeout(input.timeout_ms ?? 1_800_000);

async function waitForMarker(marker) {
  await expect(page.locator('.response-card article')).toContainText(marker, { timeout: input.timeout_ms ?? 1_800_000 });
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

  await page.getByRole('button', { name: 'Claude', exact: true }).click();
  await page.locator('.composer textarea').fill(`Reply in one short sentence containing exact markers ${markers.root} and ${markers.excerpt}.`);
  await page.getByRole('button', { name: 'Send to anthropic', exact: true }).click();
  await waitForMarker(markers.excerpt);

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
  await selectedActions.getByRole('button', { name: 'Ask Codex', exact: true }).click();
  await expect(page.getByText('1 exact graft selected')).toBeVisible();
  await page.locator('.composer textarea').fill(`Investigate the exact graft and reply with marker ${markers.side}.`);
  await page.getByRole('button', { name: 'Send to openai', exact: true }).click();
  await waitForMarker(markers.side);

  await page.getByRole('button', { name: 'both', exact: true }).click();
  await page.locator('.composer textarea').fill(`Answer independently and include marker ${markers.fanout}.`);
  await page.getByRole('button', { name: 'Send to both', exact: true }).click();
  await waitForMarker(markers.fanout);

  await selectNewestParents(2);
  await page.locator('.composer textarea').fill(`Critique the other provider's selected answer and include marker ${markers.crossfeed}.`);
  await page.getByRole('button', { name: 'Reciprocal cross-feed', exact: true }).click();
  await waitForMarker(markers.crossfeed);

  await selectNewestParents(2);
  await page.getByRole('button', { name: 'Codex', exact: true }).click();
  await page.locator('.composer textarea').fill(`Join the selected cross-fed branches and include marker ${markers.join}.`);
  await page.getByRole('button', { name: 'Inspect context', exact: true }).click();
  await expect(page.locator('.truth-status')).toContainText('Ready for one-action send');
  await expect(page.getByText('Next invocation receives')).toBeVisible();
  await page.getByRole('button', { name: 'Send to openai', exact: true }).click();
  await waitForMarker(markers.join);

  const oldest = page.locator('.history-row').last();
  await oldest.locator('button').click();
  const dormantCheckbox = oldest.getByLabel('Select as parent');
  if (!(await dormantCheckbox.isChecked())) await dormantCheckbox.check();
  await page.getByRole('button', { name: 'Claude', exact: true }).click();
  await page.locator('.composer textarea').fill(`Resume this dormant trailhead and include marker ${markers.dormant}.`);
  await page.getByRole('button', { name: 'Send to anthropic', exact: true }).click();
  await waitForMarker(markers.dormant);

  await page.getByText('Tag into a live Slate topic').click();
  await page.getByLabel('Stable Slate identity').fill(slateIdentity);
  await page.getByLabel('Visible topic label').fill(`Lattice browser acceptance ${input.run_id}`);
  await page.getByRole('button', { name: 'Tag invocation', exact: true }).click();
  await expect(page.getByText(/Live slice updated/)).toBeVisible();
  await expect(page.locator('.truth-status')).toContainText('Manifest reconstructed');

  process.stdout.write(JSON.stringify({ run_id: input.run_id, project_title: title, slate_external_ref: `slate:${slateIdentity}`, markers }));
} finally {
  await context.close();
  await browser.close();
}
