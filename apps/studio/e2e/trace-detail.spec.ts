import { test, expect } from './fixtures';
import { manifest } from './seed';

const m = manifest();

test.describe('trace detail', () => {
  test('waterfall renders the seeded spans with hierarchy and error styling', async ({ page }) => {
    await page.goto(`/traces/${m.named.failing_trace}`);
    await expect(page.getByRole('heading', { name: `Trace ${m.named.failing_trace}` })).toBeVisible();
    await expect(page.locator('p').filter({ hasText: 'spans' }).first()).toContainText('3 spans');
    await expect(page.locator('b.error')).toHaveText('error');

    const wf = page.locator('.wf .row');
    await expect(wf).toHaveCount(3);
    await expect(wf.filter({ hasText: 'tool http_fetch' }).locator('.bar')).toHaveClass(/err/);
    // agent → tool + llm: children are indented one level (14px)
    await expect(wf.filter({ hasText: 'tool http_fetch' }).locator('.name'))
      .toHaveCSS('padding-left', '14px');
  });

  test('selecting a span shows its evidence panel', async ({ page }) => {
    await page.goto(`/traces/${m.named.failing_trace}`);
    await page.locator('.wf .row', { hasText: 'tool http_fetch' }).click();
    const panel = page.locator('section.panel');
    await expect(panel.locator('h3')).toContainText('tool http_fetch');
    await expect(panel).toContainText('kind tool');
    await expect(panel).toContainText('status error');
    await expect(panel.locator('dt', { hasText: 'tool.name' })).toBeVisible();
    await expect(panel.locator('dd')).toContainText('http_fetch');
  });

  test('evidence tab: intel sections + eval battery + replay', async ({ page }) => {
    await page.goto(`/traces/${m.named.failing_trace}`);
    await page.getByRole('button', { name: 'evidence' }).click();

    // intel sections render real derived JSON, not placeholders
    const section = (name: string) =>
      page.locator('h3', { hasText: name }).locator('xpath=following-sibling::pre[1]');
    await expect(section('cost')).toContainText('total_input_tokens');
    await expect(section('failure')).toContainText('category');
    await expect(section('loops')).toContainText('findings');
    await expect(section('graph analysis')).toContainText('{');

    // default eval battery (GET /traces/:id/eval) — the failure trace
    // honestly reports error_free=0 and tool_success=0
    const evalTable = page.locator('table.eval-table tbody tr');
    await expect(evalTable).toHaveCount(2);
    await expect(evalTable.filter({ hasText: 'error_free' })).toContainText('0');
    await expect(evalTable.filter({ hasText: 'tool_success' })).toContainText('0');

    // "run selected" re-posts the checked evaluators
    await page.getByRole('button', { name: 'run selected' }).click();
    await expect(evalTable).toHaveCount(2);
  });

  test('replay controls: step, jump, keyboard and play/pause', async ({ page }) => {
    await page.goto(`/traces/${m.named.failing_trace}`);
    await page.getByRole('button', { name: 'evidence' }).click();
    await page.locator('details.replay summary').click();

    // 7 entries: 3 starts + 3 ends + 1 exception event
    const entries = page.locator('.replay-table tbody tr');
    await expect(entries).toHaveCount(7);
    const pos = page.locator('.replay-controls .pos');
    await expect(pos).toHaveText('1/7');

    await page.getByRole('button', { name: 'step forward' }).click();
    await expect(pos).toHaveText('2/7');
    await page.getByRole('button', { name: 'jump to end' }).click();
    await expect(pos).toHaveText('7/7');
    await page.getByRole('button', { name: 'jump to start' }).click();
    await expect(pos).toHaveText('1/7');

    // jump-to picks the next matching entry (wraps around)
    await page.getByRole('button', { name: 'next error' }).click();
    await expect(pos).toHaveText('4/7'); // tool span_end status error
    await expect(entries.nth(3)).toHaveClass(/cur/);
    await page.getByRole('button', { name: 'next event' }).click();
    await expect(pos).toHaveText('3/7'); // the exception event wraps back

    // keyboard while the controls panel has focus
    await page.locator('.replay-controls').focus();
    await page.keyboard.press('ArrowRight');
    await expect(pos).toHaveText('4/7');
    await page.keyboard.press('ArrowLeft');
    await expect(pos).toHaveText('3/7');

    // play auto-advances; pause freezes the position — pause FIRST,
    // then prove no further 1s ticks fire
    await page.getByRole('button', { name: 'jump to start' }).click();
    await page.getByRole('button', { name: 'play', exact: true }).click();
    await expect(page.getByRole('button', { name: 'pause' })).toBeVisible();
    await expect(pos).not.toHaveText('1/7', { timeout: 5000 });
    await page.getByRole('button', { name: 'pause' }).click();
    const frozen = await pos.textContent();
    await page.waitForTimeout(1600); // > 1 tick at 1×
    await expect(pos).toHaveText(frozen ?? '');
  });

  test('eval params form validates before posting', async ({ page }) => {
    await page.goto(`/traces/${m.named.ok_trace}`);
    await page.getByRole('button', { name: 'evidence' }).click();
    // checking a param-taking evaluator reveals its inputs
    await page.locator('label', { hasText: 'latency_budget' })
      .locator('input[type=checkbox]').check();
    const params = page.locator('.eval-params');
    await expect(params).toContainText('max_duration_ms*');
    // a malformed value blocks the run client-side
    await params.locator('input').fill('abc');
    await page.getByRole('button', { name: 'run selected' }).click();
    await expect(page.locator('p.err-banner')).toContainText('must be a number');
    // a valid value posts and lands in the results table
    await params.locator('input').fill('10000');
    await page.getByRole('button', { name: 'run selected' }).click();
    await expect(page.locator('table.eval-table tbody tr').filter({ hasText: 'latency_budget' }))
      .toContainText('1');
  });

  test('unknown trace id shows the honest error state', async ({ page }) => {
    await page.goto(`/traces/${'0'.repeat(32)}`);
    await expect(page.locator('p.err-banner')).toContainText('cannot load trace (404)');
  });
});
