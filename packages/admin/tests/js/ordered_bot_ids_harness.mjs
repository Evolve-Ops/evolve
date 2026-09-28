// Node harness for the canonical bot-list helper (`orderedBotIds`) and the
// Maintenance › Status render that consumes it.
//
// Regression pinned here (2026-08-31, evolve-stable-616): Maintenance ›
// Status rendered "No bots configured." on a pod whose own header read 9/9
// bots online. `orderedBotIds` filtered every id through
// `isScaffoldOnlyBot`, a predicate that reads network.json CONFIG records
// ("no role AND no user AND no port ⇒ phantom"). The /api/gateway/status
// payload is STATUS-shaped — {gateway_running, gateway_reachable, ts,
// gateway_pid, source} — so every bot satisfied the predicate and the
// empty-state branch fired. Same family as the AI-Optimization bot-tabs
// regression.
//
// There is no JS unit runner in this package, so — like sw_fetch_harness.mjs
// — the real page sources are evaluated in a mock browser global scope and
// the invariants are asserted from here. Run directly (`node
// ordered_bot_ids_harness.mjs`) or via tests/test_ordered_bot_ids.py.

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const PAGES = resolve(HERE, '../../evolve_admin/web/static/js/pages');

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log(`ok   ${name}`);
  } else {
    failures++;
    console.log(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
  }
}
function eq(name, actual, expected) {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  check(name, a === e, `got ${a}, want ${e}`);
}

// ── Mock browser scope ──────────────────────────────────────────────────
// Minimal DOM: only the elements loadGateway touches, each recording the
// last innerHTML written so the render can be asserted as a string.
function makeElement(id) {
  return { id, innerHTML: '', textContent: '', checked: false, classList: { contains: () => false } };
}

function makeScope({ network = {}, status = null, apiRoutes = {} } = {}) {
  const els = {};
  const scope = {
    console,
    Date,
    JSON,
    Object,
    Array,
    Math,
    String,
    Number,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    document: {
      getElementById(id) { return (els[id] ||= makeElement(id)); },
      querySelector() { return null; },
      querySelectorAll() { return []; },
    },
    window: {},
    _els: els,
    // Globals the page modules read (declared in index.html at runtime).
    _networkData: network,
    _statusData: status,
    // Core helpers the page modules call.
    api: async (_method, path) => {
      if (!(path in apiRoutes)) throw new Error(`unstubbed API route: ${path}`);
      return apiRoutes[path];
    },
    escHtml: (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    )),
    ago: () => '1m ago',
    toast: () => {},
    confirmModal: async () => false,
    // Independent surfaces loadGateway fires and does not await.
    loadRelatedProposalsStrip: () => {},
    loadDeferRunnerHealth: () => {},
  };
  scope.globalThis = scope;
  vm.createContext(scope);
  // model-catalog.js carries buildErrNote(), which maintenance's gateway
  // row render calls; load the real one rather than stubbing it.
  for (const f of ['bot-detail.js', 'model-catalog.js', 'maintenance.js', 'backup.js']) {
    vm.runInContext(readFileSync(resolve(PAGES, f), 'utf8'), scope, { filename: f });
  }
  return scope;
}

// ── Fixtures ────────────────────────────────────────────────────────────
// Nine bots, primary first in canonical order: evo (primary), then the rest
// alphabetised by display label. Deliberately declared out of order so a
// pass-through of Object.keys() cannot accidentally satisfy the assertion.
const CONFIG_BOTS = {
  'team-bot-c': { role: 'member', user: 'team-bot-c', port: 8793, display_name: 'Cy' },
  'evo': { role: 'primary', user: 'evo', port: 8790, display_name: 'Evo' },
  'team-bot-a': { role: 'member', user: 'team-bot-a', port: 8791, display_name: 'Ada' },
  'team-bot-b': { role: 'member', user: 'team-bot-b', port: 8792, display_name: 'Bex' },
  'team-bot-d': { role: 'member', user: 'team-bot-d', port: 8794, display_name: 'Dee' },
  'team-bot-e': { role: 'member', user: 'team-bot-e', port: 8795, display_name: 'Eve' },
  'team-bot-f': { role: 'member', user: 'team-bot-f', port: 8796, display_name: 'Fay' },
  'team-bot-g': { role: 'member', user: 'team-bot-g', port: 8797, display_name: 'Gus' },
  'team-bot-h': { role: 'member', user: 'team-bot-h', port: 8798, display_name: 'Hal' },
};
const CANONICAL = [
  'evo', 'team-bot-a', 'team-bot-b', 'team-bot-c', 'team-bot-d',
  'team-bot-e', 'team-bot-f', 'team-bot-g', 'team-bot-h',
];

// The EVO-SEP-S4 residue: a `bots.evolve` entry carrying only a scaffold.
const PHANTOM = { setup_checklist: { items: [] } };

// What /api/gateway/status actually returns (server.py::api_gateway_status).
function gatewayStatusPayload(ids) {
  const out = {};
  for (const id of ids) {
    out[id] = {
      bot_id: id,
      gateway_running: true,
      gateway_reachable: true,
      stale: false,
      ts: '2026-08-31T12:00:00Z',
      ts_epoch: 1788000000,
      gateway_pid: id === 'evo' ? null : '4242',
      source: id === 'evo' ? 'self' : 'direct_probe',
    };
  }
  return out;
}

// ── (a) config-shaped map incl. a genuine scaffold phantom ──────────────
{
  const withPhantom = { ...CONFIG_BOTS, evolve: PHANTOM };
  // Resolved against network.json (the normal path — loadNetwork() has run).
  const s = makeScope({ network: { primary: 'evo', bots: withPhantom } });
  eq('(a) phantom filtered, real bots kept in canonical order (config via _networkData)',
    s.orderedBotIds(withPhantom), CANONICAL);

  // And with the boot fetch missing: the passed map is itself config-shaped,
  // so the fallback still recognises the phantom. EVO-SEP-S4 holds either way.
  const s2 = makeScope({ network: {} });
  eq('(a2) phantom filtered when _networkData is empty (caller map is config-shaped)',
    s2.orderedBotIds(withPhantom), CANONICAL);
}

// ── (b) status-shaped records — THE REGRESSION PIN ──────────────────────
{
  const payload = gatewayStatusPayload(CANONICAL);
  const s = makeScope({ network: { primary: 'evo', bots: CONFIG_BOTS } });
  eq('(b) status-shaped payload returns ALL bots in canonical order',
    s.orderedBotIds(payload), CANONICAL);

  // The same must hold with no network.json loaded at all: a status record's
  // missing role/user/port is not evidence of a phantom.
  const s2 = makeScope({ network: {} });
  eq('(b2) status-shaped payload survives an empty _networkData',
    s2.orderedBotIds(payload).slice().sort(), CANONICAL.slice().sort());

  // A genuine phantom present in BOTH network.json and the status payload is
  // still suppressed — the fix must not cost EVO-SEP-S4 on this surface.
  const s3 = makeScope({
    network: { primary: 'evo', bots: { ...CONFIG_BOTS, evolve: PHANTOM } },
  });
  eq('(b3) phantom in a status payload is still suppressed when config knows it',
    s3.orderedBotIds(gatewayStatusPayload([...CANONICAL, 'evolve'])), CANONICAL);
}

// ── (c) empty map ───────────────────────────────────────────────────────
{
  const s = makeScope({ network: { primary: 'evo', bots: CONFIG_BOTS } });
  eq('(c) {} → []', s.orderedBotIds({}), []);
  eq('(c2) null → []', s.orderedBotIds(null), []);
}

// ── Posture / synthetic shapes (the sibling callers) ────────────────────
{
  const s = makeScope({ network: { primary: 'evo', bots: CONFIG_BOTS } });
  // /api/autonomy/inventory: {bot_id: {integrations: [...]}}
  const autonomy = Object.fromEntries(CANONICAL.map((id) => [id, { integrations: [] }]));
  eq('posture: autonomy inventory keeps every bot', s.orderedBotIds(autonomy), CANONICAL);
  // /api/hooks-admin/inventory: cached dict, or null when no scan has run.
  const hooks = Object.fromEntries(CANONICAL.map((id, i) => [id, i % 2 ? null : { bot_id: id }]));
  eq('posture: hook inventory keeps scanned AND unscanned bots',
    s.orderedBotIds(hooks), CANONICAL);
  // cost-measures synthesizes a stub for a tile id status data doesn't carry.
  const synth = { ...CONFIG_BOTS, 'team-bot-z': { id: 'team-bot-z' } };
  eq('synthetic: a stub for an id outside the roster is kept, not culled',
    s.orderedBotIds(synth), [...CANONICAL, 'team-bot-z']);
}

// ── Backup › Cloud: the legacy `evolve` id the endpoint appends ─────────
// /api/backup/cloud/config returns `bots_cfg.keys() + ["evolve"]`, so a pod
// with a real primary id gets an `evolve` entry network.json never carried.
// orderedBotIds keeps unknown ids by design (rule 2), so the page drops it.
{
  const cloudCfg = Object.fromEntries(
    [...CANONICAL, 'evolve'].map((id) => [id, { backupRepoUrl: '', pubkey: null }])
  );
  const s = makeScope({ network: { primary: 'evo', bots: CONFIG_BOTS } });
  eq('backup: legacy `evolve` dropped when the roster has no bots.evolve',
    Object.keys(s._backupVisibleBots(cloudCfg)).sort(), CANONICAL.slice().sort());

  // A pre-#3053 pod whose roster DOES carry bots.evolve keeps its card.
  const legacy = makeScope({
    network: { primary: 'evolve', bots: { ...CONFIG_BOTS, evolve: { role: 'primary', user: 'evolve', port: 8790 } } },
  });
  eq('backup: legacy `evolve` kept on a pod whose roster carries it',
    Object.keys(legacy._backupVisibleBots(cloudCfg)).sort(),
    [...CANONICAL, 'evolve'].sort());

  // No roster loaded is not evidence a bot doesn't exist — keep everything.
  const noNet = makeScope({ network: {} });
  eq('backup: nothing dropped when the roster has not loaded',
    Object.keys(noNet._backupVisibleBots(cloudCfg)).sort(),
    [...CANONICAL, 'evolve'].sort());
}

// ── Maintenance › Status render ─────────────────────────────────────────
{
  const s = makeScope({
    network: { primary: 'evo', bots: CONFIG_BOTS },
    status: { primary: 'evo', bots: CONFIG_BOTS, pod_breakers: [] },
    apiRoutes: { '/api/gateway/status': gatewayStatusPayload(CANONICAL) },
  });
  await s.loadGateway();
  const html = s._els['gw-status-table'].innerHTML;
  check('render: never the empty state', !html.includes('No bots configured.'),
    'empty-state branch fired on a 9-bot gateway payload');
  // Count body rows by their Bot cell — <tr> alone would also catch the
  // <thead> row.
  const rows = (html.match(/data-label="Bot"/g) || []).length;
  check('render: 9 status records → 9 rows', rows === 9, `got ${rows} body rows`);
  for (const id of CANONICAL) {
    const label = CONFIG_BOTS[id].display_name;
    check(`render: row present for ${id} (${label})`, html.includes(`<strong>${label}</strong>`));
  }
  // Primary first: Evo's cell must precede every other bot's.
  const first = html.indexOf('<strong>Evo</strong>');
  const others = CANONICAL.filter((i) => i !== 'evo')
    .map((i) => html.indexOf(`<strong>${CONFIG_BOTS[i].display_name}</strong>`));
  check('render: primary bot renders first', others.every((p) => p > first));
}

// ── Gateway Logs render (config-shaped payload — unaffected, pinned) ────
{
  const s = makeScope({
    network: { primary: 'evo', bots: CONFIG_BOTS },
    apiRoutes: {
      '/api/network': { primary: 'evo', bots: CONFIG_BOTS },
      '/api/gateway/logs/evo': { log_path: '/tmp/evo.log', lines: ['hello'] },
    },
  });
  await s.loadGatewayLogs();
  const tabs = s._els['logs-bot-tabs'].innerHTML;
  const tabCount = (tabs.match(/class="logs-bot-tab/g) || []).length;
  check('logs: 9 bot tabs rendered', tabCount === 9, `got ${tabCount}`);
  check('logs: not the empty state',
    s._els['logs-panel'].textContent !== 'No bots configured.');
}

if (failures) {
  console.log(`\n${failures} invariant(s) failed`);
  process.exit(1);
}
console.log('\nall orderedBotIds invariants hold');
