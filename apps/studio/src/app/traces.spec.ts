import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, ParamMap, Router, convertToParamMap, provideRouter }
  from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting }
  from '@angular/common/http/testing';
import { TracesComponent } from './traces';

let params: ParamMap = convertToParamMap({});

describe('TracesComponent', () => {
  let http: HttpTestingController;
  let navSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(async () => {
    localStorage.clear();
    params = convertToParamMap({});
    await TestBed.configureTestingModule({
      imports: [TracesComponent],
      providers: [
        provideRouter([{ path: '**', component: TracesComponent }]),
        provideHttpClient(),
        provideHttpClientTesting(),
        // the component only reads snapshot.queryParamMap — stub it directly so
        // deep-link state can be set without a router-outlet navigation
        { provide: ActivatedRoute,
          useFactory: () => ({ snapshot: { queryParamMap: params } }) },
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
    navSpy = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  });
  afterEach(() => http.verify());

  const flushInit = () =>
    http.expectOne(r => r.url.endsWith('/traces')).flush({ items: [] });

  it('hydrates filter state from the URL query string', () => {
    params = convertToParamMap({
      status: 'error', q: 'plan', tool: 'grep', session_id: 's9',
      min_duration_ms: '5', max_duration_ms: '50',
      since: '2026-03-01T10:00:00.000Z', limit: '25',
    });
    const f = TestBed.createComponent(TracesComponent);
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.status).toBe('error');
    expect(c.q).toBe('plan');
    expect(c.tool).toBe('grep');
    expect(c.sessionId).toBe('s9');
    expect(c.minDuration).toBe(5);
    expect(c.maxDuration).toBe(50);
    expect(c.limit).toBe(25);
    expect(c.since).toBeTruthy(); // ISO instant re-rendered as datetime-local
    const req = http.expectOne(r => r.url.endsWith('/traces'));
    expect(req.request.params.get('session_id')).toBe('s9');
    req.flush({ items: [] });
  });

  it('sends the new filters to the API and mirrors them in the URL', () => {
    const f = TestBed.createComponent(TracesComponent);
    f.detectChanges();
    flushInit();
    const c = f.componentInstance;
    c.status = 'error'; c.q = 'agent'; c.tool = 'bash'; c.sessionId = 'sess-1';
    c.minDuration = 10; c.maxDuration = 999; c.since = '2026-03-01T10:15';
    c.load();
    const req = http.expectOne(r => r.url.endsWith('/traces'));
    const p = req.request.params;
    expect(p.get('status')).toBe('error');
    expect(p.get('q')).toBe('agent');
    expect(p.get('tool')).toBe('bash');
    expect(p.get('session_id')).toBe('sess-1');
    expect(p.get('min_duration_ms')).toBe('10');
    expect(p.get('max_duration_ms')).toBe('999');
    expect(p.get('since')).toBe(new Date('2026-03-01T10:15').toISOString());
    req.flush({ items: [] });
    const qp = navSpy.mock.calls.at(-1)?.[1]?.queryParams as
      Record<string, unknown>;
    expect(qp['session_id']).toBe('sess-1');
    expect(qp['q']).toBe('agent');
    expect(qp['tool']).toBe('bash');
    expect(qp['min_duration_ms']).toBe(10);
    expect(qp['since']).toBe(new Date('2026-03-01T10:15').toISOString());
  });

  it('saved views round-trip the new fields', () => {
    const f = TestBed.createComponent(TracesComponent);
    f.detectChanges();
    flushInit();
    const c = f.componentInstance;
    c.q = 'q1'; c.tool = 't1'; c.sessionId = 's1';
    c.minDuration = 3; c.maxDuration = 30; c.since = '2026-02-01T08:30';
    c.saveView();
    c.q = ''; c.tool = ''; c.sessionId = '';
    c.minDuration = null; c.maxDuration = null; c.since = '';
    c.viewSel = c.views()[0];
    c.applyView();
    expect(c.q).toBe('q1');
    expect(c.tool).toBe('t1');
    expect(c.sessionId).toBe('s1');
    expect(c.minDuration).toBe(3);
    expect(c.maxDuration).toBe(30);
    expect(c.since).toBe('2026-02-01T08:30');
    http.expectOne(r => r.url.endsWith('/traces')).flush({ items: [] });
  });

  it('tolerates legacy saved views missing the new keys', () => {
    localStorage.setItem('lens-view:legacy',
      JSON.stringify({ status: 'ok', limit: 25 }));
    const f = TestBed.createComponent(TracesComponent);
    f.detectChanges();
    flushInit();
    const c = f.componentInstance;
    c.sessionId = 'leftover';
    c.viewSel = 'legacy';
    c.applyView();
    expect(c.status).toBe('ok');
    expect(c.limit).toBe(25);
    expect(c.sessionId).toBe('');
    expect(c.minDuration).toBeNull();
    http.expectOne(r => r.url.endsWith('/traces')).flush({ items: [] });
  });
});
