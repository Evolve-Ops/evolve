/**
 * Tests for BoardTool — the bot's half of the board (D-MB4).
 *
 * WHAT THESE PIN:
 *   * **Each verb's request shape** — method, path, body — and that NO bot id
 *     is ever sent: identity is the socket peer uid, bound server-side, so a
 *     bot cannot name another bot's board.
 *   * **Fail closed.** Daemon unreachable ⇒ one operator-legible refusal, an
 *     error envelope, and nothing written. No fallback path exists.
 *   * **The list is bounded.** A 200-card fixture renders under the cap, in
 *     compact lines rather than card JSON, and says how many it left out.
 *   * **Every failure returns a non-throwing envelope** — a board fault must
 *     never break the turn the user is having.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/boardTool.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  createBoardToolFactory,
  renderList,
  DAEMON_UNREACHABLE_REFUSAL,
  MOVE_TO_BOT_REFUSAL,
  MAX_LIST_CARDS,
  MAX_LIST_CHARS,
} from "../dist/tools/BoardTool.js";
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

function captureTransport(defaultResponse = { status: 200, body: { ok: true } }) {
  const calls = [];
  const queued = [];
  const cap = async (req) => {
    calls.push(req);
    if (queued.length) {
      const next = queued.shift();
      if (typeof next === "function") return next(req);
      return next;
    }
    return defaultResponse;
  };
  cap.calls = calls;
  cap.queue = (resp) => queued.push(resp);
  return cap;
}

function buildTool(opts = {}) {
  const logger = opts.logger ?? fakeLogger();
  const transport = opts.transport ?? captureTransport(opts.response);
  const factory = createBoardToolFactory(
    { sharedDir: "/tmp/shared", botId: opts.botId ?? "personal-bot", transport },
    logger,
  );
  return { tool: factory({}), logger, transport };
}

const CARD = {
  id: "0123456789abcdef0123456789abcdef",
  title: "Book the scan",
  lane: "today",
  owner: "me",
  cluster: "health",
};

// ── request shapes ───────────────────────────────────────────────────────────

test("list GETs the bot-facing path with filters and the cap", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 200, body: { total: 0, cards: [] } });
  await tool.execute("c1", { verb: "list", cluster: "health", lane: "today", owner: "bot" });
  const req = transport.calls[0];
  assert.equal(req.method, "GET");
  const url = new URL(req.path, "http://x");
  assert.equal(url.pathname, "/api/board-bot/cards");
  assert.equal(url.searchParams.get("cluster"), "health");
  assert.equal(url.searchParams.get("lane"), "today");
  assert.equal(url.searchParams.get("owner"), "bot");
  assert.equal(url.searchParams.get("limit"), String(MAX_LIST_CARDS));
  assert.equal(req.body, undefined);
});

test("add POSTs title/cluster/source and passes enrichment through", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 201, body: { ok: true, card: CARD } });
  const enrichment = { runtime_min: { value: 166, source: "tmdb" } };
  const res = await tool.execute("c1", {
    verb: "add", title: "  Dune  ", cluster: "watch", lane: "later",
    owner: "bot", note: "the long one", source: "chat", enrichment,
  });
  assert.equal(res.isError, undefined);
  const req = transport.calls[0];
  assert.equal(req.method, "POST");
  assert.equal(req.path, "/api/board-bot/cards");
  assert.deepEqual(req.body, {
    title: "Dune", cluster: "watch", source: "chat", lane: "later",
    owner: "bot", note: "the long one", enrichment,
  });
});

test("add defaults cluster to admin and source to chat", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 201, body: { ok: true, card: CARD } });
  await tool.execute("c1", { verb: "add", title: "Call the vet" });
  assert.deepEqual(transport.calls[0].body,
    { title: "Call the vet", cluster: "admin", source: "chat" });
});

test("move, assign and progress POST to their own sub-resources", async () => {
  const { tool, transport } = buildTool();
  for (const params of [
    { verb: "move", id: "abc123de", to_lane: "done" },
    { verb: "assign", id: "abc123de", owner: "bot" },
    { verb: "progress", id: "abc123de", state: "blocked", note: "no slots", cost_to_date: 0.4 },
  ]) {
    transport.queue({ status: 200, body: { ok: true, card: CARD } });
    await tool.execute("c1", params);
  }
  assert.deepEqual(transport.calls.map((c) => c.path), [
    "/api/board-bot/cards/abc123de/move",
    "/api/board-bot/cards/abc123de/assign",
    "/api/board-bot/cards/abc123de/progress",
  ]);
  assert.deepEqual(transport.calls[0].body, { to_lane: "done" });
  assert.deepEqual(transport.calls[1].body, { owner: "bot" });
  assert.deepEqual(transport.calls[2].body,
    { state: "blocked", note: "no slots", cost_to_date: 0.4 });
});

test("no request ever carries a bot id — identity is the peer uid", async () => {
  const { tool, transport } = buildTool();
  for (const params of [
    { verb: "list" },
    { verb: "add", title: "x" },
    { verb: "move", id: "abc123de", to_lane: "today" },
    { verb: "assign", id: "abc123de", owner: "bot" },
    { verb: "progress", id: "abc123de", state: "accepted" },
  ]) {
    transport.queue({ status: 200, body: { ok: true, card: CARD, total: 0, cards: [] } });
    await tool.execute("c1", params);
  }
  for (const call of transport.calls) {
    const wire = `${call.path} ${JSON.stringify(call.body ?? {})}`;
    assert.ok(!/personal-bot|bot_id|botId/.test(wire), wire);
  }
});

test("a card id is URL-encoded into the path", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 404, body: { error: "no such card" } });
  await tool.execute("c1", { verb: "move", id: "../../etc/passwd", to_lane: "today" });
  assert.equal(transport.calls[0].path,
    "/api/board-bot/cards/..%2F..%2Fetc%2Fpasswd/move");
});

// ── missing arguments never reach the daemon ─────────────────────────────────

test("a verb missing its required argument refuses locally", async () => {
  const { tool, transport } = buildTool();
  for (const [params, needle] of [
    [{ verb: "add" }, "`title` is required"],
    [{ verb: "move", id: "abc123de" }, "`to_lane` is required"],
    [{ verb: "move", to_lane: "today" }, "`id` is required"],
    [{ verb: "assign", id: "abc123de" }, "`owner` is required"],
    [{ verb: "progress", id: "abc123de" }, "`state` is required"],
    [{ verb: "wat" }, "unknown verb"],
  ]) {
    const res = await tool.execute("c1", params);
    assert.equal(res.isError, true, JSON.stringify(params));
    assert.ok(res.content[0].text.includes(needle), res.content[0].text);
  }
  assert.equal(transport.calls.length, 0, "nothing should have been sent");
});

// ── fail closed ──────────────────────────────────────────────────────────────

test("an unreachable daemon refuses with the standard text and writes nothing", async () => {
  const { tool, logger } = buildTool({
    transport: async () => {
      throw new AdminSocketUnavailable("cannot reach admin daemon at /tmp/x.sock");
    },
  });
  for (const params of [
    { verb: "list" },
    { verb: "add", title: "Book the scan", cluster: "health" },
    { verb: "move", id: "abc123de", to_lane: "today" },
    { verb: "assign", id: "abc123de", owner: "bot" },
    { verb: "progress", id: "abc123de", state: "done" },
  ]) {
    const res = await tool.execute("c1", params);
    assert.equal(res.isError, true);
    assert.equal(res.content[0].text, DAEMON_UNREACHABLE_REFUSAL);
  }
  assert.ok(logger.records.warn.length >= 5);
});

test("an HTTP error surfaces the daemon's own message, not a stack", async () => {
  const { tool, transport } = buildTool();
  transport.queue({ status: 400, body: { error: "invalid lane; one of ['inbox']" } });
  const res = await tool.execute("c1", { verb: "move", id: "abc123de", to_lane: "today" });
  assert.equal(res.isError, true);
  assert.ok(res.content[0].text.includes("invalid lane"), res.content[0].text);
});

test("an unexpected transport error is an envelope, not a throw", async () => {
  const { tool } = buildTool({
    transport: async () => { throw new Error("kaboom"); },
  });
  const res = await tool.execute("c1", { verb: "list" });
  assert.equal(res.isError, true);
  assert.ok(res.content[0].text.includes("kaboom"));
});

// ── context economy ──────────────────────────────────────────────────────────

test("a 200-card board renders compactly, under both caps", () => {
  const cards = Array.from({ length: 200 }, (_, i) => ({
    id: `${i}`.padStart(32, "0"),
    title: `Card number ${i} with a reasonably wordy title`,
    lane: i % 3 ? "today" : "inbox",
    owner: i % 5 === 0 ? "bot" : "me",
    cluster: i % 2 ? "work" : "home",
    ...(i % 5 === 0 ? { delegation: "accepted" } : {}),
  }));
  const text = renderList({ total: 200, cards });
  assert.ok(text.length <= MAX_LIST_CHARS + 200, `rendered ${text.length} chars`);
  const lines = text.split("\n");
  // Header + at most MAX_LIST_CARDS rows + the trailer.
  assert.ok(lines.length <= MAX_LIST_CARDS + 2, `rendered ${lines.length} lines`);
  assert.ok(text.startsWith("200 cards; showing "));
  assert.ok(text.includes("more not shown"), text.slice(-120));
  // Compact LINES, never card JSON.
  assert.ok(!text.includes("{"), "the list must not carry JSON");
  assert.ok(lines[1].includes(" · "), lines[1]);
});

test("a short board shows every card and no trailer", () => {
  const text = renderList({ total: 1, cards: [{ ...CARD, delegation: undefined }] });
  assert.equal(text, "1 card; showing 1.\n01234567 · Book the scan · today · me · health");
});

test("a bot-owned card shows its delegation state, and enrichment shows as keys", () => {
  const text = renderList({
    total: 1,
    cards: [{ ...CARD, owner: "bot", delegation: "in_progress",
              enriched: ["runtime_min"] }],
  });
  assert.ok(text.includes("bot:in_progress"), text);
  assert.ok(text.includes("(+runtime_min)"), text);
});

test("an empty board says so rather than returning nothing", () => {
  assert.equal(renderList({ total: 0, cards: [] }), "The board has no cards matching that.");
  assert.equal(renderList({ total: 12, cards: [] }), "No cards matched that filter.");
});

test("the registered schema stays small — it rides in every prompt", () => {
  const { tool } = buildTool();
  const bytes = Buffer.byteLength(JSON.stringify({
    name: tool.name, description: tool.description, parameters: tool.parameters,
  }), "utf8");
  // One tool for five verbs. The budget is a ratchet, not a target: raising
  // it is a deliberate edit, because this weight is paid on EVERY turn
  // (context economy CE-2).
  //
  // Raised 2800 -> 3000 for D-BI2's two ratified fields: `reason` (the drop
  // vocabulary, which only reaches the model if it is in the schema) and
  // `source_id` (without which stocking cannot dedup and dropped cards come
  // back). Paid for where it could be — the drop reasons ride as one
  // description line rather than a union of four literals, and every
  // description touched in that change got shorter.
  assert.ok(bytes < 3000, `registered schema is ${bytes} bytes`);
});

// ── D-BI7 / D-BI2: WHO is not a lane, and a drop can say why ─────────────

test("move refuses the retired Bot lane locally and names assign", async () => {
  const { tool, transport } = buildTool();
  const res = await tool.execute("c1", {
    verb: "move", id: "abc123de", to_lane: "bot",
  });
  assert.equal(res.isError, true);
  assert.equal(res.content[0].text, MOVE_TO_BOT_REFUSAL);
  assert.match(res.content[0].text, /assign/);
  // Refused BEFORE the socket: a request that cannot succeed should not cost
  // a round trip, and "nothing was changed" must be literally true.
  assert.equal(transport.calls.length, 0);
});

test("a drop carries its reason; a reason anywhere else is refused", async () => {
  const { tool, transport } = buildTool();
  await tool.execute("c1", {
    verb: "move", id: "abc123de", to_lane: "dropped", reason: "never",
  });
  assert.deepEqual(transport.calls[0].body, { to_lane: "dropped", reason: "never" });

  const res = await tool.execute("c2", {
    verb: "move", id: "abc123de", to_lane: "today", reason: "never",
  });
  assert.equal(res.isError, true);
  assert.match(res.content[0].text, /belongs to a drop/);
  assert.equal(transport.calls.length, 1, "the bad call never reached the socket");
});

test("a drop with no reason is still a plain move", async () => {
  const { tool, transport } = buildTool();
  await tool.execute("c1", { verb: "move", id: "abc123de", to_lane: "dropped" });
  assert.deepEqual(transport.calls[0].body, { to_lane: "dropped" });
});

test("add forwards the upstream source id that makes dedup possible", async () => {
  const { tool, transport } = buildTool();
  await tool.execute("c1", {
    verb: "add", title: "Standup", cluster: "work", source: "calendar",
    source_id: "  evt-42  ",
  });
  assert.equal(transport.calls[0].body.source_id, "evt-42");
  // Omitted when absent — an empty string would be a source id that matches
  // every other card without one.
  const second = buildTool();
  await second.tool.execute("c2", { verb: "add", title: "x", cluster: "work" });
  assert.equal("source_id" in second.transport.calls[0].body, false);
});

test("list lines carry owner, and the guidance says assign rather than move", async () => {
  const { tool } = buildTool();
  const line = renderList({
    total: 2,
    cards: [
      { ...CARD, owner: "bot", delegation: "in_progress" },
      { ...CARD, id: "ffffffffffffffffffffffffffffffff", owner: "me" },
    ],
  });
  assert.match(line, /bot:in_progress/);
  assert.match(line, / me /);
  // The tool's own description is where a model learns the axis split.
  assert.match(tool.description, /ASSIGN it \(owner=bot\)/);
  assert.match(tool.description, /don't move/);
});

test("a skipped add is reported as a skip, not as a card that was added", async () => {
  const { tool } = buildTool({
    response: {
      status: 200,
      body: { ok: true, skipped: "already settled", lane: "dropped",
              card: { ...CARD, title: "Standup", lane: "dropped" } },
    },
  });
  const res = await tool.execute("c1", {
    verb: "add", title: "Standup", cluster: "work", source_id: "evt-42",
  });
  assert.match(res.content[0].text, /skipped/);
  assert.match(res.content[0].text, /dropped/);
  assert.equal(res.isError, undefined,
    "a skip is the store working as designed, not a tool failure");
});
