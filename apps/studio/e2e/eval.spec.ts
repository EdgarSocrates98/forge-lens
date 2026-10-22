import { test, expect } from './fixtures';
import { manifest } from './seed';

const m = manifest();
const COHORT_A = `${m.named.ok_trace}\n${m.named.rich_trace}`;
const COHORT_B = `${m.named.failing_trace}`;

test.describe('datasets & cohort compare', () => {
  test('dataset list + detail + versions render the seeded data', async ({ page }) => {
    await page.goto('/datasets');
    await expect(page.getByRole('heading', { name: 'Datasets' })).toBeVisible();
    const row = page.locator('table tbody tr', { hasText: m.dataset.id });
    await expect(row).toBeVisible();
    await expect(row).toContainText('Demo QA set');
    await expect(row).toContainText('question-answering');

    await row.getByRole('button', { name: m.dataset.id }).click();
    const detail = page.locator('section.panel', { hasText: m.dataset.id });
    await expect(detail).toContainText('Demo QA set');
    await expect(detail).toContainText('question-answering');
    // the seeded version + case count
    const vrow = detail.locator('tbody tr', { hasText: m.dataset.version });
    await expect(vrow).toContainText('seeded demo cases');
    await expect(vrow).toContainText(String(m.dataset.cases));
  });

  test('compare cohorts posts /eval/compare and renders deltas + failures', async ({ page }) => {
    await page.goto('/datasets');
    await page.locator('table tbody tr', { hasText: m.dataset.id })
      .getByRole('button', { name: m.dataset.id }).click();
    await page.locator('summary', { hasText: 'compare cohorts' }).click();

    await page.getByLabel('cohort a trace ids').fill(COHORT_A);
    await page.getByLabel('cohort b trace ids').fill(COHORT_B);
    await page.getByRole('button', { name: 'run compare' }).click();

    // deltas table: error_free drops from 1.0 (a) to 0 (b)
    const report = page.locator('lens-compare-report');
    await expect(report).toContainText('error_free');
    await expect(report).toContainText('tool_success');
    // first table = metric deltas (failure examples below also rows
    // per-evaluator results — keep the assertion scoped)
    const deltaRow = report.locator('table').first()
      .locator('tbody tr', { hasText: 'no_error_spans' });
    await expect(deltaRow).toContainText('2/1'); // n_a/n_b
    await expect(deltaRow).toContainText('1'); // mean_a
    // failure examples recomputes per-trace scores — the failing trace
    // honestly shows its score-0 rows
    await expect(report).toContainText('failure examples');
    const failRow = report.locator('tbody tr', { hasText: m.named.failing_trace });
    await expect(failRow.first()).toBeVisible();
    await expect(failRow.first()).toContainText('0');
    await expect(
      failRow.first().getByRole('link', { name: m.named.failing_trace }),
    ).toHaveAttribute('href', `/traces/${m.named.failing_trace}`);
  });

  test('compare validation refuses a cohort of unknown trace ids', async ({ page }) => {
    await page.goto('/datasets');
    await page.locator('table tbody tr', { hasText: m.dataset.id })
      .getByRole('button', { name: m.dataset.id }).click();
    await page.locator('summary', { hasText: 'compare cohorts' }).click();
    await page.getByLabel('cohort a trace ids').fill(m.named.ok_trace);
    await page.getByLabel('cohort b trace ids').fill('0'.repeat(32));
    await page.getByRole('button', { name: 'run compare' }).click();
    // the server's own 404 surfaces verbatim — a missing id is never skipped
    await expect(page.locator('p.err-banner'))
      .toContainText('compare failed (404)');
  });

  test('new dataset form upserts metadata through the real API', async ({ page }) => {
    await page.goto('/datasets');
    await page.locator('summary', { hasText: 'new dataset' }).click();
    await page.getByLabel('new dataset id').fill('e2e-created-ds');
    await page.getByLabel('new dataset name').fill('E2E created');
    await page.getByRole('button', { name: 'save dataset' }).click();
    await expect(page.locator('p.ok-banner')).toContainText('stored e2e-created-ds');
    await expect(page.locator('table tbody tr', { hasText: 'e2e-created-ds' }))
      .toBeVisible();
  });
});

test.describe('experiments', () => {
  test('seeded experiment renders spec; run now appends a real run', async ({ page }) => {
    await page.goto('/experiments');
    await expect(page.getByRole('heading', { name: 'Experiments' })).toBeVisible();
    const row = page.locator('table tbody tr', { hasText: m.experiment.id });
    await expect(row).toBeVisible();
    await expect(row).toContainText('Demo A/B');
    // status is 'open' only until the first run — when the whole matrix
    // shares one seeded db, an earlier project's "run now" may already
    // have advanced it to 'completed'
    await expect(row).toContainText(/open|completed/);

    await row.getByRole('button', { name: m.experiment.id }).click();
    const detail = page.locator('section.panel');
    await expect(detail).toContainText('Demo A/B');
    await expect(detail).toContainText('a: 2 trace(s) · baseline');
    await expect(detail).toContainText('b: 2 trace(s) · candidate');
    await expect(detail.locator('code', { hasText: 'error_free' })).toBeVisible();

    // count first — a sibling project may already have appended runs
    // (shared db across the browser matrix); the assertion is growth
    const runs = detail.locator('table.runs tbody tr');
    const before = await runs.count();
    await page.getByRole('button', { name: 'run now' }).click();
    // append-only history gains run row(s) and its compare report
    await expect.poll(() => runs.count()).toBeGreaterThan(before);
    await expect(runs.first()).toContainText(/run:\d+/);
    await expect(detail.getByRole('heading', { name: /run:\d+ report/ })).toBeVisible();
    const report = detail.locator('lens-compare-report');
    await expect(report).toContainText('error_free');
    // cohort b carries the failing trace → failure examples surface it
    await expect(
      report.locator('tbody tr', { hasText: m.named.failing_trace }).first(),
    ).toBeVisible();
    // the list row advanced its lifecycle fields in place
    await expect(page.locator('table tbody tr', { hasText: m.experiment.id }))
      .toContainText('completed');
  });

  test('experiment create form rejects an empty cohort client-side', async ({ page }) => {
    await page.goto('/experiments');
    await page.locator('summary', { hasText: 'new experiment' }).click();
    await page.getByLabel('new experiment id').fill('e2e-exp');
    await page.getByLabel('new experiment name').fill('E2E exp');
    await page.getByRole('button', { name: 'save experiment' }).click();
    await expect(page.locator('p.err-banner'))
      .toContainText('cohort a: at least one trace id required');
  });
});
