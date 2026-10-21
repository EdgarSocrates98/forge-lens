import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting }
  from '@angular/common/http/testing';
import { SessionsComponent } from './sessions';
import { SessionRow } from './api';

const mk = (id: string, over: Partial<SessionRow> = {}): SessionRow => ({
  session_id: id, traces: 3, spans: 12,
  first_started_at: '2026-03-01T10:00:00Z',
  last_activity_at: '2026-03-01T11:00:00Z',
  ...over,
});
const page = (n: number, from = 0): { items: SessionRow[] } => ({
  items: Array.from({ length: n },
    (_, i) => mk(`sess-${String(from + i).padStart(3, '0')}`)),
});

const nextPage = (http: HttpTestingController) =>
  http.expectOne(r => r.url.endsWith('/sessions'));
const rows = (el: HTMLElement) => el.querySelectorAll('tbody tr');
const loadMoreBtn = (el: HTMLElement) =>
  [...el.querySelectorAll('button')].find(b => b.textContent?.includes('load more'));

describe('SessionsComponent', () => {
  let http: HttpTestingController;
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SessionsComponent],
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('renders session rows linking to the filtered trace list', async () => {
    const f = TestBed.createComponent(SessionsComponent);
    f.detectChanges();
    nextPage(http).flush({
      items: [mk('abc-def-1234567890'), mk('xyz', { last_activity_at: null })],
    });
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    const trs = rows(el);
    expect(trs.length).toBe(2);
    const a = trs[0].querySelector('a');
    expect(a?.getAttribute('href'))
      .toContain('/traces?session_id=abc-def-1234567890');
    expect(a?.textContent?.trim()).toBe('abc-def-1234…'); // 12 chars + ellipsis
    expect(a?.getAttribute('title')).toBe('abc-def-1234567890');
    expect(trs[1].textContent).toContain('—'); // null last_activity_at
  });

  it('paginates with offset when a full page is returned', async () => {
    const f = TestBed.createComponent(SessionsComponent);
    f.detectChanges();
    const r1 = nextPage(http);
    expect(r1.request.params.get('limit')).toBe('50');
    expect(r1.request.params.get('offset')).toBe('0');
    r1.flush(page(50));
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(loadMoreBtn(el)).toBeTruthy();
    loadMoreBtn(el)?.click();
    const r2 = nextPage(http);
    expect(r2.request.params.get('offset')).toBe('50');
    r2.flush(page(10, 50)); // short page → no more
    await f.whenStable();
    expect(f.componentInstance.rows().length).toBe(60);
    expect(loadMoreBtn(el)).toBeFalsy();
  });

  it('filters rows by session id prefix', async () => {
    const f = TestBed.createComponent(SessionsComponent);
    f.detectChanges();
    nextPage(http).flush({ items: [mk('sess-a'), mk('sess-b'), mk('other-1')] });
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(rows(el).length).toBe(3);
    f.componentInstance.search.set('sess-');
    await f.whenStable();
    expect(rows(el).length).toBe(2);
  });

  it('shows the error banner when the API fails', async () => {
    const f = TestBed.createComponent(SessionsComponent);
    f.detectChanges();
    nextPage(http).flush('down', { status: 500, statusText: 'Server Error' });
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(el.querySelector('.err-banner')?.textContent).toContain('500');
    expect(f.componentInstance.loaded()).toBe(true);
  });
});
