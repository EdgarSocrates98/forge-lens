import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting }
  from '@angular/common/http/testing';
import { Api } from './api';

describe('Api', () => {
  let api: Api;
  let http: HttpTestingController;

  beforeEach(() => {
    localStorage.clear();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    api = TestBed.inject(Api);
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('getIncident hits /v1/traces/{id}/incident', () => {
    let got: unknown;
    api.getIncident('t1').subscribe(v => got = v);
    const req = http.expectOne(r => r.url.endsWith('/traces/t1/incident'));
    expect(req.request.method).toBe('GET');
    req.flush({ trace_id: 't1', hypotheses: [], eval_suggestions: [],
                waste: [], bundle_version: '1.0.0' });
    expect((got as { trace_id: string }).trace_id).toBe('t1');
  });

  it('getSecurityReport / getEfficiency / getRecommendations / getCorrelate hit their per-trace routes', () => {
    api.getSecurityReport('t1').subscribe();
    http.expectOne(r => r.method === 'GET'
      && r.url.endsWith('/traces/t1/security')).flush({});
    api.getEfficiency('t1').subscribe();
    http.expectOne(r => r.method === 'GET'
      && r.url.endsWith('/traces/t1/efficiency')).flush({});
    api.getRecommendations('t1').subscribe();
    http.expectOne(r => r.method === 'GET'
      && r.url.endsWith('/traces/t1/recommendations')).flush({});
    api.getCorrelate('t1').subscribe();
    http.expectOne(r => r.method === 'GET'
      && r.url.endsWith('/traces/t1/correlate')).flush({});
  });

  it('getDatasetVersion hits /v1/datasets/{id}/versions/{v}', () => {
    api.getDatasetVersion('ds-1', 'v2').subscribe();
    const req = http.expectOne(r => r.method === 'GET'
      && r.url.endsWith('/datasets/ds-1/versions/v2'));
    req.flush({ dataset_id: 'ds-1', version: 'v2', cases: [] });
  });

  it('listSpecialists unwraps the {specialists} envelope', () => {
    let names: string[] = [];
    api.listSpecialists().subscribe(v => names = v);
    const req = http.expectOne(r => r.method === 'GET'
      && r.url.endsWith('/analyze/specialists'));
    req.flush({ specialists: ['cost_analyst', 'failure_investigator'] });
    expect(names).toEqual(['cost_analyst', 'failure_investigator']);
  });

  it('runAnalysis posts to /v1/analyze/{specialist}/{trace_id} with {} body', () => {
    api.runAnalysis('t1', 'cost_analyst').subscribe();
    const req = http.expectOne(r =>
      r.url.endsWith('/analyze/cost_analyst/t1'));
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({});
    req.flush({ run_id: 'x', specialist: 'cost_analyst', findings: [] });
  });

  it('runAnalysis passes an explicit body through verbatim', () => {
    api.runAnalysis('t1', 'loop_detection_specialist', {
      budget: { max_iterations: 4, token_budget: 5000, timeout_s: 10 },
    }).subscribe();
    const req = http.expectOne(r =>
      r.url.endsWith('/analyze/loop_detection_specialist/t1'));
    expect(req.request.body).toEqual({
      budget: { max_iterations: 4, token_budget: 5000, timeout_s: 10 },
    });
    req.flush({});
  });

  it('listTraces sends the full server-side filter set + offset', () => {
    api.listTraces({
      status: 'error', session_id: 's1', tool: 'grep', q: 'plan',
      model: 'gpt-5', agent: 'devin', host: 'mcp',
      min_total_tokens: 500, project_id: 'proj-1',
      since: '2026-01-01T00:00:00Z', min_duration_ms: 5,
      max_duration_ms: 50, limit: 25, offset: 75,
    }).subscribe();
    const req = http.expectOne(r => r.url.endsWith('/traces'));
    const p = req.request.params;
    expect(p.get('status')).toBe('error');
    expect(p.get('session_id')).toBe('s1');
    expect(p.get('tool')).toBe('grep');
    expect(p.get('q')).toBe('plan');
    expect(p.get('model')).toBe('gpt-5');
    expect(p.get('agent')).toBe('devin');
    expect(p.get('host')).toBe('mcp');
    expect(p.get('min_total_tokens')).toBe('500');
    expect(p.get('project_id')).toBe('proj-1');
    expect(p.get('since')).toBe('2026-01-01T00:00:00Z');
    expect(p.get('min_duration_ms')).toBe('5');
    expect(p.get('max_duration_ms')).toBe('50');
    expect(p.get('limit')).toBe('25');
    expect(p.get('offset')).toBe('75');
    req.flush({ items: [], limit: 25, offset: 75 });
  });

  it('listTraces omits empty filters but always sends limit', () => {
    api.listTraces({ limit: 10 }).subscribe();
    const req = http.expectOne(r => r.url.endsWith('/traces'));
    const p = req.request.params;
    expect(p.get('limit')).toBe('10');
    expect(p.get('status')).toBeNull();
    expect(p.get('model')).toBeNull();
    expect(p.get('project_id')).toBeNull();
    expect(p.get('min_total_tokens')).toBeNull();
    req.flush({ items: [], limit: 10, offset: 0 });
  });
});
