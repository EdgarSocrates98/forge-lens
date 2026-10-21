import { Component, OnInit, inject, signal } from '@angular/core';
import { JsonPipe } from '@angular/common';
import { Api } from './api';

@Component({
  selector: 'lens-ops',
  imports: [JsonPipe],
  template: `
  <h1>Operations</h1>
  @if (ops(); as o) { <pre>{{ o | json }}</pre> }
  @else { <p class="muted">loading…</p> }`,
})
export class OpsComponent implements OnInit {
  private api = inject(Api);
  ops = signal<unknown>(null);
  ngOnInit(): void { this.api.getOps().subscribe(o => this.ops.set(o)); }
}
