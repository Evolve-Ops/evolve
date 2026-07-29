/**
 * Tests for ExpandAppTool — the bot-facing `expand_app` Tier-2 disclosure tool
 * (app capability index; spec-app-invocation-just-works §2.1).
 *
 * The tool POSTs {app_id} to /api/applications/expand over the admin-daemon unix
 * socket; the SERVER binds the bot from the peer uid and returns {ok, app_id, detail}
 * or a 404 with the ids that WOULD resolve. These tests stub the transport and assert:
 * the request shape (no botId in the body — identity is peer-uid), the Tier-2 detail is
 * returned verbatim, a 404 miss surfaces recovery ids (not a crash), and every failure
 * mode (HTTP error, daemon unavailable, empty arg) returns a NON-throwing tool envelope.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/expandAppTool.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { createExpandAppToolFactory } from "../dist/tools/ExpandAppTool.js";
import { AdminSocketUnavailable } from "../dist/util/adminSocket.js";


function fakeLogger() {
  const records = { debug: [], info: [], warn: [], error: [] };
  return {
    debug: (m) => records.debug.push(m),
    info: (m) => records.info.push(m),
    warn: (m) => records.warn.push(m),
    error: (m) => records.error.push(m),
    records,
  };
}


function captureTransport() {
  const calls = [];
  const queued = [];
  const cap = async (req) => {
    calls.push(req);
    if (queued.length) {
      const next = queued.shift();
      if (typeof next === "function") return next(req);
      return next;
    }
    return { status: 200, body: { ok: true, app_id: "x", detail: "## X\n" } };
  };
  cap.calls = calls;
  cap.queue = (resp) => queued.push(resp);
  return cap;
}


function buildTool(opts = {}) {
  const logger = opts.logger ?? fakeLogger();
  const transport = opts.transport ?? captureTransport();
  const factory = createExpandAppToolFactory(
    { sharedDir: "/tmp/shared", botId: opts.botId ?? "atlas", transport },
    logger,
  );
  return { tool: factory({}), logger, transport };
}


const DETAIL = "## Task Manager — track your to-dos\n\n**How to use.** Run `tasks.py add`.\n";


test("returns the Tier-2 detail verbatim", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { ok: true, app_id: "task_manager", detail: DETAIL } });
  const res = await tool.execute("call-1", { app_id: "task_manager" });
  assert.equal(res.isError, undefined);
  assert.equal(res.content[0].text, DETAIL);
});


test("request carries app_id and NO botId (identity is peer-uid bound)", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { ok: true, app_id: "task_manager", detail: DETAIL } });
  await tool.execute("call-1", { app_id: "  task_manager  " });
  const req = transport.calls[0];
  assert.equal(req.method, "POST");
  assert.equal(req.path, "/api/applications/expand");
  assert.deepEqual(req.body, { app_id: "task_manager" });          // trimmed
  assert.ok(!("bot" in req.body) && !("botId" in req.body));       // never sent
});


test("a 404 miss lists the ids that would resolve (recoverable, not an error crash)", async () => {
  const { tool, transport } = buildTool();
  transport.queue({
    status: 404,
    body: { ok: false, error: "no installed app matches 'nope'", available: ["task_manager", "journal"] },
  });
  const res = await tool.execute("call-1", { app_id: "nope" });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /No installed app matches/);
  assert.match(res.content[0].text, /task_manager, journal/);
});


test("empty app_id is rejected locally without a socket call", async () => {
  const { tool, transport } = buildTool();
  const res = await tool.execute("call-1", { app_id: "   " });
  assert.equal(res.isError, true);
  assert.equal(transport.calls.length, 0);  // never hit the daemon
});


test("a 200 ok with empty detail degrades to a plain note, not an error", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { ok: true, app_id: "bare", detail: "" } });
  const res = await tool.execute("call-1", { app_id: "bare" });
  assert.equal(res.isError, undefined);
  assert.match(res.content[0].text, /No usage details/);
});


test("HTTP error surfaces a non-throwing error envelope", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 400, body: { error: "app_id is required" } });
  const res = await tool.execute("call-1", { app_id: "x" });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /app_id is required/);
});


test("daemon-unavailable surfaces a friendly envelope (never throws)", async () => {
  const transport = async () => { throw new AdminSocketUnavailable("ENOENT"); };
  const { tool } = buildTool({ transport });
  const res = await tool.execute("call-1", { app_id: "task_manager" });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /unavailable/i);
});


test("an unexpected throw is caught and surfaced (never propagates to the agent loop)", async () => {
  const transport = async () => { throw new Error("boom"); };
  const { tool } = buildTool({ transport });
  const res = await tool.execute("call-1", { app_id: "task_manager" });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /boom/);
});


test("tool metadata: name + a param schema with an app_id field", () => {
  const { tool } = buildTool();
  assert.equal(tool.name, "expand_app");
  assert.ok(tool.parameters.properties.app_id);
});
