import { Component, OnInit, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { Api, Capabilities, Capability } from './api';

@Component({
  selector: 'lens-caps',
  template: `
  <h1>Host capabilities</h1>
  @if (error()) { <p class="err-banner">{{ error() }}</p> }
  @else if (caps()) {
    <table><thead><tr><th>host</th><th>mechanism</th><th>coverage</th><th>limits</th></tr></thead>
    <tbody>@for (h of hosts(caps()!); track h[0]) {
      <tr><td>{{ h[0] }}</td><td>{{ h[1].mechanism }}</td>
          <td>{{ h[1].coverage }}</td><td>{{ h[1].limits }}</td></tr>
    }</tbody></table>
    <p class="muted">{{ caps()!.note }}</p> }
  @else { <p class="muted">loading…</p> }`,
})
export class CapabilitiesComponent implements OnInit {
  private api = inject(Api);
  caps = signal<Capabilities | null>(null);
  error = signal<string | null>(null);
  ngOnInit(): void {
    this.api.getCapabilities().subscribe({
      next: c => this.caps.set(c),
      error: (e: HttpErrorResponse) => this.error.set(`capabilities unavailable (${e.status})`),
    });
  }
  hosts(c: Capabilities): [string, Capability][] {
    return Object.entries(c.hosts).sort(([a], [b]) => a.localeCompare(b));
  }
}
