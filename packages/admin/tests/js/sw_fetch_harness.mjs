// Service-worker behaviour harness — deploy-safety proof.
//
// Loads the REAL packages/admin/evolve_admin/web/sw.js into a mock
// ServiceWorkerGlobalScope and exercises the fetch / activate / install
// handlers against the exact conditions a fleet promote creates. No
// browser, no network — deterministic, runs under plain `node`.
//
// What it proves (PR "[META:ui] PWA service-worker deploy-safety"):
//
//   1. FAIL-OPEN. A navigation with the network down and nothing cached
//      never resolves `respondWith` with `undefined`/`Response.error()`
//      (the value that throws "FetchEvent.respondWith received an error:
//      TypeError: Load failed" and blanks the admin shell). It rejects
//      instead → the browser's native, reload-recoverable error page.
//      The harness also demonstrates the OLD fallback (`caches.match('/')`
//      on an empty cache) would have handed `undefined` to respondWith.
//   2. NETWORK-FIRST ONLINE. A deploy's fresh shell always wins over a
//      stale cached one when the network is reachable, and the cache is
//      refreshed so the offline fallback is fresh too.
//   3. COHERENT OFFLINE SHELL. A navigation offline with a cached shell
//      serves that shell (no blank).
//   4. FLUSH-ALL ON ACTIVATE. activate() deletes every cache that isn't
//      this build's — so a deploy can't serve a prior build's assets.
//   5. SELF-HEAL. install() calls skipWaiting() so a fixed SW takes over
//      on the next reload without "clear site data".
//
// Exit 0 = all invariants hold; non-zero with a message = a failure.
//
// Fidelity caveats (what a green run does NOT prove): the mock Cache
// Storage matches on normalised path+search with no `ignoreSearch`/Vary
// handling, so query-bearing navigations aren't exercised (the SPA
// navigates to bare paths); and MockResponse.clone() doesn't model body
// consumption, so the double-clone-before-return in sw.js is verified by
// inspection, not here. The scenarios cover the deploy-safety invariants,
// not full Cache-Storage/FetchEvent semantics.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SW_PATH = join(__dirname, '..', '..', 'evolve_admin', 'web', 'sw.js');

const ORIGIN = 'https://pod';

// ── Assertion plumbing ──────────────────────────────────────────────────
let failures = 0;
function check(label, cond) {
  if (cond) {
    console.log(`  ok   ${label}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${label}`);
  }
}

// ── Mock caches API ─────────────────────────────────────────────────────
// Keys normalise to same-origin path so a navigation to ``https://pod/``
// and a literal ``'/'`` collide on ``/`` — matching real Cache-Storage
// URL normalisation closely enough for these scenarios.
function keyOf(reqOrStr) {
  const u = typeof reqOrStr === 'string' ? reqOrStr : reqOrStr.url;
  if (u.startsWith('/')) return u.split('#')[0];
  try {
    const parsed = new URL(u);
    return (parsed.pathname + parsed.search) || '/';
  } catch {
    return u;
  }
}

function makeCaches(initial = {}) {
  const store = new Map(); // name -> Map(key -> Response)
  for (const [name, entries] of Object.entries(initial)) {
    const m = new Map();
    for (const [k, v] of Object.entries(entries)) m.set(keyOf(k), v);
    store.set(name, m);
  }
  const facade = (name) => ({
    put: (req, resp) => { store.get(name).set(keyOf(req), resp); return Promise.resolve(); },
    match: (req) => Promise.resolve(store.get(name).get(keyOf(req))),
  });
  return {
    _store: store,
    open: (name) => { if (!store.has(name)) store.set(name, new Map()); return Promise.resolve(facade(name)); },
    match: (req) => {
      for (const m of store.values()) {
        const hit = m.get(keyOf(req));
        if (hit) return Promise.resolve(hit);
      }
      return Promise.resolve(undefined);
    },
    keys: () => Promise.resolve([...store.keys()]),
    delete: (name) => Promise.resolve(store.delete(name)),
  };
}

// ── Mock Response ───────────────────────────────────────────────────────
class MockResponse {
  constructor(tag, { ok = true, type = 'basic' } = {}) {
    this.tag = tag; this.ok = ok; this.type = type;
  }
  clone() { return new MockResponse(this.tag, { ok: this.ok, type: this.type }); }
  static error() { return new MockResponse('__error__', { ok: false, type: 'error' }); }
}

function navRequest(url = `${ORIGIN}/`) { return { url, method: 'GET', mode: 'navigate' }; }
function assetRequest(url = `${ORIGIN}/static/js/pages/apps.js`) {
  return { url, method: 'GET', mode: 'cors' };
}

// ── Load sw.js into a fresh mock global per scenario ────────────────────
function loadSW({ caches, fetchImpl }) {
  let src = readFileSync(SW_PATH, 'utf8');
  // Mimic the server's per-build substitution.
  src = src.replace("'__PWA_CACHE_VERSION__'", "'evolve-pwa-current'");

  const handlers = {};
  const self = {
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting() { this._skipWaiting = true; },
    location: { origin: ORIGIN },
    clients: { claim: () => Promise.resolve(), matchAll: () => Promise.resolve([]) },
    registration: { showNotification: () => Promise.resolve() },
    _skipWaiting: false,
  };
  const sandbox = {
    self, caches, Response: MockResponse, URL, console,
    Promise, setTimeout, fetch: fetchImpl,
  };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: 'sw.js' });
  return { handlers, self };
}

// Dispatch a fetch event and resolve to {value} on fulfil or {error} on reject.
function dispatchFetch(handler, request) {
  let captured;
  const event = {
    request,
    respondWith(p) { captured = p; },
    waitUntil() {},
  };
  handler(event);
  if (captured === undefined) return Promise.resolve({ noRespondWith: true });
  return captured.then((value) => ({ value }), (error) => ({ error }));
}

// ════════════════════════════════════════════════════════════════════════
async function main() {
  // ── Scenario 1: navigate, network DOWN, cache EMPTY (the lockout case) ──
  {
    const caches = makeCaches({ 'evolve-pwa-current': {} });
    const { handlers } = loadSW({
      caches,
      fetchImpl: () => Promise.reject(new TypeError('Load failed')),
    });
    const out = await dispatchFetch(handlers.fetch, navRequest());
    check('S1 fail-open: respondWith never resolves to undefined',
      !('value' in out) || out.value !== undefined);
    check('S1 fail-open: rejects (native error) rather than blanking',
      'error' in out);
    // Demonstrate the OLD fallback would have handed undefined to respondWith.
    const oldFallback = await caches.match('/');
    check('S1 contrast: OLD `caches.match("/")` on empty cache === undefined',
      oldFallback === undefined);
  }

  // ── Scenario 2: navigate ONLINE — fresh network shell beats stale cache ─
  {
    const caches = makeCaches({ 'evolve-pwa-current': { '/': new MockResponse('stale-shell') } });
    const { handlers } = loadSW({
      caches,
      fetchImpl: () => Promise.resolve(new MockResponse('fresh-shell')),
    });
    const out = await dispatchFetch(handlers.fetch, navRequest());
    check('S2 network-first: serves FRESH shell online',
      out.value && out.value.tag === 'fresh-shell');
    // Cache refreshed so the offline fallback is also fresh.
    await new Promise((r) => setTimeout(r, 0));
    const cachedShell = await caches.match('/');
    check('S2 cache refreshed: `/` now holds the fresh shell',
      cachedShell && cachedShell.tag === 'fresh-shell');
  }

  // ── Scenario 3: navigate OFFLINE with a cached shell → serve it ─────────
  {
    const caches = makeCaches({ 'evolve-pwa-current': { '/': new MockResponse('cached-shell') } });
    const { handlers } = loadSW({
      caches,
      fetchImpl: () => Promise.reject(new TypeError('Load failed')),
    });
    const out = await dispatchFetch(handlers.fetch, navRequest(`${ORIGIN}/dashboard`));
    check('S3 coherent offline shell: serves cached `/` shell, no blank',
      out.value && out.value.tag === 'cached-shell');
  }

  // ── Scenario 4: activate flushes EVERY non-current cache ────────────────
  {
    const caches = makeCaches({
      'evolve-pwa-old1': { '/': new MockResponse('old1') },
      'evolve-pwa-old2': { '/': new MockResponse('old2') },
      'evolve-pwa-current': { '/': new MockResponse('cur') },
    });
    const { handlers } = loadSW({ caches, fetchImpl: () => Promise.reject(new Error('n/a')) });
    let waited;
    handlers.activate({ waitUntil(p) { waited = p; } });
    await waited;
    const remaining = await caches.keys();
    check('S4 flush-all: only this build\'s cache survives activate',
      remaining.length === 1 && remaining[0] === 'evolve-pwa-current');
  }

  // ── Scenario 5: install calls skipWaiting (self-heal) ───────────────────
  {
    const caches = makeCaches({});
    const { handlers, self } = loadSW({
      caches,
      fetchImpl: () => Promise.resolve(new MockResponse('asset')),
    });
    handlers.install({ waitUntil() {} });
    check('S5 self-heal: install() calls skipWaiting()', self._skipWaiting === true);
  }

  // ── Scenario 6: asset miss OFFLINE → passthrough, not a synthetic blank ──
  {
    const caches = makeCaches({ 'evolve-pwa-current': {} });
    const { handlers } = loadSW({
      caches,
      fetchImpl: () => Promise.reject(new TypeError('Load failed')),
    });
    const out = await dispatchFetch(handlers.fetch, assetRequest());
    check('S6 asset fail-open: never resolves to undefined / Response.error',
      !('value' in out) || (out.value !== undefined && out.value.type !== 'error'));
  }

  console.log('');
  if (failures) {
    console.log(`SW harness: ${failures} invariant(s) FAILED`);
    process.exit(1);
  }
  console.log('SW harness: all deploy-safety invariants hold');
}

main().catch((e) => { console.error('harness crashed:', e); process.exit(2); });
