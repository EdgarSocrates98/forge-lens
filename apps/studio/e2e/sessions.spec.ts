import { test, expect } from './fixtures';
import { manifest } from './seed';

const m = manifest();
const rows = (page: import('@playwright/test').Page) => page.locator('table tbody tr');

test.describe('sessions', () => {
  test('lists seeded sessions, most recent activity first', async ({ page }) => {
    await page.goto('/sessions');
    await expect(page.getByRole('heading', { name: 'Sessions' })).toBeVisible();
    await expect(rows(page)).toHaveCount(2);

    // beta last-activity (12:04:01.2) > alpha (12:02:01.8)
    const firstLink = rows(page).first().locator('td a');
    await expect(firstLink).toHaveAttribute('title', m.sessions['beta'].id);
    await expect(rows(page).first().locator('td').nth(1)).toHaveText('2');
    await expect(rows(page).first().locator('td').nth(2)).toHaveText('6');
    await expect(rows(page).nth(1).locator('td a'))
      .toHaveAttribute('title', m.sessions['alpha'].id);
    await expect(rows(page).nth(1).locator('td').nth(1)).toHaveText('3');
    await expect(rows(page).nth(1).locator('td').nth(2)).toHaveText('11');
  });

  test('client-side prefix filter narrows the list', async ({ page }) => {
    await page.goto('/sessions');
    await expect(rows(page)).toHaveCount(2);
    await page.getByLabel('filter sessions').fill('session-demo-a');
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first().locator('td a'))
      .toHaveAttribute('title', m.sessions['alpha'].id);

    // a non-matching prefix shows the honest empty message
    await page.getByLabel('filter sessions').fill('zzz');
    await expect(rows(page)).toHaveCount(0);
    await expect(page.locator('p.muted'))
      .toContainText('no sessions — ingest traces carrying a session id');
  });

  test('session row deep-links to the filtered trace list', async ({ page }) => {
    await page.goto('/sessions');
    // link text is truncated (…); the title attr carries the full id
    await page.locator(`td a[title="${m.sessions['alpha'].id}"]`).click();
    await expect(page).toHaveURL(/\/traces\?session_id=session-demo-alpha/);
    await expect(page.getByLabel('session id filter')).toHaveValue(m.sessions['alpha'].id);
    await expect(rows(page)).toHaveCount(m.sessions['alpha'].traces);
  });
});
