/**
 * Tests for channel-identity resolution — the OC 2026.7 ctx-shape fix.
 *
 * The defect. TurnObserver's before_agent_run hook derived the speaker's
 * messaging platform from ``ctx.channelId``, on the documented assumption
 * that OC threads the channel TYPE there. On OC 2026.7.1-2 that field
 * carries the actual chat id, so ``normalizePlatform`` correctly returned
 * null for every Slack turn — and the two resolve-or-omit consumers
 * silently degraded:
 *
 *   1. ``_buildSpeakerContextBlock`` omitted the SPEAKER block on EVERY
 *      Slack turn (the bot never learned who was speaking).
 *   2. The Layer-2 gate logged the pod owner as an ordinary participant
 *      on his own bot ("speaker=null:U0PLKKXV0 role='participant'").
 *
 * Telegram was unaffected (its channelId still normalized), which is why
 * this survived the audit R1a G-N2 / #3378 resolve-or-omit work that was
 * specifically meant to make non-Telegram platforms work.
 *
 * The tests below pin the fix against the REAL live ctx shape (team-bot-a,
 * Slack channel turn, agent_end ctx dump, 2026-08-19) rather than against
 * a shape invented to match the code.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/channelIdentity.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  resolveSenderPlatform,
  resolveChannelKindHint,
  platformFromSessionKey,
  isKindShapedChannel,
} from "../dist/util/channelIdentity.js";
import { getSender, normalizePlatform, _resetForTests } from "../dist/util/senderRegistry.js";
import { resolveConfig } from "../dist/config.js";
import { TurnObserver } from "../dist/observer/TurnObserver.js";

const BOT = "atlas";

/**
 * The verbatim live ctx from an OC 2026.7.1-2 Slack channel turn. The
 * whole point of this file is that the code agrees with THIS, so it is
 * defined once and every Slack case builds from it.
 */
const OC_2026_7_SLACK_CTX = Object.freeze({
  sessionKey: "agent:main:slack:channel:g0t79fgse",
  messageProvider: "slack",
  channel: "slack",
  channelId: "g0t79fgse",
  chatId: "G0T79FGSE",
  senderId: "U087LN8U4J0",
});

/** The Telegram shape that worked before the fix and must keep working. */
const TELEGRAM_CTX = Object.freeze({
  sessionKey: "agent:main:telegram:direct:1260193629",
  messageProvider: "telegram",
  channel: "telegram",
  channelId: "telegram",
  senderId: "1260193629",
});

function mkTmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "chanident-"));
}

function recordingLogger() {
  const calls = { info: [], warn: [], error: [], debug: [] };
  return {
    _calls: calls,
    info: (m) => calls.info.push(m),
    warn: (m) => calls.warn.push(m),
    error: (m) => calls.error.push(m),
    debug: (m) => calls.debug.push(m),
  };
}

/** network.json + overlay making slack:U0PLKKXV0 the pod admin — the
 *  identity the production log misreported as a plain participant. */
function seedSlackOwner(sharedDir) {
  fs.mkdirSync(sharedDir, { recursive: true });
  fs.writeFileSync(
    path.join(sharedDir, "network.json"),
    JSON.stringify({
      pod: { admins: { external_ids: { slack: ["U0PLKKXV0"] } } },
      bots: { [BOT]: { primary_user: { external_ids: { telegram: "500" } } } },
    }),
  );
  const dir = path.join(sharedDir, "rosters");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, `${BOT}.json`),
    JSON.stringify({ identities: {}, blocked: {} }),
  );
}

/** Build an observer with before_agent_run registered, and hand back the
 *  handler + the observer so a test can drive the REAL hook path. */
function mkObserver(sharedDir, tier = "full") {
  const config = resolveConfig({ botId: BOT, sharedDir, tier }, {});
  const logger = recordingLogger();
  const handlers = {};
  const fakeApi = { on: (evt, h) => { handlers[evt] = h; }, logger };
  const observer = new TurnObserver(config, logger, fakeApi);
  observer.register(fakeApi);
  return { observer, handlers, logger };
}

test.beforeEach(() => _resetForTests());


// ── 1. resolveSenderPlatform over the real shapes ─────────────────────

test("OC 2026.7 Slack ctx resolves to platform 'slack' (the defect)", () => {
  // Pre-fix this read ctx.channelId → "g0t79fgse" → null.
  assert.equal(resolveSenderPlatform(OC_2026_7_SLACK_CTX, {}), "slack");
  // …and it must be the TYPE-SHAPED FIELDS doing the work, not the
  // sessionKey last resort. The live ctx happens to carry "slack" in four
  // places, so without this second assertion the test passes even with the
  // fix's primary mechanism deleted — it would be measuring the fallback.
  const noKey = { ...OC_2026_7_SLACK_CTX, sessionKey: undefined };
  assert.equal(resolveSenderPlatform(noKey, {}), "slack");
  // Belt and braces: with the type-shaped fields ALSO gone, only the
  // (useless) chat id is left and the resolver must say null.
  assert.equal(
    resolveSenderPlatform({ channelId: "g0t79fgse" }, {}), null,
    "the chat id alone must never resolve",
  );
});

test("Telegram ctx still resolves to 'telegram' (no regression)", () => {
  assert.equal(resolveSenderPlatform(TELEGRAM_CTX, {}), "telegram");
});

test("legacy gateway that threads the TYPE on channelId still resolves", () => {
  // No ctx.channel / ctx.messageProvider at all — the pre-2026.7 shape.
  assert.equal(resolveSenderPlatform({ channelId: "telegram" }, {}), "telegram");
  assert.equal(resolveSenderPlatform({ channelId: "slack_dm" }, {}), "slack");
  // …and off the event when ctx carries nothing.
  assert.equal(resolveSenderPlatform({}, { channelId: "discord" }), "discord");
});

test("a present-but-useless channelId does not terminate the chain", () => {
  // The `??` shape this replaced would stop on the chat id and return null
  // even though a type-shaped field was available.
  assert.equal(
    resolveSenderPlatform({ channelId: "g0t79fgse", messageProvider: "slack" }, {}),
    "slack",
  );
});

test("the type-shaped fields win over a legacy-shaped channelId", () => {
  assert.equal(
    resolveSenderPlatform({ channel: "slack", channelId: "telegram" }, {}),
    "slack",
  );
});


// ── 2. the resolve-or-omit guard is NOT loosened ──────────────────────

test("unrecognized / absent platform still resolves to null — never a guess", () => {
  assert.equal(resolveSenderPlatform(null, null), null);
  assert.equal(resolveSenderPlatform({}, {}), null);
  assert.equal(resolveSenderPlatform({ channel: "web" }, {}), null);
  assert.equal(resolveSenderPlatform({ channel: "unknown", channelId: "" }, {}), null);
  // A bare chat id with nothing else to go on: still null, NOT a default.
  assert.equal(resolveSenderPlatform({ channelId: "g0t79fgse" }, {}), null);
  // Non-string junk must not throw or coerce into a match.
  assert.equal(resolveSenderPlatform({ channel: 42, channelId: null }, {}), null);
});

test("sessionKey last resort matches a whole segment only", () => {
  assert.equal(platformFromSessionKey("agent:main:slack:channel:g0t79fgse"), "slack");
  assert.equal(platformFromSessionKey("agent:main:telegram:direct:123"), "telegram");
  // NOT a prefix match. normalizePlatform accepts "slack_…" BY DESIGN —
  // it exists to fold surface-suffixed channel-TYPE values ("slack_dm")
  // onto their base platform, not to judge arbitrary session-key
  // segments. So "slack_relay" below normalizes to "slack", and only the
  // whole-segment rule stops this last resort from inventing a platform
  // out of a channel slug or a namespaced id.
  assert.equal(normalizePlatform("slack_relay"), "slack", "the case must discriminate");
  assert.equal(platformFromSessionKey("agent:main:cli:slack_relay:1"), null);
  assert.equal(platformFromSessionKey("agent:main:cli:slackbot-relay:1"), null);
  assert.equal(platformFromSessionKey("agent:main:web:direct:1"), null);
  assert.equal(platformFromSessionKey(""), null);
  // Evolve's own subagent keys carry no platform segment.
  assert.equal(platformFromSessionKey("evolve:summarizer:1755600000000"), null);
});

test("sessionKey is consulted only after the explicit fields fail", () => {
  // Explicit field wins even when the sessionKey says otherwise.
  assert.equal(
    resolveSenderPlatform(
      { channel: "slack", sessionKey: "agent:main:telegram:direct:9" }, {},
    ),
    "slack",
  );
  // …and carries the turn when nothing else is threaded.
  assert.equal(
    resolveSenderPlatform({ sessionKey: "agent:main:slack:channel:g0t79fgse" }, {}),
    "slack",
  );
});


// ── 3. end-to-end through the real before_agent_run hook ──────────────

test("before_agent_run captures platform='slack' from the live OC 2026.7 ctx", async () => {
  const sharedDir = mkTmpDir();
  seedSlackOwner(sharedDir);
  const { handlers } = mkObserver(sharedDir, "monitor");
  assert.ok(handlers["before_agent_run"], "hook must be registered");

  await handlers["before_agent_run"](
    { senderId: "U0PLKKXV0", senderIsOwner: true, channelId: "g0t79fgse" },
    { ...OC_2026_7_SLACK_CTX, runId: "slack-run", senderId: "U0PLKKXV0" },
  );

  const captured = getSender("slack-run");
  assert.ok(captured, "sender must be captured");
  assert.equal(captured.senderId, "U0PLKKXV0");
  assert.equal(captured.platform, "slack", "pre-fix this was null");
  // Same discrimination as the unit test: prove the type-shaped fields are
  // the mechanism, not the sessionKey fallback that also carries "slack".
  await handlers["before_agent_run"](
    { senderId: "U0PLKKXV0", channelId: "g0t79fgse" },
    { ...OC_2026_7_SLACK_CTX, sessionKey: undefined, runId: "slack-run-nokey" },
  );
  assert.equal(getSender("slack-run-nokey").platform, "slack");
  // channelId keeps carrying the raw value — it is an ID, and the chat-id
  // consumers still want it verbatim.
  assert.equal(captured.channelId, "g0t79fgse");
});

test("the SPEAKER block renders on a Slack turn, with the owner's real role", async () => {
  const sharedDir = mkTmpDir();
  seedSlackOwner(sharedDir);
  const { observer, handlers } = mkObserver(sharedDir, "monitor");

  const ctx = { ...OC_2026_7_SLACK_CTX, runId: "slack-block", senderId: "U0PLKKXV0" };
  await handlers["before_agent_run"](
    { senderId: "U0PLKKXV0", senderIsOwner: true, channelId: "g0t79fgse" }, ctx,
  );

  const block = observer._buildSpeakerContextBlock(ctx);
  assert.notEqual(block, "", "pre-fix the block was omitted on EVERY Slack turn");
  // The block must render off the type-shaped fields alone — otherwise this
  // test, the one that carries the whole narrative, is really exercising the
  // sessionKey fallback.
  const noKeyCtx = {
    ...OC_2026_7_SLACK_CTX, sessionKey: undefined, runId: "slack-block-nokey",
  };
  await handlers["before_agent_run"](
    { senderId: "U0PLKKXV0", channelId: "g0t79fgse" }, noKeyCtx,
  );
  assert.match(observer._buildSpeakerContextBlock(noKeyCtx), /slack:U0PLKKXV0/);
  assert.match(block, /^SPEAKER \(this turn\):/);
  assert.match(block, /slack:U0PLKKXV0/, "must key the slack id-space, not telegram");
  // The reported symptom: the pod owner logged as an ordinary participant.
  assert.match(block, /role=admin/);
  assert.match(block, /can_mutate_roster=yes/);
});

test("the SPEAKER block still renders on a Telegram turn (no regression)", async () => {
  const sharedDir = mkTmpDir();
  fs.mkdirSync(sharedDir, { recursive: true });
  fs.writeFileSync(
    path.join(sharedDir, "network.json"),
    JSON.stringify({
      pod: { admins: { external_ids: { telegram: ["1260193629"] } } },
      bots: { [BOT]: { primary_user: { external_ids: { telegram: "500" } } } },
    }),
  );
  const { observer, handlers } = mkObserver(sharedDir, "monitor");

  const ctx = { ...TELEGRAM_CTX, runId: "tg-block" };
  await handlers["before_agent_run"](
    { senderId: "1260193629", senderIsOwner: true, channelId: "telegram" }, ctx,
  );

  assert.equal(getSender("tg-block").platform, "telegram");
  const block = observer._buildSpeakerContextBlock(ctx);
  assert.match(block, /telegram:1260193629/);
  assert.match(block, /role=admin/);
});

test("an unresolvable platform still OMITS the block — guard not loosened", async () => {
  const sharedDir = mkTmpDir();
  seedSlackOwner(sharedDir);
  const { observer, handlers } = mkObserver(sharedDir, "monitor");

  // A sender arrives, but nothing in the ctx names a known platform. The
  // block must be omitted rather than defaulting to a platform — guessing
  // would resolve U0PLKKXV0 against the telegram id-space, which is the
  // cross-id-space mis-attribution audit R1a G-N2 closed.
  const ctx = { runId: "web-run", channel: "web", channelId: "some-web-session" };
  await handlers["before_agent_run"]({ senderId: "U0PLKKXV0", channelId: "x" }, ctx);

  assert.equal(getSender("web-run").platform, null);
  assert.equal(observer._buildSpeakerContextBlock(ctx), "");
});

test("no captured sender (cron / heartbeat turn) still omits the block", () => {
  const sharedDir = mkTmpDir();
  seedSlackOwner(sharedDir);
  const { observer } = mkObserver(sharedDir, "monitor");
  assert.equal(observer._buildSpeakerContextBlock({ runId: "never-captured" }), "");
});


// ── 4. the channel-KIND hint (the sibling site) ───────────────────────

test("channel-kind hint repairs a chat id into the real channel type", () => {
  assert.equal(resolveChannelKindHint(OC_2026_7_SLACK_CTX), "slack");
  assert.equal(resolveChannelKindHint({ channelId: "g0t79fgse", channel: "slack" }), "slack");
});

test("channel-kind hint NEVER overwrites an auto tell or a legacy type", () => {
  // These three are load-bearing: isAutoSource's L1 veto keys on them and
  // shouldRetagHeartbeatSource needs "heartbeat" verbatim. Substituting a
  // platform name here would fail the cost breaker OPEN.
  for (const tell of ["unknown", "heartbeat", "cron"]) {
    assert.equal(
      resolveChannelKindHint({ channelId: tell, channel: "slack", messageProvider: "slack" }),
      tell,
      `auto tell '${tell}' must survive untouched`,
    );
  }
  // A legacy type-threading gateway is already correct — leave it alone.
  assert.equal(
    resolveChannelKindHint({ channelId: "telegram_group", channel: "telegram" }),
    "telegram_group",
    "the surface-suffixed legacy value carries more information, not less",
  );
});

test("llm_output accumulates the channel KIND, not the chat id (call site)", async () => {
  // The helper is only worth anything if the hot path actually calls it.
  const sharedDir = mkTmpDir();
  seedSlackOwner(sharedDir);
  const { observer, handlers } = mkObserver(sharedDir, "monitor");
  assert.ok(handlers["llm_output"], "llm_output must be registered");

  await handlers["llm_output"](
    { sessionId: "s-slack", model: "claude-sonnet-5", provider: "anthropic", usage: {} },
    { ...OC_2026_7_SLACK_CTX, trigger: "user" },
  );
  assert.equal(
    observer.sessionLlmData.get("s-slack").channel, "slack",
    "pre-fix this accumulated the raw chat id 'g0t79fgse'",
  );

  // …and the auto tell that the L1 veto + heartbeat re-tag key on is not
  // rewritten into a platform name by the repair.
  await handlers["llm_output"](
    { sessionId: "s-hb", model: "claude-haiku-4-5", provider: "anthropic", usage: {} },
    { channelId: "heartbeat", channel: "slack", trigger: "heartbeat" },
  );
  assert.equal(observer.sessionLlmData.get("s-hb").channel, "heartbeat");
});


test("channel-kind hint falls through to the raw value, then null", () => {
  // Nothing typed and not a tell: hand back exactly what the old code used.
  assert.equal(resolveChannelKindHint({ channelId: "g0t79fgse" }), "g0t79fgse");
  assert.equal(resolveChannelKindHint({}), null);
  assert.equal(resolveChannelKindHint(null), null);
});


// ── 5. the guard's vocabulary must not drift behind its consumers ─────

test("isKindShapedChannel normalizes case and whitespace", () => {
  // Load-bearing and previously unpinned: without the normalization a
  // channelId of "HEARTBEAT" would miss the tell and be overwritten with a
  // platform name off ctx.channel, destroying a tell isAutoSource honours.
  for (const v of ["HEARTBEAT", "  heartbeat  ", "Cron-Event", "SUBAGENT"]) {
    assert.equal(isKindShapedChannel(v), true, `'${v}' must be recognized`);
  }
  assert.equal(resolveChannelKindHint({ channelId: "HEARTBEAT", channel: "slack" }), "HEARTBEAT");
  assert.equal(isKindShapedChannel("g0t79fgse"), false);
  assert.equal(isKindShapedChannel(""), false);
  assert.equal(isKindShapedChannel(null), false);
});


test("the guard covers every channel literal its downstream consumers read", () => {
  // A DRIFT RATCHET, not a restatement. The guard's contract is "never
  // overwrite a value some kind-consumer recognizes". That set lives in
  // three other files, in two languages, and none of them import from here
  // — so the only thing keeping this honest is a scan of the real sources.
  // If a consumer grows a new channel value, this fails rather than letting
  // the guard silently fall one vocabulary short (which is exactly how the
  // first version shipped, covering the 3 L1 tells while consumers read 7).
  const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
  const scans = [
    // inferTriggerKind's channel-based fallback: `ch === "x"`.
    { file: join(root, "packages/plugin/src/observer/TurnObserver.ts"),
      re: /\bch === "([a-z0-9_-]+)"/g },
    // _infer_trigger_kind / _chat_surface: `ch == "x"` and `ch in {...}`.
    { file: join(root, "packages/analyzer/cost_event_converter.py"),
      re: /\bch == "([a-z0-9_-]+)"/g },
    { file: join(root, "packages/analyzer/cost_event_converter.py"),
      re: /\bch in \{([^}]*)\}/g, split: true },
    // identity_discovery._NON_HUMAN_CHANNELS.
    { file: join(root, "packages/admin/evolve_admin/evo/identity_discovery.py"),
      re: /_NON_HUMAN_CHANNELS[^=]*=\s*frozenset\(\{([^}]*)\}/g, split: true },
  ];

  const found = new Set();
  for (const { file, re, split } of scans) {
    const text = readFileSync(file, "utf8");
    for (const m of text.matchAll(re)) {
      if (split) {
        for (const lit of m[1].matchAll(/"([a-z0-9_-]+)"/g)) found.add(lit[1]);
      } else {
        found.add(m[1]);
      }
    }
  }

  assert.ok(found.size >= 8, `scan must find the vocabulary, got ${[...found]}`);
  const uncovered = [...found].filter((v) => !isKindShapedChannel(v));
  assert.deepEqual(
    uncovered, [],
    `channel value(s) a consumer reads but the kind guard would overwrite: ${uncovered}`,
  );
});


test("an ABSENT channelId is never repaired — the caller's 'unknown' tell stands", () => {
  // A cron / heartbeat / daemon turn has no chat id at all. The caller's
  // fall-through is `existing?.channel ?? "unknown"`, and "unknown" IS the
  // auto tell isAutoSource keys the L1 veto on. Reading a platform name off
  // ctx.channel here would suppress the tell on exactly the turns it exists
  // for — a fail-open the present-channelId guard does NOT cover.
  assert.equal(resolveChannelKindHint({ channel: "telegram" }), null);
  assert.equal(resolveChannelKindHint({ messageProvider: "slack", trigger: "cron" }), null);
  assert.equal(resolveChannelKindHint({ channelId: "", channel: "slack" }), null);
});
