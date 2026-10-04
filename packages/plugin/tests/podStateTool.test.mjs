/**
 * Tests for the consolidated pod_state tool (overhead-budget B2 v2).
 *
 * Pins:
 *   1. Every query routes to the endpoint the eight legacy tools used, with
 *      the right params forwarded (and ONLY those params).
 *   2. query=turns / query=bot refuse without `bot` (clean tool error, no
 *      socket call).
 *   3. Unknown query → clean tool error listing valid queries.
 *
 * The schema-size regression guard that used to live here (pin 4 on the B2
 * win: < 3,000 chars vs the legacy ~6,460) generalized into
 * toolSchemaBudget.test.mjs (overhead-budget D1) — every factory is measured
 * there, pod_state included.
 *
 * Uses a real HTTP server on a unix socket so the adminSocketRequest path is
 * exercised end to end.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/podStateTool.test.mjs
 */
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as http from "node:http";
import * as os from "node:os";
import * as path from "node:path";

import { createPodStateToolFactory, PodStateParamsSchema } from "../dist/tools/PodStateTools.js";

const quiet = { warn: () => {}, info: () => {}, debug: () => {}, error: () => {} };

let server;
let socketPath;
const seenPaths = [];

before(async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-ps-"));
  socketPath = path.join(dir, "admin.sock");
  server = http.createServer((req, res) => {
    seenPaths.push(req.url);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
});

after(() => server?.close());

function tool() {
  return createPodStateToolFactory(quiet, socketPath)({});
}

async function run(params) {
  seenPaths.length = 0;
  const result = await tool().execute("tc-1", params);
  return { result, url: seenPaths[0] };
}

test("every query hits its legacy endpoint with only its params", async () => {
  const cases = [
    [{ query: "status" }, "/api/primary/state/pod_status"],
    [{ query: "signals", state: "all", producer: "cost_alert", bot: "b1", limit: 5 },
      "/api/primary/state/signals?state=all&producer=cost_alert&bot=b1&limit=5"],
    [{ query: "proposals", state: "pending", limit: 3 },
      "/api/primary/state/proposals?state=pending&limit=3"],
    [{ query: "watchdog", hours: 48, bot: "b1", limit: 7 },
      "/api/primary/state/watchdog?hours=48&bot=b1&limit=7"],
    [{ query: "spend", window: "1d", bot: "b1" },
      "/api/primary/state/spend?window=1d&bot=b1"],
    [{ query: "turns", bot: "b1", limit: 9 },
      "/api/primary/state/turns?bot=b1&limit=9"],
    [{ query: "bot", bot: "b1" }, "/api/primary/state/describe?bot=b1"],
    [{ query: "audits", bot: "b1", limit: 2 },
      "/api/primary/state/audits?bot=b1&limit=2"],
  ];
  for (const [params, expected] of cases) {
    const { result, url } = await run(params);
    assert.equal(url, expected, JSON.stringify(params));
    assert.equal(result.isError, undefined, `no error for ${params.query}`);
  }
});

test("cross-query params are NOT forwarded to endpoints that ignore them", async () => {
  // hours/producer are watchdog/signals-only; proposals must not see them.
  const { url } = await run({ query: "proposals", hours: 48, producer: "x" });
  assert.equal(url, "/api/primary/state/proposals");
});

test("turns and bot require a bot id — refused before any socket call", async () => {
  for (const query of ["turns", "bot"]) {
    const { result, url } = await run({ query });
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /requires 'bot'/);
    assert.equal(url, undefined, "no request should have been made");
  }
});

test("unknown query is a clean tool error naming the valid set", async () => {
  const { result, url } = await run({ query: "everything" });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /status.*signals.*audits/s);
  assert.equal(url, undefined);
});

test("params schema stays exported for the budget test", () => {
  assert.ok(PodStateParamsSchema);
});
