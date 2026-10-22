import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

/**
 * Studio E2E — real backend + production build.
 *
 * Two webServers come up per run:
 *  1. API (port 7778): `scripts/seed_demo_data.py` rebuilds a fresh
 *     deterministic SQLite db + a JSON manifest the specs read, then
 *     `forge-lens serve` binds it. A non-default port keeps the suite
 *     from colliding with a developer's own `forge-lens serve`.
 *  2. Studio (port 4230): `npm run build` → `e2e/serve-dist.mjs`
 *     (static dist + /v1 proxy → 7778). The Angular dev server is NOT
 *     used — its filesystem watcher exhausts the host inotify limit
 *     here (ENOSPC on `ng serve`), and same-origin /v1 proxying is the
 *     documented no-CORS path (apps/studio/README.md).
 *
 * The app defaults its API base to http://localhost:7777/v1; the shared
 * fixture (e2e/fixtures.ts) overrides it to same-origin `/v1` via
 * localStorage BEFORE app boot — required, since the API has no CORS.
 */

const REPO_ROOT = path.resolve(__dirname, '../..');
const STUDIO_DIR = __dirname;
const RUNTIME_DIR = path.join(STUDIO_DIR, 'e2e', '.runtime');
const DB_PATH = path.join(RUNTIME_DIR, 'lens.db');
const MANIFEST_PATH = path.join(RUNTIME_DIR, 'seed-manifest.json');

const API_PORT = 7778;
const API_URL = `http://127.0.0.1:${API_PORT}`;
const STUDIO_PORT = 4230;
const STUDIO_URL = `http://127.0.0.1:${STUDIO_PORT}`;

export default defineConfig({
  testDir: './e2e',
  testMatch: '*.spec.ts',
  outputDir: './e2e/.artifacts/test-results',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ['list'],
    ['html', { outputFolder: 'e2e/.artifacts/report', open: 'never' }],
    ['json', { outputFile: 'e2e/.artifacts/results.json' }],
  ],
  // WebKit/Firefox run ~3-4× slower than Chromium on this box —
  // give every test headroom rather than per-project timeout forks.
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: STUDIO_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  webServer: [
    {
      command:
        `.venv/bin/python scripts/seed_demo_data.py` +
        ` --db "${DB_PATH}" --manifest "${MANIFEST_PATH}"` +
        ` && .venv/bin/forge-lens serve --db "${DB_PATH}"` +
        ` --host 127.0.0.1 --port ${API_PORT}`,
      cwd: REPO_ROOT,
      url: `${API_URL}/v1/health`,
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command:
        `npm run build &&` +
        ` E2E_PORT=${STUDIO_PORT} LENS_API_ORIGIN=${API_URL}` +
        ` node e2e/serve-dist.mjs`,
      cwd: STUDIO_DIR,
      url: STUDIO_URL,
      timeout: 300_000, // production build ≈35s cold
      reuseExistingServer: false,
      stdout: 'pipe',
      stderr: 'pipe',
    },
  ],
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});
