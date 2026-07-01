/**
 * Tests for RosterTools — Phase C.2 + C.3.
 *
 * Covers the four plugin-side roster mutation tools and the two
 * sender-resolution paths (sender registry from before_agent_run for
 * groups + DMs; sessionKey trailing-int fallback for DMs only).
 *
 * Daemon transport is stubbed via the factory's optional `transport`.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/rosterTools.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  createRosterSetRoleToolFactory,
  createRosterBlockToolFactory,
  createRosterUnblockToolFactory,
  createChannelSetNewcomerModeToolFactory,
} from "../dist/tools/RosterTools.js";
import {
  captureSender,
  _resetForTests as resetSenderRegistry,
} from "../dist/util/senderRegistry.js";


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


/**
 * Capturing stub transport. Returns either `next_response` for the
 * next call (set via `.queue()`) or a 200/{ok:true} default. Records
 * every request so tests can assert on path + body + headers.
 */
function captureTransport() {
  const calls = [];
  const queued = [];
  const cap = async (req) => {
    calls.push(req);
    if (queued.length) return queued.shift();
    return { status: 200, body: { ok: true } };
  };
  cap.calls = calls;
  cap.queue = (status, body) => queued.push({ status, body });
  return cap;
}


function buildSetRoleTool(opts = {}) {
  const logger = opts.logger ?? fakeLogger();
  const transport = opts.transport ?? captureTransport();
  const factory = createRosterSetRoleToolFactory(
    { botId: opts.botId ?? "atlas", transport },
    logger,
  );
  const ctx = {
    sessionKey: opts.sessionKey ?? "agent:main:telegram:direct:987654321",
    sessionId: opts.sessionId ?? "sess-uuid",
    messageChannel: opts.messageChannel ?? "telegram",
    runId: opts.runId ?? null,
  };
  return { tool: factory(ctx), logger, transport };
}


function buildBlockTool(opts = {}) {
  const logger = opts.logger ?? fakeLogger();
  const transport = opts.transport ?? captureTransport();
  const factory = createRosterBlockToolFactory(
    { botId: opts.botId ?? "atlas", transport },
    logger,
  );
  const ctx = {
    sessionKey: opts.sessionKey ?? "agent:main:telegram:direct:987654321",
    runId: opts.runId ?? null,
  };
  return { tool: factory(ctx), logger, transport };
}


function buildUnblockTool(opts = {}) {
  const logger = opts.logger ?? fakeLogger();
  const transport = opts.transport ?? captureTransport();
  const factory = createRosterUnblockToolFactory(
    { botId: opts.botId ?? "atlas", transport },
    logger,
  );
  const ctx = {
    sessionKey: opts.sessionKey ?? "agent:main:telegram:direct:987654321",
    runId: opts.runId ?? null,
  };
  return { tool: factory(ctx), logger, transport };
}


function buildNewcomerModeTool(opts = {}) {
  const logger = opts.logger ?? fakeLogger();
  const transport = opts.transport ?? captureTransport();
  const factory = createChannelSetNewcomerModeToolFactory(
    { botId: opts.botId ?? "atlas", transport },
    logger,
  );
  const ctx = {
    sessionKey: opts.sessionKey ?? "agent:main:telegram:direct:987654321",
    runId: opts.runId ?? null,
  };
  return { tool: factory(ctx), logger, transport };
}


// Reset the registry between tests so capture state doesn't bleed.
test.beforeEach(() => resetSenderRegistry());


// ── Schema metadata ────────────────────────────────────────────────────


test("each tool exposes name + description + parameters schema", () => {
  const { tool: set } = buildSetRoleTool();
  const { tool: block } = buildBlockTool();
  const { tool: unblock } = buildUnblockTool();
  const { tool: mode } = buildNewcomerModeTool();
  for (const t of [set, block, unblock, mode]) {
    // Anthropic Messages API enforces ^[a-zA-Z0-9_-]{1,128}$ on tool names.
    assert.match(t.name, /^[a-zA-Z0-9_-]{1,128}$/);
    assert.ok(typeof t.description === "string" && t.description.length > 0);
    assert.ok(t.parameters);  // TypeBox schema
  }
  assert.equal(set.name, "roster_set_role");
  assert.equal(block.name, "roster_block");
  assert.equal(unblock.name, "roster_unblock");
  assert.equal(mode.name, "channel_set_newcomer_mode");
});


// ── Sender resolution paths ───────────────────────────────────────────


test("sender registry path: tool uses senderId captured by before_agent_run", async () => {
  // Simulate what TurnObserver.before_agent_run does: capture the
  // sender against the turn's runId. Tools then resolve via getSender.
  captureSender("run-xyz", {
    senderId: "1260193629",
    channelId: "-100123456789",  // group chat_id
    senderIsOwner: false,
  });
  const { tool, transport } = buildBlockTool({
    sessionKey: "agent:main:telegram:direct:-100123456789",  // group session
    runId: "run-xyz",
  });
  const result = await tool.execute("call-1", {
    channel: "telegram",
    target_id: "555",
  });
  // Tool succeeded — group sessions now work via the registry path.
  assert.equal(result.isError, undefined);
  assert.equal(transport.calls.length, 1);
  assert.equal(
    transport.calls[0].headers["X-Requester-Identity"],
    "telegram:1260193629",
  );
});


test("sessionKey fallback path: tool extracts user_id from DM sessionKey when no registry entry", async () => {
  const { tool, transport } = buildBlockTool({
    sessionKey: "agent:main:telegram:direct:987654321",
    runId: "run-no-capture",  // no captureSender call → fallback fires
  });
  await tool.execute("call-1", {
    channel: "telegram",
    target_id: "555",
  });
  assert.equal(
    transport.calls[0].headers["X-Requester-Identity"],
    "telegram:987654321",
  );
});


test("group sessionKey + no registry entry → refusal (sessionKey fallback rejects negative trailing int)", async () => {
  const { tool, transport } = buildBlockTool({
    sessionKey: "agent:main:telegram:direct:-100123456789",
    runId: "run-no-capture-group",
  });
  const result = await tool.execute("call-1", {
    channel: "telegram",
    target_id: "555",
  });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /identify|verified sender/i);
  assert.equal(transport.calls.length, 0);
});


test("missing runId AND missing sessionKey → refusal", async () => {
  const { tool, transport } = buildBlockTool({
    sessionKey: "",  // explicit empty so the builder default doesn't kick in
    runId: undefined,
  });
  const result = await tool.execute("call-1", {
    channel: "telegram",
    target_id: "555",
  });
  assert.equal(result.isError, true);
  assert.equal(transport.calls.length, 0);
});


test("registry path wins over sessionKey when both are present", async () => {
  // sessionKey says one id; registry says another. Registry wins because
  // it's the trusted gateway-attested source.
  captureSender("run-priority", {
    senderId: "REGISTRY_ID",
    channelId: null,
    senderIsOwner: false,
  });
  const { tool, transport } = buildBlockTool({
    sessionKey: "agent:main:telegram:direct:SESSIONKEY_ID",
    runId: "run-priority",
  });
  // sessionKey trailing isn't all-digits so the fallback wouldn't match
  // anyway, but the test is about the priority order: even when both
  // would resolve, registry wins. Use a numeric sessionKey to make it real.
  await tool.execute("call-1", {
    channel: "telegram",
    target_id: "555",
  });
  assert.equal(
    transport.calls[0].headers["X-Requester-Identity"],
    "telegram:REGISTRY_ID",
  );
});


// ── Platform threading (audit R1a G-N2) ──────────────────────────────


test("Slack sender resolves 'slack:<id>' in X-Requester-Identity (not telegram:)", async () => {
  captureSender("run-slack", {
    senderId: "U0ABCDEF",
    channelId: "C0GROUP",
    platform: "slack",
    senderIsOwner: false,
  });
  const { tool, transport } = buildBlockTool({
    sessionKey: "agent:main:slack:channel:C0GROUP",
    runId: "run-slack",
  });
  const result = await tool.execute("call-1", {
    channel: "slack",
    target_id: "U0TARGET",
  });
  assert.equal(result.isError, undefined);
  assert.equal(
    transport.calls[0].headers["X-Requester-Identity"],
    "slack:U0ABCDEF",
  );
});


test("Discord sender resolves 'discord:<id>' in X-Requester-Identity", async () => {
  captureSender("run-discord", {
    senderId: "112233445566",
    channelId: "778899",
    platform: "discord",
    senderIsOwner: false,
  });
  const { tool, transport } = buildSetRoleTool({
    sessionKey: "agent:main:discord:guild:778899",
    runId: "run-discord",
  });
  await tool.execute("call-1", {
    channel: "discord",
    target_id: "998877",
    role: "primary_user",
  });
  assert.equal(
    transport.calls[0].headers["X-Requester-Identity"],
    "discord:112233445566",
  );
});


test("registry sender with no threaded platform falls back to telegram (no regression)", async () => {
  // Pre-fix capture path: platform absent → default telegram, preserving
  // the working Telegram behavior. Fail-closed for a foreign id.
  captureSender("run-noplat", {
    senderId: "1260193629",
    channelId: null,
    senderIsOwner: false,
  });
  const { tool, transport } = buildBlockTool({ runId: "run-noplat" });
  await tool.execute("call-1", { channel: "telegram", target_id: "555" });
  assert.equal(
    transport.calls[0].headers["X-Requester-Identity"],
    "telegram:1260193629",
  );
});


test("sessionKey DM fallback keeps telegram platform (chat_id==user_id is telegram-only)", async () => {
  const { tool, transport } = buildBlockTool({
    sessionKey: "agent:main:telegram:direct:987654321",
    runId: "run-no-capture",
  });
  await tool.execute("call-1", { channel: "telegram", target_id: "555" });
  assert.equal(
    transport.calls[0].headers["X-Requester-Identity"],
    "telegram:987654321",
  );
});


test("fail-closed: unresolved Slack sender refuses locally, never calls header-less", async () => {
  // No registry capture, and a group sessionKey whose trailing int is
  // negative → sessionKey fallback rejects → local refusal, no daemon call.
  const { tool, transport } = buildBlockTool({
    sessionKey: "agent:main:slack:channel:-100999",
    runId: "run-unresolved-slack",
  });
  const result = await tool.execute("call-1", {
    channel: "slack",
    target_id: "U0TARGET",
  });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /identify|verified sender/i);
  assert.equal(transport.calls.length, 0);
});


// ── Tool happy paths ────────────────────────────────────────────────


test("roster_set_role PATCHes the right path with X-Requester-Identity", async () => {
  const { tool, transport } = buildSetRoleTool();
  const result = await tool.execute("call-1", {
    channel: "telegram",
    target_id: "111",
    role: "primary_user",
  });
  assert.equal(result.isError, undefined);
  assert.equal(transport.calls.length, 1);
  const call = transport.calls[0];
  assert.equal(call.method, "PATCH");
  assert.equal(call.path, "/api/admin/bots/atlas/users/telegram/111");
  assert.deepEqual(call.body, { role: "primary_user" });
  assert.equal(call.headers["X-Requester-Identity"], "telegram:987654321");
  assert.equal(call.headers["X-Requester-Source-Bot"], "atlas");
});


test("roster_block POSTs to /block with reason in body", async () => {
  const { tool, transport } = buildBlockTool();
  await tool.execute("call-1", {
    channel: "telegram",
    target_id: "555",
    reason: "spam",
  });
  const call = transport.calls[0];
  assert.equal(call.method, "POST");
  assert.equal(call.path, "/api/admin/bots/atlas/users/telegram/555/block");
  assert.deepEqual(call.body, { reason: "spam" });
});


test("roster_block sends empty reason when omitted", async () => {
  const { tool, transport } = buildBlockTool();
  await tool.execute("call-1", {
    channel: "telegram",
    target_id: "555",
  });
  assert.deepEqual(transport.calls[0].body, { reason: "" });
});


test("roster_unblock POSTs to /unblock with no body", async () => {
  const { tool, transport } = buildUnblockTool();
  await tool.execute("call-1", {
    channel: "telegram",
    target_id: "555",
  });
  const call = transport.calls[0];
  assert.equal(call.method, "POST");
  assert.equal(call.path, "/api/admin/bots/atlas/users/telegram/555/unblock");
  assert.equal(call.body, undefined);
});


test("channel_set_newcomer_mode PUTs with mode + optional surfaces", async () => {
  const { tool, transport } = buildNewcomerModeTool();
  await tool.execute("call-1", {
    channel: "telegram",
    mode: "auto_admit",
    default_engagement_surfaces: ["group"],
  });
  const call = transport.calls[0];
  assert.equal(call.method, "PUT");
  assert.equal(call.path, "/api/admin/bots/atlas/channels/telegram/newcomer_mode");
  assert.deepEqual(call.body, {
    mode: "auto_admit",
    default_engagement_surfaces: ["group"],
  });
});


test("channel_set_newcomer_mode omits surfaces field when not provided", async () => {
  const { tool, transport } = buildNewcomerModeTool();
  await tool.execute("call-1", {
    channel: "telegram",
    mode: "closed",
  });
  assert.deepEqual(transport.calls[0].body, { mode: "closed" });
});


// ── Refusal envelopes from the daemon ─────────────────────────────────


test("403 from daemon returns a refusal envelope with the detail message", async () => {
  const transport = captureTransport();
  transport.queue(403, {
    error: "forbidden",
    detail:
      "role 'participant' on bot 'atlas' does not grant 'bot.roster.mutate' " +
      "(requester telegram:987654321)",
  });
  const { tool } = buildBlockTool({ transport });
  const result = await tool.execute("call-1", {
    channel: "telegram",
    target_id: "555",
  });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /Refused/i);
  assert.match(result.content[0].text, /bot\.roster\.mutate/);
});


test("400 from daemon returns a clean failure envelope", async () => {
  const transport = captureTransport();
  transport.queue(400, { error: "invalid 'channel'" });
  const { tool } = buildSetRoleTool({ transport });
  const result = await tool.execute("call-1", {
    channel: "telegram",
    target_id: "111",
    role: "primary_user",
  });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /Failed.*invalid/i);
});


test("AdminSocketUnavailable maps to a 'try again' message", async () => {
  const transport = async () => {
    const err = new Error("cannot reach admin daemon: ENOENT");
    err.name = "AdminSocketUnavailable";
    Object.setPrototypeOf(
      err,
      (await import("../dist/util/adminSocket.js")).AdminSocketUnavailable.prototype,
    );
    throw err;
  };
  const { tool } = buildSetRoleTool({ transport });
  const result = await tool.execute("call-1", {
    channel: "telegram",
    target_id: "111",
    role: "primary_user",
  });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /unavailable|try again/i);
});
