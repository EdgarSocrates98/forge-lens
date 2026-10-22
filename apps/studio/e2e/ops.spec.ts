import { test, expect } from './fixtures';
import { manifest } from './seed';

const m = manifest();

test.describe('operations & capabilities', () => {
  test('ops summary cards, recent failures, alerts panel', async ({ page }) => {
    await page.goto('/ops');
    await expect(page.getByRole('heading', { name: 'Operations' })).toBeVisible();

    // .stat = <div><b>value</b><span>label</span></div> — exact span
    // text, or 'traces' would also match 'traces in window'
    const card = (label: string) =>
      page.locator('.stat span').getByText(label, { exact: true })
        .locator('xpath=../b');
    await expect(card('traces')).toHaveText(String(m.trace_count));
    await expect(card('spans')).toHaveText(String(m.span_count));
    await expect(card('error spans')).toHaveText('2');
    await expect(card('db size')).not.toHaveText('—');
    await expect(card('schema')).toHaveText('1.0.0');
    await expect(card('traces in window')).toHaveText(String(m.trace_count));
    await expect(card('failed in window')).toHaveText('1');
    await expect(page.locator('p.muted').first())
      .toContainText('bounded window (200 traces)');

    // the seeded failure is the one recent failure — linked to detail
    const failLink = page.getByRole('link', { name: m.named.failing_trace });
    await expect(failLink).toBeVisible();

    // alerts evaluate honestly: no saved rules
    await expect(page.locator('p.muted').last())
      .toContainText('no saved rules');

    // refresh re-issues both calls and keeps the data
    await page.getByRole('button', { name: 'refresh' }).click();
    await expect(card('traces')).toHaveText(String(m.trace_count));

    await failLink.click();
    await expect(page).toHaveURL(new RegExp(`/traces/${m.named.failing_trace}`));
  });

  test('capabilities table renders the host matrix', async ({ page }) => {
    await page.goto('/capabilities');
    await expect(page.getByRole('heading', { name: 'Host capabilities' })).toBeVisible();
    const rows = page.locator('table tbody tr');
    expect(await rows.count()).toBeGreaterThanOrEqual(3);
    const claude = rows.filter({ hasText: 'claude_code' });
    await expect(claude).toContainText('hooks + session JSONL logs');
    await expect(claude).toContainText('no in-process span capture');
    // honest note under the matrix
    await expect(page.locator('p.muted').last()).toBeVisible();
  });
});
