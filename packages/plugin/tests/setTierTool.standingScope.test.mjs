/**
 * Tests for SetTierTool `scope: "standing"` — the bot-invocable per-user
 * standing tier default (G4 of the spec-user-tier-control 2026-08-03
 * addendum).
 *
 * "Always use the good model for our chats" must land in the SAME
 * ``{sharedDir}/{botId}/user-tier-prefs.json`` entry the admin-side
 * ``evo tier-default`` handler writes (evolve_admin/evo/user_tier_prefs.py)
 * — never in a session pin. Covered here:
 *
 *   • happy path (incl. `max` — a per-user standing default is an
 *     explicit pull and skips the bot-initiated session gate)
 *   • `auto` deletes the caller's entry (no tombstones)
 *   • refusal when the session has no pinned user_key (heartbeat /
 *     internal turns must never guess an identity)
 *   • file-format round-trip against a hand-written admin-side-shaped
 *     file (indent=2, sort_keys, legacy defaultTier entries preserved)
 *   • in-memory effect without a reload — the next routing decision
 *     sees the new default
 *   • loud failure (with the `evo tier-default` fallback instruction)
 *     when the write hits permissions
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/setTierTool.standingScope.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { createSetTierToolFactory } from "../dist/tools/SetTierTool.js";
import { ModelRouter } from "../dist/observer/ModelRouter.js";

const BOT_ID = "team_bot_a";

// Rungs/roles config with a max rung so the explicit-pull path is real.
const RUNGS_CFG = {
  rungs: [
    { id: "haiku-class", models: ["grunt/model"], costClass: "low" },
    { id: "sonnet-class", models: ["workhorse/model"], costClass: "medium" },
    { id: "opus-class", models: ["power/model"], costClass: "high" },
    { id: "fable-class", models: ["frontier/model"], costClass: "premium" },
  ],
  roles: {
    fast: "haiku-class",
    standard: "sonnet-class",
    power: "opus-class",
    max: "fable-class",
  },
  routing: { enabled: true },
};

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

/** Fresh tmp sharedDir with the per-bot subdir pre-created (deploy.py
 * pre-creates it on real pods). */
function newSharedDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "standing-tier-"));
  fs.mkdirSync(path.join(dir, BOT_ID), { recursive: true });
  return dir;
}

function prefsPath(sharedDir) {
  return path.join(sharedDir, BOT_ID, "user-tier-prefs.json");
}

function buildTool({ sharedDir, cfg = RUNGS_CFG, userKey, sessionKey = "sess-1" } = {}) {
  const router = new ModelRouter(cfg, sharedDir ?? "", sharedDir ? BOT_ID : "");
  if (userKey) router.setSessionUserKey(sessionKey, userKey);
  const logger = fakeLogger();
  const factory = createSetTierToolFactory(
    { botId: BOT_ID, modelRouter: router },
    logger,
  );
  const tool = factory({ sessionKey });
  return { tool, router, logger };
}

function parseResult(toolReturn) {
  if (toolReturn.isError) {
    return { isError: true, text: toolReturn.content[0].text };
  }
  return { isError: false, ...JSON.parse(toolReturn.content[0].text) };
}

/** Deep key-sort mirror of the writers' sort_keys serialization. */
function sortKeysDeep(v) {
  if (Array.isArray(v)) return v.map(sortKeysDeep);
  if (v && typeof v === "object") {
    const out = {};
    for (const k of Object.keys(v).sort()) out[k] = sortKeysDeep(v[k]);
    return out;
  }
  return v;
}

// ── Schema ──────────────────────────────────────────────────────────────────

test("schema exposes the optional scope parameter", () => {
  const { tool } = buildTool({ userKey: "ext:telegram:alice", sharedDir: newSharedDir() });
  const props = tool.parameters.properties;
  assert.ok(props.scope, "scope parameter missing from schema");
  // The description must teach the routing rule (session vs standing).
  assert.match(tool.description, /standing/i);
  assert.match(tool.description, /always \/ from now on/i);
});

test("unknown scope is rejected", async () => {
  const { tool } = buildTool({ userKey: "ext:telegram:alice", sharedDir: newSharedDir() });
  const result = parseResult(
    await tool.execute("c1", { choice: "power", scope: "forever" }),
  );
  assert.equal(result.isError, true);
  assert.match(result.text, /unknown scope/);
});

// ── Happy path ──────────────────────────────────────────────────────────────

test("standing power writes the per-user entry in admin-side shape", async () => {
  const sharedDir = newSharedDir();
  const { tool } = buildTool({ sharedDir, userKey: "ext:telegram:alice" });
  const result = parseResult(
    await tool.execute("c1", { choice: "power", scope: "standing" }),
  );
  assert.equal(result.isError, false);
  assert.equal(result.ok, true);
  assert.equal(result.applied_choice, "power");
  assert.equal(result.scope, "standing");
  assert.match(result.ack_hint, /all their future conversations/);

  const data = JSON.parse(fs.readFileSync(prefsPath(sharedDir), "utf8"));
  const entry = data.users["ext:telegram:alice"];
  assert.ok(entry, "entry not written");
  assert.equal(entry.defaultRole, "power");
  // Timestamp format matches the Python writer: seconds precision, +00:00.
  assert.match(entry.updated_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$/);
  // Entry carries EXACTLY the admin writer's key names.
  assert.deepEqual(Object.keys(entry).sort(), ["defaultRole", "updated_at"]);
});

test("standing max is allowed (explicit pull) even though session max is bot-blocked", async () => {
  const sharedDir = newSharedDir();
  const { tool, router } = buildTool({ sharedDir, userKey: "ext:slack:bob" });

  // Sanity: on this same router, a SESSION-scoped max degrades (bot-
  // initiated max is blocked by default) …
  const sess = parseResult(await tool.execute("c1", { choice: "max" }));
  assert.equal(sess.requested_choice, "max");
  assert.notEqual(sess.applied_choice, "max");

  // … but the STANDING default may be max, matching `evo tier-default max`.
  const result = parseResult(
    await tool.execute("c2", { choice: "max", scope: "standing" }),
  );
  assert.equal(result.isError, false);
  assert.equal(result.applied_choice, "max");
  assert.equal(result.scope, "standing");
  const data = JSON.parse(fs.readFileSync(prefsPath(sharedDir), "utf8"));
  assert.equal(data.users["ext:slack:bob"].defaultRole, "max");

  // And the standing max actually routes to the frontier rung for a
  // fresh session of the same user.
  router.setSessionUserKey("sess-9", "ext:slack:bob");
  assert.equal(router.resolveModelOverride("sess-9"), "frontier/model");
});

test("standing auto deletes the caller's entry, preserves others", async () => {
  const sharedDir = newSharedDir();
  fs.writeFileSync(
    prefsPath(sharedDir),
    JSON.stringify(sortKeysDeep({
      users: {
        "ext:telegram:alice": { defaultRole: "power", updated_at: "2026-08-01T10:00:00+00:00" },
        "ext:slack:bob": { defaultRole: "fast", updated_at: "2026-08-01T11:00:00+00:00" },
      },
    }), null, 2),
  );
  const { tool } = buildTool({ sharedDir, userKey: "ext:telegram:alice" });
  const result = parseResult(
    await tool.execute("c1", { choice: "auto", scope: "standing" }),
  );
  assert.equal(result.isError, false);
  assert.equal(result.scope, "standing");
  assert.match(result.ack_hint, /cleared/i);

  const data = JSON.parse(fs.readFileSync(prefsPath(sharedDir), "utf8"));
  assert.equal(data.users["ext:telegram:alice"], undefined);
  assert.equal(data.users["ext:slack:bob"].defaultRole, "fast");
  // File keeps the canonical {users: {...}} shape.
  assert.ok(data.users && typeof data.users === "object");
});

// ── Refusals ────────────────────────────────────────────────────────────────

test("no pinned user_key → refuses, nothing written, suggests evo tier-default", async () => {
  const sharedDir = newSharedDir();
  const { tool } = buildTool({ sharedDir /* no userKey */ });
  const result = parseResult(
    await tool.execute("c1", { choice: "power", scope: "standing" }),
  );
  assert.equal(result.isError, true);
  assert.match(result.text, /no pinned user identity/);
  assert.match(result.text, /evo tier-default power/);
  assert.equal(fs.existsSync(prefsPath(sharedDir)), false);
});

test("userTierOverride.enabled=false refuses standing writes (admin-handler parity)", async () => {
  const sharedDir = newSharedDir();
  const cfg = { ...RUNGS_CFG, userTierOverride: { enabled: false } };
  const { tool } = buildTool({ sharedDir, cfg, userKey: "ext:telegram:alice" });
  const result = parseResult(
    await tool.execute("c1", { choice: "fast", scope: "standing" }),
  );
  assert.equal(result.isError, true);
  assert.match(result.text, /disabled/);
  assert.equal(fs.existsSync(prefsPath(sharedDir)), false);
});

// ── File-format round-trip vs the admin-side writer ────────────────────────

test("round-trips an admin-side-shaped file: legacy + unknown fields preserved, sorted 2-space output", async () => {
  const sharedDir = newSharedDir();
  // Hand-written byte shape of the Python writer:
  //   json.dumps(data, indent=2, sort_keys=True) — no trailing newline.
  // bob is a pre-migration legacy entry (defaultTier); the top-level
  // extra key mimics forward-compat fields another writer may add.
  const adminShaped =
    '{\n' +
    '  "schema_note": "written by evo tier-default",\n' +
    '  "users": {\n' +
    '    "ext:slack:bob": {\n' +
    '      "defaultTier": "fast",\n' +
    '      "updated_at": "2026-08-01T09:00:00+00:00"\n' +
    '    }\n' +
    '  }\n' +
    '}';
  fs.writeFileSync(prefsPath(sharedDir), adminShaped);

  const { tool } = buildTool({ sharedDir, userKey: "ext:telegram:alice" });
  const result = parseResult(
    await tool.execute("c1", { choice: "standard", scope: "standing" }),
  );
  assert.equal(result.isError, false);

  const text = fs.readFileSync(prefsPath(sharedDir), "utf8");
  const data = JSON.parse(text);
  // Other users' entries — including the legacy defaultTier key — and
  // unknown top-level fields survive untouched.
  assert.deepEqual(data.users["ext:slack:bob"], {
    defaultTier: "fast",
    updated_at: "2026-08-01T09:00:00+00:00",
  });
  assert.equal(data.schema_note, "written by evo tier-default");
  assert.equal(data.users["ext:telegram:alice"].defaultRole, "standard");
  // Output serialization matches the Python writer's format: recursively
  // sorted keys, indent 2, no trailing newline.
  assert.equal(text, JSON.stringify(sortKeysDeep(data), null, 2));
});

// ── In-memory effect (no reload) ────────────────────────────────────────────

test("standing write takes effect on the next routing decision without reload", async () => {
  const sharedDir = newSharedDir();
  const { tool, router } = buildTool({ sharedDir, userKey: "ext:telegram:alice" });

  // Before: no per-user default — no override resolves.
  router.setSessionUserKey("sess-2", "ext:telegram:alice");
  assert.equal(router.resolveModelOverride("sess-2"), null);

  await tool.execute("c1", { choice: "power", scope: "standing" });

  // After: the SAME router instance (no reloadConfig call) routes the
  // user's sessions to the power rung.
  assert.equal(router.resolveModelOverride("sess-2"), "power/model");
  assert.equal(router.getLastDecisionDriver("sess-2"), "user_default");
});

test("standing scope does NOT pin the session (session override stays empty)", async () => {
  const sharedDir = newSharedDir();
  const { tool, router } = buildTool({ sharedDir, userKey: "ext:telegram:alice" });
  await tool.execute("c1", { choice: "fast", scope: "standing" });
  // No session-scoped user override was set — the effect rides the
  // per-user default (level 4a), not the level-2 session pin.
  assert.equal(router.getUserTier("sess-1"), null);
});

// ── Loud permission failure ─────────────────────────────────────────────────

test("unwritable per-bot dir → loud error with evo tier-default fallback", async (t) => {
  if (process.getuid && process.getuid() === 0) {
    t.skip("running as root — chmod-based EACCES cannot be simulated");
    return;
  }
  const sharedDir = newSharedDir();
  const botDir = path.join(sharedDir, BOT_ID);
  fs.chmodSync(botDir, 0o555);
  try {
    const { tool, logger } = buildTool({ sharedDir, userKey: "ext:telegram:alice" });
    const result = parseResult(
      await tool.execute("c1", { choice: "power", scope: "standing" }),
    );
    assert.equal(result.isError, true);
    assert.match(result.text, /standing default write failed/);
    assert.match(result.text, /Nothing was saved/);
    assert.match(result.text, /evo tier-default power/);
    // And it logged the failure for the operator.
    assert.ok(logger.records.warn.some((m) => /standing write FAILED/.test(m)));
  } finally {
    fs.chmodSync(botDir, 0o755);
  }
});

test("existing-but-unreadable prefs file → refuses rather than clobbering", async (t) => {
  if (process.getuid && process.getuid() === 0) {
    t.skip("running as root — chmod-based EACCES cannot be simulated");
    return;
  }
  const sharedDir = newSharedDir();
  fs.writeFileSync(prefsPath(sharedDir), '{"users": {"ext:slack:bob": {"defaultRole": "fast"}}}');
  fs.chmodSync(prefsPath(sharedDir), 0o000);
  try {
    const { tool } = buildTool({ sharedDir, userKey: "ext:telegram:alice" });
    const result = parseResult(
      await tool.execute("c1", { choice: "power", scope: "standing" }),
    );
    assert.equal(result.isError, true);
    assert.match(result.text, /refusing to overwrite/);
  } finally {
    fs.chmodSync(prefsPath(sharedDir), 0o644);
  }
  // The original content survived.
  const data = JSON.parse(fs.readFileSync(prefsPath(sharedDir), "utf8"));
  assert.equal(data.users["ext:slack:bob"].defaultRole, "fast");
});

// ── Session scope unchanged ─────────────────────────────────────────────────

test("scope omitted → session behavior, no prefs file written", async () => {
  const sharedDir = newSharedDir();
  const { tool, router } = buildTool({ sharedDir, userKey: "ext:telegram:alice" });
  const result = parseResult(await tool.execute("c1", { choice: "power" }));
  assert.equal(result.isError, false);
  assert.equal(result.scope, "session");
  assert.equal(router.resolveModelOverride("sess-1"), "power/model");
  assert.equal(fs.existsSync(prefsPath(sharedDir)), false);
});
