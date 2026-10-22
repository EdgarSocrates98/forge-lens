import { test, expect } from './fixtures';

/** Boot, header chrome, theme persistence, API-base wiring, keyboard/focus. */
test.describe('app boot & chrome', () => {
  test('boots to the trace explorer with the full nav', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle('Traces — Forge Lens');
    await expect(page.getByRole('heading', { name: 'Trace Explorer' })).toBeVisible();
    for (const link of ['traces', 'sessions', 'datasets', 'experiments', 'graph', 'ops', 'capabilities']) {
      await expect(page.getByRole('link', { name: link, exact: true })).toBeVisible();
    }
    // seeded data actually rendered — the app is live, not a shell
    await expect(page.locator('table tbody tr').first()).toBeVisible();
  });

  test('theme toggle switches dark↔light and persists', async ({ page }) => {
    await page.goto('/');
    const root = page.locator('app-root > div').first();
    const toggle = page.getByRole('button', { name: 'toggle theme' });

    // default is dark when lens-theme is unset; the button labels the
    // theme it will switch TO ('light' while dark is active)
    await expect(root).toHaveClass(/dark/);
    await expect(toggle).toHaveText('light');

    await toggle.click();
    await expect(root).not.toHaveClass(/dark/);
    await expect(toggle).toHaveText('dark');
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem('lens-theme')))
      .toBe('light');

    // persists across a full reload — and toggles back
    await page.reload();
    const root2 = page.locator('app-root > div').first();
    await expect(root2).not.toHaveClass(/dark/);
    const toggle2 = page.getByRole('button', { name: 'toggle theme' });
    await expect(toggle2).toHaveText('dark');
    await toggle2.click();
    await expect(root2).toHaveClass(/dark/);
    await expect(toggle2).toHaveText('light');
  });

  test('⚙ settings opens the API base/key panel and shows the wired base', async ({ page }) => {
    await page.goto('/');
    const cfg = page.locator('section.cfg');
    await expect(cfg).toBeHidden();
    await page.getByRole('button', { name: 'api settings' }).click();
    await expect(cfg).toBeVisible();
    // the fixture's localStorage override is what the service adopted
    await expect(page.getByLabel('API base')).toHaveValue('/v1');
    await expect(page.getByLabel('API key')).toHaveValue('');
  });

  test('keyboard: Tab walks the header and focus is visible', async ({ page }) => {
    await page.goto('/');
    // first Tab lands on the first nav link
    await page.keyboard.press('Tab');
    let active = await page.evaluate(
      () => (document.activeElement as HTMLElement | null)?.textContent?.trim(),
    );
    expect(active).toBe('traces');

    // every header control is reachable in order
    const seen: string[] = [];
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press('Tab');
      seen.push(
        (await page.evaluate(
          () => (document.activeElement as HTMLElement | null)?.textContent?.trim() ?? '',
        )) ?? '',
      );
    }
    for (const expected of ['sessions', 'datasets', 'experiments', 'graph', 'ops', 'capabilities']) {
      expect(seen).toContain(expected);
    }

    // the focused control must be visible and carry a visible focus ring
    const focus = page.locator(':focus');
    await expect(focus).toHaveCount(1);
    const ring = await focus.evaluate((el) => {
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return {
        outlineStyle: s.outlineStyle,
        outlineWidth: s.outlineWidth,
        boxShadow: s.boxShadow,
        area: r.width * r.height,
      };
    });
    expect(ring.area).toBeGreaterThan(0);
    const hasRing =
      (ring.outlineStyle !== 'none' && ring.outlineWidth !== '0px') ||
      ring.boxShadow !== 'none';
    expect(hasRing).toBeTruthy();
  });
});
