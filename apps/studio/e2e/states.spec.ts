import { test, expect } from './fixtures';
import { manifest } from './seed';

const m = manifest();

/**
 * Empty/degraded states — response-level faults injected through the
 * real network path (route interception), plus genuine 404s against the
 * live server. The seeded db stays untouched.
 */
test.describe('empty & error states', () => {
  test('empty trace list renders the honest empty state', async ({ page }) => {
    await page.route(/\/v1\/traces\?/, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '{"items":[]}' }));
    await page.goto('/');
    await expect(page.locator('p.muted'))
      .toContainText('no traces — instrument a run or check the API base');
    await expect(page.locator('table tbody tr')).toHaveCount(0);
  });

  test('API down renders the unreachable banner, not a crash', async ({ page }) => {
    await page.route('**/v1/**', (route) => route.abort('connectionrefused'));
    await page.goto('/');
    await expect(page.locator('p.err-banner'))
      .toContainText('API unreachable');
    await expect(page.locator('p.err-banner'))
      .toContainText('is forge-lens serve running?');
  });

  test('sessions view surfaces a server error verbatim', async ({ page }) => {
    await page.route(/\/v1\/sessions/, (route) =>
      route.fulfill({ status: 500, contentType: 'application/json',
        body: '{"detail":"boom"}' }));
    await page.goto('/sessions');
    await expect(page.locator('p.err-banner'))
      .toContainText('sessions unavailable (500)');
  });

  test('deep-link to an unknown trace id → real 404 handling', async ({ page }) => {
    await page.goto(`/traces/${'f'.repeat(32)}`);
    await expect(page.locator('p.err-banner'))
      .toContainText('cannot load trace (404)');
    // header chrome still works — the app didn't crash
    await expect(page.getByRole('link', { name: 'traces', exact: true })).toBeVisible();
    await page.getByRole('link', { name: 'traces', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Trace Explorer' })).toBeVisible();
    await expect(page.locator('table tbody tr').first()).toBeVisible();
  });

  test('empty datasets/experiments keep their create affordances', async ({ page }) => {
    await page.route(/\/v1\/(datasets|experiments)(\?|$)/, (route) => {
      const isDatasets = route.request().url().includes('datasets');
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: isDatasets ? '[]' : '{"items":[],"limit":500,"offset":0}',
      });
    });
    await page.goto('/datasets');
    await expect(page.getByText('no datasets — create one below.')).toBeVisible();
    await page.goto('/experiments');
    await expect(page.getByText('no experiments — create one below.')).toBeVisible();
  });

  test('graph without a subject shows the entry hint', async ({ page }) => {
    await page.goto('/graph');
    await expect(page.locator('p.muted'))
      .toContainText('enter a trace id — or deep-link');
  });
});
