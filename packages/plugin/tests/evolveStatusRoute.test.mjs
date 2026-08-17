/**
 * Tests for the /evolve/status liveness route.
 *
 * Regression test for the consumer-discovery gap that caused PR #1709
 * to silently break the admin Maintenance "Evolve Plugin" probe (8/8
 * bots showing plugin_loaded=warned on the 2026-05-29 deploy):
 *
 *   - admin/health.py:_probe_evolve_plugin_loaded fetches /evolve/status
 *     and looks for one of {bot_id, plugin_version, status} in the
 *     JSON body to confirm the plugin handler ran.
 *
 * If a future audit thinks /evolve/status is dead again, deleting it
 * must trip this test. The probe is in admin Python so we can't import
 * its checker directly, but we pin the contract here: the response must
 * be JSON and must include one of those three keys.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/evolveStatusRoute.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { registerApiRoutes } from "../dist/api/routes.js";

const CONFIG = {
  botId: "team_bot_a",
  role: "member",
  networkId: "test-net",
  sharedDir: "/tmp/fake-shared",
};

/** Minimal stand-in for the gateway's `api` object. Records routes
 *  and lets tests synthesise requests against them. */
function makeFakeApi() {
  const routes = [];
  return {
    routes,
    registerHttpRoute: (route) => routes.push(route),
    find: (method, path) =>
      routes.find((r) => r.method === method && r.path === path),
  };
}

function makeFakeRes() {
  const headers = {};
  const r = {
    statusCode: 0,
    setHeader: (k, v) => { headers[k] = v; },
    end: (body) => { r.body = body; },
    body: undefined,
    headers,
  };
  return r;
}

test("/evolve/status is registered", () => {
  const api = makeFakeApi();
  registerApiRoutes(api, CONFIG);
  const route = api.find("GET", "/evolve/status");
  assert.ok(route, "/evolve/status must be registered — admin health.py probe depends on it");
});

test("/evolve/status responds with JSON containing keys the probe consumes", async () => {
  const api = makeFakeApi();
  registerApiRoutes(api, CONFIG);
  const route = api.find("GET", "/evolve/status");

  const res = makeFakeRes();
  await route.handler({}, res);

  assert.equal(res.statusCode, 200);
  assert.equal(res.headers["Content-Type"], "application/json");

  const body = JSON.parse(res.body);
  // The admin probe (health.py:_probe_evolve_plugin_loaded) checks for
  // ANY of these three keys — losing all of them silently breaks the
  // pod-wide Maintenance "Evolve Plugin" panel.
  const probeKeys = ["bot_id", "plugin_version", "status"];
  const matched = probeKeys.filter((k) => k in body);
  assert.ok(
    matched.length > 0,
    `body must include at least one of ${JSON.stringify(probeKeys)} for admin probe — got ${JSON.stringify(body)}`,
  );

  // Stronger assertion: this minimal beacon should include all three.
  assert.equal(body.bot_id, CONFIG.botId);
  assert.equal(typeof body.plugin_version, "string");
  assert.equal(body.status, "loaded");
});

test("/evolve/status does not perform file I/O (audit guard)", async () => {
  // Pre-#1709 the handler read {shared}/metrics/{botId}-{today}.json from
  // disk. The metrics layout was wrong (different from what measure.py
  // writes) and every call returned status: "no-data". Re-introducing
  // file I/O risks the same drift — the restored route stays a pure
  // in-memory beacon. We can't directly assert "doesn't open files" in a
  // node test, but we can assert it succeeds with a sharedDir that
  // doesn't exist: file I/O against a missing path would throw or
  // return "no-data" instead of "loaded".
  const api = makeFakeApi();
  registerApiRoutes(api, {
    ...CONFIG,
    sharedDir: "/var/empty/definitely-does-not-exist",
  });
  const route = api.find("GET", "/evolve/status");

  const res = makeFakeRes();
  await route.handler({}, res);

  const body = JSON.parse(res.body);
  assert.equal(body.status, "loaded",
    "status must be 'loaded' regardless of sharedDir state — no file I/O");
});
