/**
 * Tests for the `directory_upsert` WRITE tool (user-directory Phase 3b).
 *
 * The tool POSTs the provided directory-owned fields to /api/directory/upsert over the
 * admin-daemon unix socket; the SERVER binds the bot from the peer uid, stamps the write
 * bot-asserted, and returns {ok, created, person}. These tests stub the transport and
 * assert: the request shape (no botId in the body — identity is peer-uid; only provided
 * fields are sent), a created/updated record is summarised, and every failure mode (HTTP
 * error, daemon unavailable, empty ref) returns a NON-throwing tool envelope — a write
 * fault must never crash the gateway turn.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/directoryUpsert.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { createDirectoryUpsertToolFactory } from "../dist/tools/DirectoryTools.js";
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
    return { status: 200, body: { ok: true, created: false, person: null } };
  };
  cap.calls = calls;
  cap.queue = (resp) => queued.push(resp);
  return cap;
}


function buildTool(opts = {}) {
  const logger = opts.logger ?? fakeLogger();
  const transport = opts.transport ?? captureTransport();
  const factory = createDirectoryUpsertToolFactory(
    { sharedDir: "/tmp/shared", botId: opts.botId ?? "atlas", transport },
    logger,
  );
  return { tool: factory({}), logger, transport };
}


const STORED = {
  person_id: "pers_0000000000000001",
  names: { display: "Dana Lopez" },
  identities: [{ platform: "telegram", id: "70000001", source: "bot-asserted" }],
  emails: [{ addr: "dana@example.net", rank: "primary", provenance: "bot-asserted", verified: false }],
  contact: {},
  membership: null,
};


test("created record is summarised with the stored person", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { ok: true, created: true, person: STORED } });
  const res = await tool.execute("c1", {
    ref: "dana@example.net",
    emails: [{ addr: "dana@example.net", rank: "primary" }],
    names: { display: "Dana Lopez" },
  });
  assert.equal(res.isError, undefined);
  assert.match(res.content[0].text, /Created a new contact/);
  assert.match(res.content[0].text, /pers_0000000000000001/);
});


test("updated record says 'Updated'", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { ok: true, created: false, person: STORED } });
  const res = await tool.execute("c1", { ref: "Dana Lopez", contact: { org: "Acme" } });
  assert.equal(res.isError, undefined);
  assert.match(res.content[0].text, /Updated the directory entry/);
});


test("a 200 with person:null (e.g. blocked no-op) is a clean success, not an error", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { ok: true, created: false, person: null } });
  const res = await tool.execute("c1", { ref: "someone", contact: { org: "X" } });
  assert.equal(res.isError, undefined);
  assert.match(res.content[0].text, /Directory updated/);
});


test("request carries ONLY provided fields + ref, and NO botId", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { ok: true, created: true, person: STORED } });
  await tool.execute("c1", {
    ref: "  dana@example.net  ",
    emails: [{ addr: "dana@example.net", rank: "primary" }],
  });
  const req = transport.calls[0];
  assert.equal(req.method, "POST");
  assert.equal(req.path, "/api/directory/upsert");
  assert.equal(req.body.ref, "dana@example.net");           // trimmed
  assert.ok("emails" in req.body);
  assert.ok(!("contact" in req.body));                      // not provided → not sent
  assert.ok(!("identities" in req.body) && !("names" in req.body));
  assert.ok(!("bot" in req.body) && !("botId" in req.body));  // identity is peer-uid
});


test("empty ref is rejected locally without a socket call", async () => {
  const { tool, transport } = buildTool();
  const res = await tool.execute("c1", { ref: "   ", contact: { org: "X" } });
  assert.equal(res.isError, true);
  assert.equal(transport.calls.length, 0);  // never hit the daemon
});


test("a server rejection (e.g. authority key, 400) surfaces a non-throwing error envelope", async () => {
  const { tool, transport } = buildTool();
  transport.queue({
    status: 400,
    body: { error: "directory_upsert cannot set ['role']; it records identity and contact only" },
  });
  const res = await tool.execute("c1", { ref: "dana@example.net" });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /cannot set/);
});


test("a 422 (uncreatable name-only ref) surfaces a clean error, never throws", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 422, body: { error: "no one matches that reference" } });
  const res = await tool.execute("c1", { ref: "Just A Name", contact: { org: "X" } });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /no one matches/);
});


test("daemon-unavailable surfaces a friendly envelope (never throws)", async () => {
  const transport = async () => { throw new AdminSocketUnavailable("ENOENT"); };
  const { tool } = buildTool({ transport });
  const res = await tool.execute("c1", { ref: "dana@example.net" });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /unavailable/i);
});


test("an unexpected throw is caught (never propagates to the agent loop)", async () => {
  const transport = async () => { throw new Error("boom"); };
  const { tool } = buildTool({ transport });
  const res = await tool.execute("c1", { ref: "dana@example.net" });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /boom/);
});


test("tool metadata: name + a param schema with ref/emails fields", () => {
  const { tool } = buildTool();
  assert.equal(tool.name, "directory_upsert");
  assert.ok(tool.parameters.properties.ref);
  assert.ok(tool.parameters.properties.emails);
  // The description steers writes away from USER.md and disclaims authority.
  assert.match(tool.description, /USER\.md/);
  assert.match(tool.description, /never grant roles|never grant|membership/i);
});
