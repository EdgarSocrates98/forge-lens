import { Component, signal } from '@angular/core';
import { RouterOutlet, RouterLink } from '@angular/router';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink],
  template: `
  <div [class.dark]="dark()">
    <header>
      <strong>Forge Lens</strong>
      <nav>
        <a routerLink="/">traces</a>
        <a routerLink="/ops">ops</a>
        <a routerLink="/capabilities">capabilities</a>
      </nav>
      <button (click)="toggle()" aria-label="toggle theme">{{ dark() ? 'light' : 'dark' }}</button>
    </header>
    <main><router-outlet /></main>
  </div>`,
})
export class App {
  dark = signal(localStorage.getItem('lens-theme') !== 'light');
  toggle(): void {
    this.dark.update(d => !d);
    localStorage.setItem('lens-theme', this.dark() ? 'dark' : 'light');
  }
}
