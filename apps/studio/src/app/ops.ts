import { Component, OnInit, inject, signal } from '@angular/core';
import { JsonPipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Api, OpsSummary } from './api';

@Component({
  selector: 'lens-ops',
  imports: [JsonPipe],
  template: `
  <h1>Operations</h1>
  @if (error()) { <p class="err-banner">{{ error() }}</p> }
  @else if (ops()) { <pre>{{ ops() | json }}</pre> }
  @else { <p class="muted">loading…</p> }`,
})
export class OpsComponent implements OnInit {
  private api = inject(Api);
  ops = signal<OpsSummary | null>(null);
  error = signal<string | null>(null);
  ngOnInit(): void {
    this.api.getOps().subscribe({
      next: o => this.ops.set(o),
      error: (e: HttpErrorResponse) => this.error.set(`ops unavailable (${e.status})`),
    });
  }
}
