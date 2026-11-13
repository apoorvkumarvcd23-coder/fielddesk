import { chromium, expect } from '@playwright/test';
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
const baseURL = process.env.FIELDDESK_BASE_URL;
const origin = new URL(baseURL);
if (!['127.0.0.1','localhost','[::1]'].includes(origin.hostname)) throw new Error('A loopback demo is required');
const accounts = JSON.parse(readFileSync(process.env.FIELDDESK_CREDENTIALS_FILE, 'utf8'));
const out = resolve(process.env.FIELDDESK_ARTIFACTS || 'artifacts/workflow');
mkdirSync(out, { recursive: true });
const fixture = JSON.parse(readFileSync(new URL('../fixtures/rag_manuals.json', import.meta.url), 'utf8'))[0];
const browser = await chromium.launch();
const contexts = [];
async function pageFor(name) {
  const context = await browser.newContext({ baseURL, viewport: { width: 1380, height: 1000 } }); contexts.push(context);
  const page = await context.newPage();
  await page.goto('/');
  await page.getByLabel('Identity access token').fill(accounts[name].token);
  await page.getByRole('button', { name: 'Connect identity', exact: true }).click();
  await expect(page.locator('#identity')).toContainText('Connected as ' + name, { timeout: 30000 });
  expect(await page.locator('#token').inputValue()).toBe('');
  return page;
}
async function request(page, name, path, body, method) {
  return page.request.fetch(path, { method: method || (body ? 'POST' : 'GET'),
    headers: { Authorization: 'Bearer ' + accounts[name].token }, ...(body ? { data: body } : {}) });
}
async function question(page, value, handoff) {
  await page.getByLabel('Your question', { exact: true }).fill(value);
  const response = page.waitForResponse(r => r.url().endsWith('/rag/chat') && r.request().method() === 'POST', { timeout: 180000 });
  await page.getByRole('button', { name: 'Find an answer', exact: true }).click();
  const actual = await response; expect(actual.status()).toBe(200);
  const result = await actual.json(); expect(result.handoff).toBe(handoff);
  await expect(page.locator('#answer-status')).toContainText(handoff ? 'Operator handoff' : 'Cited local-model answer', { timeout: 180000 });
  return result;
}
try {
  if (process.argv.includes('--outage')) {
    const page = await pageFor('alice');
    const result = await question(page, 'What must I record in a projector repair ticket?', true);
    expect(result.reason).toBe('model_unavailable');
    expect(result.citations).toEqual([]);
    await page.screenshot({ path: resolve(out,'model-outage.png'), fullPage: true });
    writeFileSync(resolve(out,'browser-outage.json'), JSON.stringify(result,null,2));
  } else {
    const admin = await pageFor('admin');
    await admin.getByLabel('Source key', { exact: true }).fill('synthetic-ui-projector');
    await admin.getByLabel('Document title', { exact: true }).fill(fixture.title);
    await admin.getByLabel('Approved document text', { exact: true }).fill(fixture.body);
    const queued = admin.waitForResponse(r => r.url().endsWith('/rag/documents') && r.request().method() === 'POST');
    await admin.getByRole('button', { name: 'Queue document', exact: true }).click();
    const jobResponse = await queued; expect(jobResponse.status()).toBe(202);
    const job = await jobResponse.json();
    // Repeat actual ingest through the UI: one immutable version/job.
    const replayWait = admin.waitForResponse(r => r.url().endsWith('/rag/documents') && r.request().method() === 'POST');
    await admin.getByRole('button', { name: 'Queue document', exact: true }).click();
    const replay = await (await replayWait).json(); expect(replay.id).toBe(job.id);
    for (const name of ['alice','operator']) {
      await admin.getByLabel('Reader identity', { exact: true }).selectOption(accounts[name].id);
      await admin.getByRole('button', { name: 'Save document grant', exact: true }).click();
      await expect(admin.locator('#grant-status')).toContainText('Document grant saved');
      await expect(admin.getByRole('button', { name: 'Save document grant', exact: true })).toBeEnabled();
    }
    await expect.poll(async () => {
      await admin.getByRole('button', { name: 'Refresh indexing jobs', exact: true }).click();
      const ready = await request(admin,'admin','/rag/jobs/'+job.id);
      return (await ready.json()).status;
    }, { timeout: 120000, intervals: [500,1000,2000] }).toBe('ready');
    await admin.getByRole('button', { name: 'Refresh indexing jobs', exact: true }).click();
    await expect(admin.locator('#jobs')).toContainText('READY');
    await admin.screenshot({ path: resolve(out,'indexing-admin.png'), fullPage: true });
    await admin.setViewportSize({ width: 390, height: 844 });
    await admin.screenshot({ path: resolve(out,'indexing-mobile.png'), fullPage: true });
    expect(await admin.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await admin.context().close(); // Keep CI memory bounded before loading the 9B model.
    const alice = await pageFor('alice');
    const answer = await question(alice,'What must I record in a projector repair ticket?',false);
    expect(answer.citations.length).toBeGreaterThan(0);
    await alice.getByRole('button', { name: 'View permitted source', exact: true }).first().click();
    await expect(alice.locator('#citations')).toContainText('room code');
    await alice.screenshot({ path: resolve(out,'cited-answer.png'), fullPage: true });
    const unsupported = await question(alice,'What is the price of a projector repair?',true);
    await expect(alice.getByRole('button', { name: 'Request operator review', exact: true })).toBeDisabled();
    await alice.getByLabel('Share this question with service operators', { exact: true }).check();
    const reviewWait = alice.waitForResponse(r => r.url().endsWith('/rag/reviews') && r.request().method() === 'POST');
    await alice.getByRole('button', { name: 'Request operator review', exact: true }).click();
    const review = await (await reviewWait).json();
    await expect(alice.locator('#review-status')).toContainText('No ticket has been created');
    const userDenied = await request(alice,'alice','/rag/reviews/'+review.id+'/claim',{ expected_revision: review.revision });
    expect(userDenied.status()).toBe(403);
    const bob = await pageFor('bob');
    expect((await request(bob,'bob','/rag/reviews/'+review.id)).status()).toBe(404);
    await bob.context().close();
    const operator = await pageFor('operator');
    const row = operator.locator('[data-review-id="'+review.id+'"]');
    await row.getByRole('button', { name: 'Claim review', exact: true }).click();
    await expect(row).toContainText('in review');
    await row.getByLabel('Human ticket title', { exact: true }).fill('Synthetic projector pricing enquiry');
    await row.getByRole('button', { name: 'Propose service ticket', exact: true }).click();
    await expect(operator.locator('#review-status')).toContainText('Explicit approval is still required');
    const pending = await (await request(operator,'operator','/rag/reviews/'+review.id)).json();
    expect(pending.action.ticket_id).toBeNull();
    const approveWait = operator.waitForResponse(r => r.url().endsWith('/approve') && r.request().method() === 'POST');
    await row.getByRole('button', { name: 'Approve service ticket', exact: true }).click();
    const approved = await (await approveWait).json(); expect(approved.action.ticket_id).toBeTruthy();
    await expect(row).toContainText('resolved');
    const repeated = await request(operator,'operator','/rag/reviews/'+review.id+'/approve',{ expected_revision: pending.revision });
    expect((await repeated.json()).action.ticket_id).toBe(approved.action.ticket_id);
    await row.getByRole('button', { name: 'View service ticket', exact: true }).click();
    await expect(row).toContainText(approved.action.ticket_id);
    await operator.screenshot({ path: resolve(out,'approved-review.png'), fullPage: true });
    await alice.getByRole('button', { name: 'Refresh reviews', exact: true }).click();
    await alice.getByRole('button', { name: 'View service ticket', exact: true }).click();
    await expect(alice.locator('#reviews')).toContainText(approved.action.ticket_id);
    expect((await request(alice,'bob','/rag/tickets/'+approved.action.ticket_id)).status()).toBe(404);
    // Clear all private state and ensure a different identity sees no prior answer/review.
    await alice.getByRole('button', { name: 'Disconnect', exact: true }).click();
    await expect(alice.locator('#answer')).toHaveText('');
    await expect(alice.locator('#reviews')).toHaveText('');
    await alice.getByLabel('Identity access token').fill(accounts.bob.token);
    await alice.getByRole('button', { name: 'Connect identity', exact: true }).click();
    await expect(alice.locator('#identity')).toContainText('Connected as bob');
    await expect(alice.locator('#reviews')).toContainText('No review requests yet');
    await expect(alice.locator('#answer')).toHaveText('');
    writeFileSync(resolve(out,'browser-workflow.json'), JSON.stringify({
      integration:'Actual Chromium UI → FastAPI → persisted data and actual local models; no API/model/browser mocks',
      job, replay_job_id:replay.id, answer, unsupported, review_id:review.id,
      approved, permission_checks:{user_claim:403,cross_user_review:404,cross_user_ticket:404,disconnect_clears_private_state:true},
      images:['indexing-admin.png','indexing-mobile.png','cited-answer.png','approved-review.png']
    },null,2));
  }
} finally {
  for (const context of contexts) await context.close().catch(() => {});
  await browser.close();
}
