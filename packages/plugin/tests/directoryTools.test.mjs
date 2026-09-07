/**
 * Tests for DirectoryTools — the bot-facing `directory_lookup` read tool
 * (user-directory Phase 3a).
 *
 * The tool POSTs {ref} to /api/directory/lookup over the admin-daemon unix socket; the
 * SERVER binds the bot from the peer uid and returns {person} (sanitized) or
 * {person:null}. These tests stub the transport (the factory's optional `transport`) and
 * assert: the request shape (no botId in the body — identity is peer-uid), a resolved
 * person is returned verbatim, a miss surfaces as a clean "no match", and every failure
 * mode (HTTP error, daemon unavailable, empty ref) returns a NON-throwing tool envelope.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/directoryTools.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { createDirectoryLookupToolFactory } from "../dist/tools/DirectoryTools.js";
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
    return { status: 200, body: { person: null } };
  };
  cap.calls = calls;
  cap.queue = (resp) => queued.push(resp);
  return cap;
}


function buildTool(opts = {}) {
  const logger = opts.logger ?? fakeLogger();
  const transport = opts.transport ?? captureTransport();
  const factory = createDirectoryLookupToolFactory(
    { sharedDir: "/tmp/shared", botId: opts.botId ?? "atlas", transport },
    logger,
  );
  return { tool: factory({}), logger, transport };
}


const PERSON = {
  person_id: "pers_0000000000000001",
  names: { display: "Dana Lopez" },
  identities: [{ platform: "telegram", id: "70000001", handle: "@dana", source: "channel-captured" }],
  emails: [{ addr: "dana@example.net", rank: "primary", provenance: "bot-asserted", verified: false }],
  contact: {},
  membership: { admitted: true, role: "participant", capabilities: [], channels: ["telegram"] },
};


test("resolves a person and returns it verbatim", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { person: PERSON } });
  const res = await tool.execute("call-1", { ref: "@dana" });
  assert.equal(res.isError, undefined);
  const payload = JSON.parse(res.content[0].text);
  assert.equal(payload.person_id, "pers_0000000000000001");
  assert.equal(payload.names.display, "Dana Lopez");
});


test("request carries the ref and NO botId (identity is peer-uid bound)", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { person: PERSON } });
  await tool.execute("call-1", { ref: "  @dana  " });
  const req = transport.calls[0];
  assert.equal(req.method, "POST");
  assert.equal(req.path, "/api/directory/lookup");
  assert.deepEqual(req.body, { ref: "@dana" });          // trimmed
  assert.ok(!("bot" in req.body) && !("botId" in req.body));  // never sent
});


test("a miss (person:null) is a clean 'no match', not an error", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { person: null } });
  const res = await tool.execute("call-1", { ref: "@nobody" });
  assert.equal(res.isError, undefined);
  assert.match(res.content[0].text, /No one in this bot's directory matches/);
});


test("empty ref is rejected locally without a socket call", async () => {
  const { tool, transport } = buildTool();
  const res = await tool.execute("call-1", { ref: "   " });
  assert.equal(res.isError, true);
  assert.equal(transport.calls.length, 0);  // never hit the daemon
});


test("HTTP error surfaces a non-throwing error envelope", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 400, body: { error: "ref is required" } });
  const res = await tool.execute("call-1", { ref: "x" });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /ref is required/);
});


test("daemon-unavailable surfaces a friendly envelope (never throws)", async () => {
  const transport = async () => { throw new AdminSocketUnavailable("ENOENT"); };
  const { tool } = buildTool({ transport });
  const res = await tool.execute("call-1", { ref: "@dana" });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /unavailable/i);
});


test("an unexpected throw is caught and surfaced (never propagates to the agent loop)", async () => {
  const transport = async () => { throw new Error("boom"); };
  const { tool } = buildTool({ transport });
  const res = await tool.execute("call-1", { ref: "@dana" });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /boom/);
});


test("tool metadata: name + a param schema with a ref field", () => {
  const { tool } = buildTool();
  assert.equal(tool.name, "directory_lookup");
  assert.ok(tool.parameters.properties.ref);
});
