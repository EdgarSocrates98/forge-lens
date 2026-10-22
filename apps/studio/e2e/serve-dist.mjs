#!/usr/bin/env node
/**
 * E2E static server for the Studio production build — zero deps.
 *
 * Serves `dist/studio/browser` with an SPA fallback to index.html, and
 * proxies `/v1/*` to the Forge Lens API (`LENS_API_ORIGIN`, default
 * http://127.0.0.1:7778 — the port the playwright webServer binds).
 *
 * Why this exists instead of `ng serve`: the Angular dev server watches
 * the whole node_modules tree and exhausts the host's inotify limit
 * (ENOSPC) in this environment; a static build + this server is
 * deterministic and watcher-free. The `/v1` proxy also keeps all API
 * calls same-origin, so no CORS is needed — the spec fixture seeds
 * `localStorage['lens-api-base'] = '/v1'`.
 *
 * Usage: node e2e/serve-dist.mjs [port]
 */
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(here, '../dist/studio/browser');
const PORT = Number(process.env.E2E_PORT || process.argv[2] || 4230);
const API_ORIGIN = process.env.LENS_API_ORIGIN || 'http://127.0.0.1:7778';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.ico': 'image/x-icon',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
  '.map': 'application/json',
};

/** Forward one /v1 request to the API and mirror status/headers/body. */
function proxy(req, res, apiUrl) {
  const upstream = httpRequest(
    apiUrl,
    { method: req.method, headers: { ...req.headers, host: new URL(API_ORIGIN).host } },
    (up) => {
      const headers = { ...up.headers };
      delete headers['transfer-encoding'];
      delete headers['connection'];
      res.writeHead(up.statusCode ?? 502, headers);
      up.pipe(res);
    },
  );
  upstream.on('error', (err) => {
    res.writeHead(502, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ detail: `e2e proxy: API unreachable (${err.code ?? err.message})` }));
  });
  req.pipe(upstream);
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://${req.headers.host ?? 'localhost'}`);
  if (url.pathname.startsWith('/v1')) {
    proxy(req, res, new URL(url.pathname + url.search, API_ORIGIN));
    return;
  }
  // static file, else SPA fallback to index.html (Angular routes)
  const rel = decodeURIComponent(url.pathname).replace(/^\/+/, '');
  let file = path.resolve(ROOT, rel || 'index.html');
  // prefix check alone would let a sibling dir "browser-evil" slip past
  if (file !== ROOT && !file.startsWith(ROOT + path.sep)) {
    file = path.join(ROOT, 'index.html');
  }
  try {
    const body = await readFile(file);
    res.writeHead(200, {
      'content-type': MIME[path.extname(file)] ?? 'application/octet-stream',
    });
    res.end(body);
  } catch {
    try {
      const body = await readFile(path.join(ROOT, 'index.html'));
      res.writeHead(200, { 'content-type': MIME['.html'] });
      res.end(body);
    } catch {
      res.writeHead(500, { 'content-type': 'text/plain' });
      res.end('dist/studio/browser/index.html missing — run `npm run build` first');
    }
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`studio-e2e: serving ${ROOT} on http://127.0.0.1:${PORT} — /v1 → ${API_ORIGIN}`);
});
