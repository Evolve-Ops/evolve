/**
 * Tests for the per-role daily cap on an operator/user DEFAULT role.
 *
 * 2026-09-04: a bot's `userTierOverride.defaultTier: "power"` routed every
 * session to the opus rung. The cap machinery existed — `roleCaps.power
 * .maxPerDayPerBot`, the per-day counters, `degradeRoleOnCap` — but only
 * `canEscalateToRole` consulted it, and that is called from SetTierTool and
 * the admin chip: the ESCALATION-REQUEST paths. `_resolveOperatorDefaultRole`
 * never asked, so the default spent without limit while the cap read 5.
 *
 * A default spends exactly like a request does, so it is now bounded like
 * one, degrading down the chain (max→power→standard) rather than refusing
 * the turn. Gates 1-2 of `canEscalateToRole` (`enabled`, `allowBotInitiated`)
 * deliberately do NOT apply here — those ask whether the BOT may escalate
 * itself, and an operator default is not the bot asking.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.defaultRoleCap.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import fs from "node:fs";
import path from "node:path";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const HAIKU = "anthropic/claude-haiku-4-5";
const SONNET = "anthropic/claude-sonnet-4-6";
const OPUS = "anthropic/claude-opus-4-8";
const FABLE = "anthropic/claude-fable-5";

const CFG = {
  rungs: [
    { id: "haiku-class",  models: [HAIKU],  costClass: "low" },
    { id: "sonnet-class", models: [SONNET], costClass: "medium" },
    { id: "opus-class",   models: [OPUS],   costClass: "high" },
    { id: "fable-class",  models: [FABLE],  costClass: "premium" },
  ],
  roles: { fast: "haiku-class", standard: "sonnet-class", power: "opus-class", max: "fable-class" },
  routing: { enabled: true, maintenanceRole: "fast", backgroundRole: "fast", ambiguousRole: null },
};

/** Router whose operator default is `role`, with `cap` power-turns a day. */
function newRouter({ role = "power", cap = 2, maxCap = 2, prefs } = {}) {
  return new ModelRouter({
    ...CFG,
    roleCaps: { power: { maxPerDayPerBot: cap }, max: { maxPerDayPerBot: maxCap } },
    userTierOverride: { enabled: true, defaultRole: role },
    ...(prefs ? { userTierPrefs: { users: prefs } } : {}),
  }, "", "");
}

/** Resolve N distinct sessions, returning the model each one landed on. */
function resolveSessions(router, n, prefix = "s") {
  const out = [];
  for (let i = 0; i < n; i++) out.push(router.resolveModelOverride(`${prefix}${i}`));
  return out;
}

// ── The cap now binds the default path ──────────────────────────────────────

test("sessions under the cap get the operator default", () => {
  const r = newRouter({ cap: 3 });
  assert.deepEqual(resolveSessions(r, 3), [OPUS, OPUS, OPUS]);
  assert.equal(r.getLastDecisionDriver("s2"), "operator_default");
});

test("the session past the cap degrades power → standard", () => {
  const r = newRouter({ cap: 2 });
  assert.deepEqual(resolveSessions(r, 4), [OPUS, OPUS, SONNET, SONNET]);
});

test("a degraded turn is attributed to the cap, not to the default", () => {
  const r = newRouter({ cap: 1 });
  resolveSessions(r, 1);
  assert.equal(r.resolveModelOverride("s-late"), SONNET);
  assert.equal(r.getLastDecisionDriver("s-late"), "role_cap");
  // The tier whose budget ran out is what the user needs named, and it is
  // not the tier the turn ended up on.
  assert.equal(r.getLastCapDegradedFrom("s-late"), "power");
});

test("an uncapped default is never gated", () => {
  const r = newRouter({ role: "standard", cap: 0 });
  assert.deepEqual(resolveSessions(r, 3), [SONNET, SONNET, SONNET]);
  assert.equal(r.getLastDecisionDriver("s0"), "operator_default");
  assert.equal(r.getLastCapDegradedFrom("s0"), null);
});

test("cap 0 disables the role outright — the first session already degrades", () => {
  // 0 is the operator's "role disabled" sentinel on the chip path; the
  // default path must read it the same way.
  const r = newRouter({ cap: 0 });
  assert.equal(r.resolveModelOverride("s0"), SONNET);
  assert.equal(r.getLastDecisionDriver("s0"), "role_cap");
});

// ── Carryover: a session already in the role keeps it ───────────────────────

test("a session already in power is not evicted when the cap fills", () => {
  // The counters are transition-edge: this session was counted once, on
  // entry, and continuing it costs nothing more. Re-gating every turn would
  // evict it mid-answer against a count that includes itself.
  const r = newRouter({ cap: 1 });
  assert.equal(r.resolveModelOverride("long"), OPUS);
  // Another session exhausts nothing further (the cap is already full) and
  // degrades, but the long-running one keeps its rung across later turns.
  assert.equal(r.resolveModelOverride("other"), SONNET);
  assert.equal(r.resolveModelOverride("long"), OPUS);
  assert.equal(r.resolveModelOverride("long"), OPUS);
  assert.equal(r.getLastDecisionDriver("long"), "operator_default");
});

test("a cleared session is no longer a carryover — and does not refund the day", () => {
  const r = newRouter({ cap: 1 });
  assert.equal(r.resolveModelOverride("long"), OPUS);
  r.clearSession("long");
  // Ending a session returns its exemption, not its spend: the same key
  // starting fresh is a NEW transition, and the day's budget is already gone.
  assert.equal(r.resolveModelOverride("long"), SONNET);
  assert.equal(r.getLastDecisionDriver("long"), "role_cap");
});

test("clearSession drops the degrade record with the rest of the session", () => {
  const r = newRouter({ cap: 0 });
  assert.equal(r.resolveModelOverride("s0"), SONNET);
  assert.equal(r.getLastCapDegradedFrom("s0"), "power");
  r.clearSession("s0");
  assert.equal(r.getLastCapDegradedFrom("s0"), null);
});

// ── The degrade chain ───────────────────────────────────────────────────────

test("a per-user max default walks max → power → standard as each cap fills", () => {
  // A per-user default MAY be `max` (an explicit pull), so it is the one
  // path that can exercise more than one hop.
  const r = newRouter({ role: "standard", cap: 1, maxCap: 1,
    prefs: { "ext:telegram:1": { defaultRole: "max" } } });
  const key = (n) => `s${n}`;
  for (const n of [0, 1, 2]) r.setSessionUserKey(key(n), "ext:telegram:1");

  assert.equal(r.resolveModelOverride(key(0)), FABLE);          // max, cap 1/1
  assert.equal(r.resolveModelOverride(key(1)), OPUS);           // max full → power
  assert.equal(r.getLastCapDegradedFrom(key(1)), "max");
  assert.equal(r.resolveModelOverride(key(2)), SONNET);         // both full → standard
  assert.equal(r.getLastCapDegradedFrom(key(2)), "max");
});

// ── What the cap deliberately does NOT gate ─────────────────────────────────

test("allowBotInitiated does not gate a default — that gate is about the BOT asking", () => {
  // max is bot-initiated-blocked by default. An operator/user DEFAULT is not
  // the bot escalating itself, so the block must not apply; only the cap does.
  const r = newRouter({ role: "standard", cap: 5, maxCap: 5,
    prefs: { "ext:telegram:1": { defaultRole: "max" } } });
  r.setSessionUserKey("s0", "ext:telegram:1");
  assert.equal(r.canEscalateToRole("max").allowed, false,
    "precondition: the bot may not request max here");
  assert.equal(r.resolveModelOverride("s0"), FABLE,
    "but the user's own default still resolves");
});

// ── The ledger the cap is counted in ────────────────────────────────────────
//
// The in-process counters are seeded from
// {sharedDir}/cost/tier-usage/{botId}/{date}.jsonl at construction, so that
// append is what makes the cap survive a gateway restart. It runs as the BOT
// user and is wrapped in a no-throw try; on the reference pod the tree was
// owned by another user entirely, so it had been failing on every bot but one
// for as long as it existed. Nothing surfaced, because the warn inside the
// catch called a `logger` property ModelRouter never had — an optional chain
// on undefined, swallowing every call. The logger is now a real constructor
// argument, which is what turns that dead surface back on.

test("a failed tier-usage append warns through the gateway logger", () => {
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-tierusage-"));
  // A FILE where the ledger's parent directory belongs: mkdirSync throws
  // ENOTDIR, standing in for the EACCES a bot user really got.
  fs.writeFileSync(path.join(shared, "cost"), "not a directory");

  const warns = [];
  const r = new ModelRouter({
    ...CFG,
    roleCaps: { power: { maxPerDayPerBot: 5 } },
    userTierOverride: { enabled: true, defaultRole: "power" },
  }, shared, "team-bot-c", { warn: (m) => warns.push(String(m)) });

  assert.equal(r.resolveModelOverride("s0"), OPUS);
  assert.equal(warns.length, 1, "the first failure must warn");
  assert.match(warns[0], /FAILED to append tier-usage record/);
  assert.match(warns[0], /cap.+will UNDER-count|UNDER-count/);

  // One warn per process, not one per turn — a broken pod must not flood.
  r.resolveModelOverride("s1");
  r.resolveModelOverride("s2");
  assert.equal(warns.length, 1);
});

test("a router built without a logger still routes (the arg is optional)", () => {
  // Every pre-existing 3-arg construction — and every test above — must keep
  // working; the warn is simply not emitted.
  const shared = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-tierusage-"));
  fs.writeFileSync(path.join(shared, "cost"), "not a directory");
  const r = new ModelRouter({
    ...CFG,
    roleCaps: { power: { maxPerDayPerBot: 5 } },
    userTierOverride: { enabled: true, defaultRole: "power" },
  }, shared, "team-bot-c");
  assert.equal(r.resolveModelOverride("s0"), OPUS);
});
