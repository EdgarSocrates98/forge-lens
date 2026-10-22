import { test, expect } from './fixtures';
import { manifest } from './seed';

const m = manifest();

test.describe('graph explorer', () => {
  test('trace deep-link draws the DAG with nodes and edges', async ({ page }) => {
    await page.goto(`/graph?trace_id=${m.named.rich_trace}`);
    await expect(page.getByRole('heading', { name: 'Graph Explorer' })).toBeVisible();
    const svg = page.locator('svg[aria-label="execution graph"]');
    await expect(svg).toBeVisible();
    // agent → chain → retriever + llm: 4 nodes, ≥3 drawable edges
    await expect(svg.locator('g.node')).toHaveCount(4);
    await expect(svg.locator('line.edge')).toHaveCount(4);
    // the server's own stats render verbatim
    await expect(page.locator('.statline')).toContainText('span_count: 4');
    await expect(page.locator('.statline')).toContainText('edge_count: 4');
  });

  test('node selection, bottlenecks, critical path, zoom/fit', async ({ page }) => {
    await page.goto(`/graph?trace_id=${m.named.rich_trace}`);
    const svg = page.locator('svg[aria-label="execution graph"]');

    // pick a node → the aside shows its stored evidence
    // (aside holds two panels — selected node + bottlenecks, both h3)
    await svg.locator('g.node', { hasText: 'llm claude-demo-3' }).click();
    const aside = page.locator('aside');
    await expect(aside.locator('h3').first()).toHaveText('llm claude-demo-3');
    await expect(aside).toContainText('kind llm');
    await expect(aside).toContainText('token usage recorded');
    await expect(aside.getByRole('link', { name: 'open trace detail →' }))
      .toHaveAttribute('href', `/traces/${m.named.rich_trace}`);

    // bottlenecks table (trace mode) — clickable rows select the node
    const bn = aside.locator('section', { hasText: 'bottlenecks' });
    await expect(bn.locator('tbody tr').first()).toBeVisible();
    await bn.locator('tbody tr', { hasText: 'retriever docs' }).click();
    await expect(aside.locator('h3').first()).toHaveText('retriever docs');

    // critical-path toggle marks the path nodes
    await page.getByRole('button', { name: 'critical path' }).click();
    await expect(page.locator('.controls')).toContainText(/path \d+ms · \d+ nodes/);
    expect(await svg.locator('g.node.cp').count()).toBeGreaterThanOrEqual(2);
    await page.getByRole('button', { name: 'critical path' }).click();
    await expect(svg.locator('g.node.cp')).toHaveCount(0);

    // zoom mutates the viewBox; fit restores the full-bounds camera
    // (selecting a node above centerOn-ed it, so fit first to take
    // the canonical baseline)
    await page.getByRole('button', { name: 'fit' }).click();
    const vb0 = await svg.getAttribute('viewBox');
    await page.getByRole('button', { name: 'zoom in' }).click();
    const vb1 = await svg.getAttribute('viewBox');
    expect(vb1).not.toBe(vb0);
    await page.getByRole('button', { name: 'fit' }).click();
    expect(await svg.getAttribute('viewBox')).toBe(vb0);
  });

  test('session mode renders one node per trace with sequence edges', async ({ page }) => {
    await page.goto(`/graph?session_id=${m.sessions['alpha'].id}`);
    const svg = page.locator('svg[aria-label="execution graph"]');
    await expect(svg.locator('g.node')).toHaveCount(m.sessions['alpha'].traces);
    // edge <line>s are zero-height SVG — invisible to toBeVisible,
    // assert the count instead (3 traces → 2 sequence edges)
    await expect(svg.locator('line.edge.seq')).toHaveCount(2);
    // a trace node opens its detail link
    await svg.locator('g.node').first().click();
    await expect(page.locator('aside').getByRole('link', { name: 'open trace →' }))
      .toBeVisible();
  });

  test('subject form submits and syncs the URL', async ({ page }) => {
    await page.goto('/graph');
    await page.getByLabel('graph subject id').fill(m.named.ok_trace);
    await page.getByRole('button', { name: 'load graph' }).click();
    await expect(page.locator('svg g.node').first()).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`trace_id=${m.named.ok_trace}`));
  });

  test('unknown id shows the honest error state', async ({ page }) => {
    await page.goto(`/graph?trace_id=${'0'.repeat(32)}`);
    await expect(page.locator('p.err-banner')).toContainText('cannot load trace graph (404)');
  });
});
