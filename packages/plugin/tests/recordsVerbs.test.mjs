/**
 * Tests for the `records` tool — the six records-layer verbs (D-AD2, D-AD3),
 * run from the built `dist/` against a FIXTURE DAEMON: a real HTTP server on a
 * real unix socket, so the tool's own transport (adminSocketRequest) is what
 * carries every call.
 *
 * WHAT THESE PIN:
 *   * **Each verb's request** — POST, the path under /api/records-bot/<app>/,
 *     the body — and that NO bot id is ever sent (identity is the peer uid).
 *   * **The six verbs are on the contract**: registerAppTool accepts the tool
 *     with `append` registered under the `ledger.append` row; a verb with no
 *     row refuses the whole tool.
 *   * **Refusals pass through typed**: the daemon's `{error, code}` reaches the
 *     model as an error envelope naming the code, and says nothing changed.
 *   * **Fail closed**: daemon unreachable ⇒ one refusal, nothing written.
 *   * **Bounded list**: a 500-row response renders under the caps.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/recordsVerbs.test.mjs
 */
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as http from "node:http";
import * as os from "node:os";
import * as path from "node:path";

import {
  createRecordsToolFactory,
  renderRecordsList,
  DAEMON_UNREACHABLE_REFUSAL,
  RECORDS_CONTRACT_VERBS,
  MAX_LIST_ROWS,
  MAX_LIST_CHARS,
} from "../dist/tools/records.js";
import { registerAppTool, requireToolVerbRows } from "../dist/apps/contract/register.js";

const quiet = { warn: () => {}, info: () => {}, debug: () => {}, error: () => {} };

// ── the fixture daemon: an in-memory store with the real refusal shapes ────

let server;
let socketPath;
const seen = [];
const rows = new Map();   // key -> row   (table "games", key column "id")
const entries = [];       // ledger "events"
const ROLE = { value: "owner" };

function reply(res, status, body) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

function handle(url, body, res) {
  const m = url.match(/^\/api\/records-bot\/([^/]+)\/(list|get|put|delete|history|ledger\/append)$/);
  if (!m) return reply(res, 404, { error: "no route" });
  const [, app, verb] = m;
  if (app !== "collection-tracker") return reply(res, 404, { error: `no app ${app}`, code: "unknown_app" });
  if (verb !== "ledger/append" && body.table !== "games") {
    return reply(res, 400, { error: `no table ${body.table}`, code: "unknown_table" });
  }
  switch (verb) {
    case "list":
      return reply(res, 200, { total: rows.size, cap: 200, rows: [...rows.values()] });
    case "get": {
      const row = rows.get(String(body.key));
      if (!row) return reply(res, 404, { error: `no row ${body.key}`, code: "not_found" });
      const mine = entries.filter((e) => e.thing_id === row.id);
      const last = mine.at(-1);
      return reply(res, 200, { row, rollups: { current_status: last ? last.kind : null } });
    }
    case "put": {
      const extra = Object.keys(body.row).filter((k) => !["id", "title"].includes(k));
      if (extra.length) return reply(res, 400, { error: `undeclared ${extra}`, code: "undeclared_column" });
      const op = rows.has(body.row.id) ? "replace" : "insert";
      rows.set(body.row.id, body.row);
      return reply(res, 200, { op, row: body.row });
    }
    case "delete":
      if (ROLE.value !== "owner") {
        return reply(res, 403, { error: "role 'user' does not include records.delete", code: "forbidden" });
      }
      rows.delete(String(body.key));
      return reply(res, 200, { deleted: [body.key] });
    case "history":
      return reply(res, 200, {
        revisions: [{ at: "2026-09-30T10:00:00+00:00", op: "insert", by: "bot:b" }],
        entries: entries.filter((e) => e.thing_id === String(body.key)),
      });
    case "ledger/append": {
      const e = body.entry;
      if (!["acquired", "sold", "loaned", "returned"].includes(e.kind)) {
        return reply(res, 400, { error: `kind ${e.kind} not in the closed set`, code: "unknown_kind" });
      }
      entries.push(e);
      return reply(res, 200, { entry: { seq: entries.length, ...e } });
    }
  }
}

before(async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-rec-"));
  socketPath = path.join(dir, "admin.sock");
  server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => { raw += c; });
    req.on("end", () => {
      const body = raw ? JSON.parse(raw) : {};
      seen.push({ method: req.method, url: req.url, body });
      handle(req.url, body, res);
    });
  });
  await new Promise((resolve) => server.listen(socketPath, resolve));
});

after(() => server?.close());

function tool(sock = socketPath) {
  return createRecordsToolFactory({ sharedDir: "/nonexistent", botId: "bot-a", socketPath: sock }, quiet)({});
}

async function run(params, sock) {
  seen.length = 0;
  const result = await tool(sock).execute("tc-1", params);
  return { result, call: seen[0], text: result.content[0].text };
}

const APP = "collection-tracker";

// ── the six verbs, end to end through the socket ───────────────────────────

test("put → append → get derives status from the ledger → history → list → delete", async () => {
  let r = await run({ verb: "put", app: APP, table: "games", row: { id: "g1", title: "A board game" } });
  assert.equal(r.call.method, "POST");
  assert.equal(r.call.url, `/api/records-bot/${APP}/put`);
  assert.deepEqual(r.call.body, { table: "games", row: { id: "g1", title: "A board game" } });
  assert.match(r.text, /^put \(insert\): id=g1/);

  r = await run({ verb: "append", app: APP,
    entry: { thing_id: "g1", kind: "loaned", at: "2026-09-30", counterparty: "a friend" } });
  assert.equal(r.call.url, `/api/records-bot/${APP}/ledger/append`);
  assert.deepEqual(r.call.body, {
    entry: { thing_id: "g1", kind: "loaned", at: "2026-09-30", counterparty: "a friend" } });
  assert.match(r.text, /recorded: loaned for g1/);

  r = await run({ verb: "get", app: APP, table: "games", key: "g1" });
  assert.equal(r.call.url, `/api/records-bot/${APP}/get`);
  assert.deepEqual(r.call.body, { table: "games", key: "g1" });
  assert.match(r.text, /derived: current_status=loaned/);

  r = await run({ verb: "history", app: APP, table: "games", key: "g1" });
  assert.equal(r.call.url, `/api/records-bot/${APP}/history`);
  assert.match(r.text, /insert by bot:b/);
  assert.match(r.text, /loaned ↔ a friend/);

  r = await run({ verb: "list", app: APP, table: "games", filter: { title: "A board game" }, sort: "-id" });
  assert.equal(r.call.url, `/api/records-bot/${APP}/list`);
  assert.deepEqual(r.call.body, {
    table: "games", filter: { title: "A board game" }, sort: "-id", limit: MAX_LIST_ROWS });
  assert.match(r.text, /^1 row; showing 1\./);

  r = await run({ verb: "delete", app: APP, table: "games", key: "g1" });
  assert.equal(r.call.url, `/api/records-bot/${APP}/delete`);
  assert.equal(r.result.isError, undefined);
});

test("no call ever carries a bot id — identity is the socket peer uid", async () => {
  await run({ verb: "put", app: APP, table: "games", row: { id: "g2", title: "t" } });
  await run({ verb: "list", app: APP, table: "games" });
  for (const c of seen) {
    const blob = JSON.stringify(c);
    assert.ok(!blob.includes("bot-a"), `bot id leaked into ${blob}`);
    assert.ok(!("bot" in c.body) && !("bot_id" in c.body));
  }
});

test("instance is sent only when named (pod-scoped apps)", async () => {
  const r = await run({ verb: "list", app: APP, table: "games", instance: "household" });
  assert.equal(r.call.body.instance, "household");
});

// ── refusals ────────────────────────────────────────────────────────────────

test("a daemon refusal reaches the model typed, and says nothing changed", async () => {
  ROLE.value = "user";
  try {
    const r = await run({ verb: "delete", app: APP, table: "games", key: "g2" });
    assert.equal(r.result.isError, true);
    assert.match(r.text, /\[forbidden\]/);
    assert.match(r.text, /records\.delete/);
    assert.match(r.text, /Nothing was changed\./);
  } finally {
    ROLE.value = "owner";
  }
  for (const [params, code] of [
    [{ verb: "put", app: APP, table: "games", row: { id: "g3", colour: "red" } }, "undeclared_column"],
    [{ verb: "append", app: APP, entry: { thing_id: "g2", kind: "stolen", at: "2026-09-30" } }, "unknown_kind"],
    [{ verb: "list", app: APP, table: "nope" }, "unknown_table"],
    [{ verb: "list", app: "other-app", table: "games" }, "unknown_app"],
    [{ verb: "get", app: APP, table: "games", key: "missing" }, "not_found"],
  ]) {
    const r = await run(params);
    assert.equal(r.result.isError, true, JSON.stringify(params));
    assert.match(r.text, new RegExp(`\\[${code}\\]`));
  }
});

test("missing arguments refuse locally, with no daemon call", async () => {
  for (const params of [
    { verb: "list", app: "", table: "games" },
    { verb: "list", app: APP },
    { verb: "get", app: APP, table: "games" },
    { verb: "put", app: APP, table: "games" },
    { verb: "append", app: APP },
  ]) {
    const r = await run(params);
    assert.equal(r.result.isError, true, JSON.stringify(params));
    assert.equal(r.call, undefined, "no request should have been made");
  }
});

test("daemon unreachable ⇒ fail closed with the one refusal", async () => {
  const r = await run({ verb: "put", app: APP, table: "games", row: { id: "x" } },
    path.join(os.tmpdir(), "no-such-dir-evolve", "admin.sock"));
  assert.equal(r.result.isError, true);
  assert.equal(r.text, DAEMON_UNREACHABLE_REFUSAL);
});

test("a 500-row list renders under both caps and says what it left out", () => {
  const many = Array.from({ length: 500 }, (_, i) => ({ id: `g${i}`, title: "x".repeat(40) }));
  const text = renderRecordsList({ total: 500, rows: many });
  assert.ok(text.length <= MAX_LIST_CHARS + 200, `rendered ${text.length} chars`);
  assert.ok(text.split("\n").length <= MAX_LIST_ROWS + 2);
  assert.match(text, /more not shown/);
});

// ── the contract ────────────────────────────────────────────────────────────

test("the six verbs are on the contract; append registers as ledger.append", () => {
  assert.deepEqual([...RECORDS_CONTRACT_VERBS],
    ["list", "get", "put", "delete", "history", "ledger.append"]);
  requireToolVerbRows("records", RECORDS_CONTRACT_VERBS);  // must not throw
  const registered = [];
  const ok = registerAppTool({ registerTool: (f) => registered.push(f) }, "records",
    RECORDS_CONTRACT_VERBS, () => ({}), quiet);
  assert.equal(ok, true);
  assert.equal(registered.length, 1);
  const refused = registerAppTool({ registerTool: (f) => registered.push(f) }, "records",
    [...RECORDS_CONTRACT_VERBS, "export"], () => ({}), quiet);
  assert.equal(refused, false);
  assert.equal(registered.length, 1);
});
