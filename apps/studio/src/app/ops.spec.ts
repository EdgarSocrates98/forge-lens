import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting }
  from '@angular/common/http/testing';
import { OpsComponent } from './ops';
import { AlertEval, OpsSummary } from './api';

const mkOps = (over: Partial<OpsSummary> = {}): OpsSummary => ({
  project_id: 'default',
  stats: {
    traces: 42, spans: 300, error_spans: 5,
    db_size_bytes: 2048, schema_version: '1',
  },
  recent_traces: 17,
  recent_failures: ['t-fail-1', 't-fail-2'],
  note: 'bounded window (200 traces) — not a fleet-wide total',
  ...over,
});
const mkAlert = (over: Partial<AlertEval> = {}): AlertEval => ({
  fired: [{
    rule_id: 'err-rate', metric: 'error_rate', op: '>',
    threshold: 0.1, observed: 0.5, severity: 'critical',
    message: 'error_rate > 0.1 (observed 0.5)',
  }],
  evaluated: 2,
  project_id: 'default',
  ...over,
});

const opsReq = (http: HttpTestingController) =>
  http.expectOne(r => r.method === 'GET' && r.url.endsWith('/ops/summary'));
const alertReq = (http: HttpTestingController) =>
  http.expectOne(r => r.method === 'POST' && r.url.endsWith('/alerts/evaluate'));
const flushOk = (http: HttpTestingController,
  ops = mkOps(), alerts = mkAlert()) => {
  opsReq(http).flush(ops);
  alertReq(http).flush(alerts);
};
const btn = (el: HTMLElement, text: string) =>
  [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === text);

describe('OpsComponent', () => {
  let http: HttpTestingController;
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [OpsComponent],
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('renders store stats as cards and shows the bound note verbatim', async () => {
    const f = TestBed.createComponent(OpsComponent);
    f.detectChanges();
    flushOk(http);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    const grid = el.querySelector('.stat-grid')?.textContent ?? '';
    expect(grid).toContain('42');          // traces
    expect(grid).toContain('300');         // spans
    expect(grid).toContain('error spans');
    expect(grid).toContain('2.0 KiB');     // db_size_bytes humanized
    expect(grid).toContain('17');          // traces in window
    expect(grid).toContain('failed in window');
    expect(el.textContent).toContain('bounded window (200 traces)');
    expect(el.textContent).toContain('project default');
  });

  it('links each recent failure to its trace detail', async () => {
    const f = TestBed.createComponent(OpsComponent);
    f.detectChanges();
    flushOk(http, mkOps({ recent_failures: ['abc-123', 'def-456'] }));
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    const links = [...el.querySelectorAll('tbody a')];
    expect(links.length).toBe(2);
    expect(links[0].getAttribute('href')).toBe('/traces/abc-123');
    expect(links[1].getAttribute('href')).toBe('/traces/def-456');
  });

  it('posts {use_saved:true} and renders fired rules', async () => {
    const f = TestBed.createComponent(OpsComponent);
    f.detectChanges();
    const al = alertReq(http);
    expect(al.request.body).toEqual({ use_saved: true });
    opsReq(http).flush(mkOps());
    al.flush(mkAlert());
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    const text = el.textContent ?? '';
    expect(text).toContain('err-rate');
    expect(text).toContain('error_rate');
    expect(text).toContain('0.5');
    expect(text).toContain('critical');
    expect(text).toContain('fired');
    expect(text).toContain('2 saved rule(s) evaluated');
    expect(el.querySelector('b.error')?.textContent).toContain('fired');
  });

  it('renders honest empty states', async () => {
    const f = TestBed.createComponent(OpsComponent);
    f.detectChanges();
    flushOk(http, mkOps({ recent_failures: [] }),
      mkAlert({ fired: [], evaluated: 3 }));
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(el.textContent).toContain('no failed traces in the bounded window');
    expect(el.textContent).toContain('all 3 evaluated rule(s) passed');
  });

  it('says so when no rules are saved', async () => {
    const f = TestBed.createComponent(OpsComponent);
    f.detectChanges();
    flushOk(http, mkOps(), mkAlert({ fired: [], evaluated: 0 }));
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(el.textContent).toContain('no saved rules');
  });

  it('shows an error banner when the summary call fails but keeps alerts', async () => {
    const f = TestBed.createComponent(OpsComponent);
    f.detectChanges();
    opsReq(http).flush('down', { status: 500, statusText: 'Server Error' });
    alertReq(http).flush(mkAlert());
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(el.querySelector('.err-banner')?.textContent).toContain('500');
    expect(el.textContent).toContain('err-rate'); // alerts unaffected
  });

  it('shows an error banner when alerts evaluation fails but keeps the summary', async () => {
    const f = TestBed.createComponent(OpsComponent);
    f.detectChanges();
    opsReq(http).flush(mkOps());
    alertReq(http).flush('down', { status: 503, statusText: 'Unavailable' });
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(el.querySelector('.err-banner')?.textContent).toContain('503');
    expect(el.querySelector('.stat-grid')?.textContent).toContain('42');
  });

  it('refresh re-issues both calls', async () => {
    const f = TestBed.createComponent(OpsComponent);
    f.detectChanges();
    flushOk(http);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    btn(el, 'refresh')?.click();
    flushOk(http, mkOps({ recent_traces: 20 }),
      mkAlert({ fired: [], evaluated: 1 }));
    await f.whenStable();
    expect(el.querySelector('.stat-grid')?.textContent).toContain('20');
    expect(el.textContent).toContain('all 1 evaluated rule(s) passed');
  });
});
