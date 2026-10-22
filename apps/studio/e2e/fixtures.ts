import { test as base } from '@playwright/test';

/**
 * Shared fixture — points the Studio's Api service at the same-origin
 * `/v1` proxy BEFORE the app boots.
 *
 * The app reads `localStorage['lens-api-base']` at service construction
 * (default `http://localhost:7777/v1`). The E2E API lives on 127.0.0.1:7778
 * and serves NO CORS headers, so a cross-origin base would fail every
 * call; same-origin `/v1` is proxied to the API by e2e/serve-dist.mjs
 * (the documented no-CORS path — apps/studio/README.md "API connection").
 */
export const test = base.extend({
  page: async ({ page }, use) => {
    await page.addInitScript(() => {
      localStorage.setItem('lens-api-base', '/v1');
    });
    await use(page);
  },
});

export { expect } from '@playwright/test';
