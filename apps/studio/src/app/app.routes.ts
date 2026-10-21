import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    loadComponent: () => import('./traces').then(m => m.TracesComponent),
    title: 'Traces — Forge Lens',
  },
  {
    path: 'traces/:id',
    loadComponent: () => import('./trace-detail').then(m => m.TraceDetailComponent),
    title: 'Trace — Forge Lens',
  },
  {
    path: 'ops',
    loadComponent: () => import('./ops').then(m => m.OpsComponent),
    title: 'Operations — Forge Lens',
  },
  {
    path: 'capabilities',
    loadComponent: () => import('./capabilities').then(m => m.CapabilitiesComponent),
    title: 'Capabilities — Forge Lens',
  },
];
