import { Component, inject, signal } from '@angular/core';
import { RouterOutlet, RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { Api } from './api';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, FormsModule],
  template: `
  <div [class.dark]="dark()">
    <header>
      <strong>Forge Lens</strong>
      <nav>
        <a routerLink="/">traces</a>
        <a routerLink="/sessions">sessions</a>
        <a routerLink="/ops">ops</a>
        <a routerLink="/capabilities">capabilities</a>
      </nav>
      <button (click)="toggle()" aria-label="toggle theme">{{ dark() ? 'light' : 'dark' }}</button>
      <button (click)="showCfg.update(v => !v)" aria-label="api settings">⚙</button>
    </header>
    @if (showCfg()) {
      <section class="cfg">
        <label>API base <input [(ngModel)]="apiBase" (change)="api.setBase(apiBase)" /></label>
        <label>API key <input [(ngModel)]="apiKey" type="password"
          (change)="api.setKey(apiKey)" placeholder="X-Forge-Lens-Key" /></label>
      </section>
    }
    <main><router-outlet /></main>
  </div>`,
})
export class App {
  api = inject(Api);
  dark = signal(localStorage.getItem('lens-theme') !== 'light');
  showCfg = signal(false);
  apiBase = this.api.base();
  apiKey = localStorage.getItem('lens-api-key') ?? '';
  toggle(): void {
    this.dark.update(d => !d);
    localStorage.setItem('lens-theme', this.dark() ? 'dark' : 'light');
  }
}
