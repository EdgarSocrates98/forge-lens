import { test, expect } from './fixtures';
import { manifest } from './seed';

const m = manifest();
const rows = (page: import('@playwright/test').Page) => page.locator('table tbody tr');

test.describe('trace explorer', () => {
  test('lists all seeded traces, newest first', async ({ page }) => {
    await page.goto('/');
    await expect(rows(page)).toHaveCount(m.trace_count);
    // newest = standalone-mini (12:09) — first row carries its id link
    const firstId = await rows(page).first().locator('td a').textContent();
    expect(m.traces[firstId?.trim() ?? '']).toBeTruthy();
    // every visible id is a seeded trace
    const ids = await rows(page).locator('td a').allTextContents();
    expect(ids.length).toBe(m.trace_count);
    for (const id of ids) expect(Object.keys(m.traces)).toContain(id.trim());
    // the seeded failure row renders error styling
    await expect(rows(page).filter({ hasText: m.named.failing_trace })).toHaveClass(/err/);
  });

  test('status filter + URL sync + deep link', async ({ page }) => {
    await page.goto('/');
    await page.getByLabel('status filter').selectOption('error');
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText(m.named.failing_trace);
    await expect(page).toHaveURL(/status=error/);

    // the deep link restores the same filtered view
    await page.goto('/?status=error');
    await expect(page.getByLabel('status filter')).toHaveValue('error');
    await expect(rows(page)).toHaveCount(1);

    // 'unset' shows the seeded observer span
    await page.getByLabel('status filter').selectOption('unset');
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText(m.named.unset_trace);

    // back to all
    await page.getByLabel('status filter').selectOption('');
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(rows(page)).toHaveCount(m.trace_count);
  });

  test('text search matches span names', async ({ page }) => {
    await page.goto('/');
    await page.getByLabel('text search').fill('rag-pipeline');
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText(m.named.rich_trace);
  });

  test('tool + session + duration + since filters', async ({ page }) => {
    await page.goto('/');
    await page.getByLabel('tool filter').fill('calculator');
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText(m.named.multi_tool_trace);

    await page.getByLabel('tool filter').fill('');
    await page.getByLabel('session id filter').fill(m.sessions['alpha'].id);
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(rows(page)).toHaveCount(m.sessions['alpha'].traces);
    await expect(page).toHaveURL(/session_id=session-demo-alpha/);

    // duration floor isolates the slow trace (5000ms envelope)
    await page.getByLabel('session id filter').fill('');
    await page.getByLabel('min duration ms').fill('3000');
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText(m.named.slow_trace);

    // since-filter: input is local time — format the cutoff in the
    // runner's TZ (the browser inherits the same OS timezone)
    const p = (n: number) => String(n).padStart(2, '0');
    const cut = new Date(Date.UTC(2025, 5, 15, 12, 6));
    const local = `${cut.getFullYear()}-${p(cut.getMonth() + 1)}-${p(cut.getDate())}` +
      `T${p(cut.getHours())}:${p(cut.getMinutes())}`;
    await page.getByLabel('min duration ms').fill('');
    await page.getByLabel('since (local time)').fill(local);
    await page.getByRole('button', { name: 'Apply' }).click();
    // ≥ 12:06Z — unset, events, multi-tool, mini
    await expect(rows(page)).toHaveCount(4);
    await expect(page).toHaveURL(/since=/);
  });

  test('row limit pages server-side and columns sort', async ({ page }) => {
    await page.goto('/');
    await page.getByLabel('row limit').fill('3');
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(rows(page)).toHaveCount(3);
    await expect(page).toHaveURL(/limit=3/);

    await page.getByLabel('row limit').fill(String(m.trace_count));
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(rows(page)).toHaveCount(m.trace_count);

    // status sort desc: 'unset' > 'ok' > 'error'
    await page.getByRole('columnheader', { name: 'status' }).click();
    await expect(rows(page).first().locator('td').nth(2)).toHaveText('unset');
    // spans sort desc: the 620-node wide trace leads
    await page.getByRole('columnheader', { name: 'spans' }).click();
    await expect(rows(page).first().locator('td').nth(3)).toHaveText('620');
    // no client "show more" cap is hit at 10 rows
    await expect(page.getByRole('button', { name: /show more/ })).toHaveCount(0);
  });

  test('saved views round-trip a filter set', async ({ page }) => {
    await page.goto('/');
    await page.getByLabel('status filter').selectOption('error');
    await page.getByRole('button', { name: 'Save view' }).click();
    const saved = page.getByLabel('saved views').locator('option', { hasText: 'status=error' });
    await expect(saved).toHaveCount(1);
    const name = await saved.first().getAttribute('value');
    expect(name).toContain('status=error');

    // change filters, then restore the view → filters + rows come back
    await page.getByLabel('status filter').selectOption('');
    await page.getByRole('button', { name: 'Apply' }).click();
    await expect(rows(page)).toHaveCount(m.trace_count);
    await page.getByLabel('saved views').selectOption(name ?? '');
    await expect(page.getByLabel('status filter')).toHaveValue('error');
    await expect(rows(page)).toHaveCount(1);
  });

  test('export JSON downloads the visible trace set', async ({ page }) => {
    await page.goto('/');
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.getByRole('button', { name: 'Export JSON' }).click(),
    ]);
    expect(download.suggestedFilename()).toBe('traces.json');
    const stream = await download.createReadStream();
    const chunks: Buffer[] = [];
    for await (const c of stream) chunks.push(c as Buffer);
    const items = JSON.parse(Buffer.concat(chunks).toString('utf-8')) as unknown[];
    expect(items.length).toBe(m.trace_count);
  });

  test('trace id links deep-link to the detail view', async ({ page }) => {
    await page.goto('/');
    await rows(page).filter({ hasText: m.named.failing_trace }).locator('a').click();
    await expect(page).toHaveURL(new RegExp(`/traces/${m.named.failing_trace}`));
    await expect(page.getByRole('heading', { name: `Trace ${m.named.failing_trace}` })).toBeVisible();
  });
});
