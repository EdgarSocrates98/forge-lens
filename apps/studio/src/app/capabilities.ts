import { Component, OnInit, inject, signal } from '@angular/core';
import { Api } from './api';

@Component({
  selector: 'lens-caps',
  template: `
  <h1>Host capabilities</h1>
  @if (caps(); as c) {
    <table><thead><tr><th>host</th><th>mechanism</th><th>coverage</th><th>limits</th></tr></thead>
    <tbody>@for (h of hosts(c); track h[0]) {
      <tr><td>{{ h[0] }}</td><td>{{ h[1].mechanism }}</td>
          <td>{{ h[1].coverage }}</td><td>{{ h[1].limits }}</td></tr>
    }</tbody></table>
    <p class="muted">{{ c.note }}</p> }
  @else { <p class="muted">loading…</p> }`,
})
export class CapabilitiesComponent implements OnInit {
  private api = inject(Api);
  caps = signal<{ hosts: Record<string, { mechanism: string; coverage: string; limits: string }>; note: string } | null>(null);
  ngOnInit(): void { this.api.getCapabilities().subscribe(c => this.caps.set(c as never)); }
  hosts(c: { hosts: Record<string, { mechanism: string; coverage: string; limits: string }> }) {
    return Object.entries(c.hosts).sort(([a], [b]) => a.localeCompare(b));
  }
}
