/**
 * Tests for roleResolver — Phase C.4 (per-turn speaker context).
 *
 * Covers the 5-priority role-resolution chain that mirrors the Python
 * roster_overlay.resolve_role. Drift between this TS engine and the
 * Python engine would manifest as the LLM thinking the speaker is one
 * role while the daemon enforces another. The tests pin the resolution
 * rules from the Python side.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/roleResolver.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  resolveSpeakerRole,
  buildSpeakerContextBlock,
} from "../dist/util/roleResolver.js";


function mkTmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "roleresolver-"));
}


function writeNetwork(sharedDir, network) {
  fs.mkdirSync(sharedDir, { recursive: true });
  fs.writeFileSync(
    path.join(sharedDir, "network.json"),
    JSON.stringify(network, null, 2),
  );
}


function writeOverlay(sharedDir, botId, overlay) {
  const dir = path.join(sharedDir, "rosters");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, `${botId}.json`),
    JSON.stringify(overlay, null, 2),
  );
}


// ── Default resolution ────────────────────────────────────────────────


test("missing overlay AND missing network → default participant", () => {
  const sharedDir = mkTmpDir();
  const r = resolveSpeakerRole("atlas", "telegram", "111", { sharedDir });
  assert.equal(r.role, "participant");
  assert.deepEqual(r.capabilities, []);
  assert.equal(r.resolvedBy, "default_participant");
});


test("admitted identity with no claims → participant", () => {
  const sharedDir = mkTmpDir();
  writeNetwork(sharedDir, {
    pod: { admins: { external_ids: { telegram: ["999"] } } },
    bots: { atlas: {} },
  });
  writeOverlay(sharedDir, "atlas", { identities: {}, blocked: {} });
  const r = resolveSpeakerRole("atlas", "telegram", "111", { sharedDir });
  assert.equal(r.role, "participant");
});


// ── Priority chain ────────────────────────────────────────────────────


test("blocked map wins over admin claim (sticky deny)", () => {
  const sharedDir = mkTmpDir();
  writeNetwork(sharedDir, {
    pod: { admins: { external_ids: { telegram: ["999"] } } },
  });
  writeOverlay(sharedDir, "atlas", {
    blocked: { "telegram:999": { reason: "compromised" } },
  });
  const r = resolveSpeakerRole("atlas", "telegram", "999", { sharedDir });
  assert.equal(r.role, "blocked");
  assert.deepEqual(r.capabilities, []);
  assert.equal(r.resolvedBy, "blocked");
});


test("pod-admin claim wins over explicit overlay role", () => {
  const sharedDir = mkTmpDir();
  writeNetwork(sharedDir, {
    pod: { admins: { external_ids: { telegram: ["999"] } } },
  });
  writeOverlay(sharedDir, "atlas", {
    identities: { "telegram:999": { role: "participant" } },
    blocked: {},
  });
  const r = resolveSpeakerRole("atlas", "telegram", "999", { sharedDir });
  assert.equal(r.role, "admin");
  assert.ok(r.capabilities.includes("bot.roster.mutate"));
  assert.equal(r.resolvedBy, "admin_claim");
});


test("explicit overlay role wins over primary-owner default", () => {
  const sharedDir = mkTmpDir();
  writeNetwork(sharedDir, {
    bots: {
      atlas: { primary_user: { external_ids: { telegram: "222" } } },
    },
  });
  writeOverlay(sharedDir, "atlas", {
    identities: { "telegram:222": { role: "participant" } },
    blocked: {},
  });
  const r = resolveSpeakerRole("atlas", "telegram", "222", { sharedDir });
  assert.equal(r.role, "participant");
  assert.equal(r.resolvedBy, "explicit_overlay");
});


test("primary-owner claim → primary_user without explicit overlay", () => {
  const sharedDir = mkTmpDir();
  writeNetwork(sharedDir, {
    bots: {
      atlas: { primary_user: { external_ids: { telegram: "222" } } },
    },
  });
  writeOverlay(sharedDir, "atlas", { identities: {}, blocked: {} });
  const r = resolveSpeakerRole("atlas", "telegram", "222", { sharedDir });
  assert.equal(r.role, "primary_user");
  assert.ok(r.capabilities.includes("bot.roster.mutate"));
  assert.equal(r.isOwner, true);
  assert.equal(r.resolvedBy, "primary_owner");
});


test("explicit primary_user role → primary_user with that resolution", () => {
  const sharedDir = mkTmpDir();
  writeOverlay(sharedDir, "atlas", {
    identities: { "telegram:222": { role: "primary_user" } },
    blocked: {},
  });
  const r = resolveSpeakerRole("atlas", "telegram", "222", { sharedDir });
  assert.equal(r.role, "primary_user");
  assert.equal(r.resolvedBy, "explicit_overlay");
});


test("invalid overlay role string falls through to default", () => {
  // Defense: if someone hand-edited the overlay to "wizard", treat it
  // as if no explicit role were set.
  const sharedDir = mkTmpDir();
  writeOverlay(sharedDir, "atlas", {
    identities: { "telegram:222": { role: "wizard" } },
    blocked: {},
  });
  const r = resolveSpeakerRole("atlas", "telegram", "222", { sharedDir });
  assert.equal(r.role, "participant");
});


// ── Capability shape ──────────────────────────────────────────────────


test("admin capabilities match the documented set", () => {
  const sharedDir = mkTmpDir();
  writeNetwork(sharedDir, {
    pod: { admins: { external_ids: { telegram: ["999"] } } },
  });
  const r = resolveSpeakerRole("atlas", "telegram", "999", { sharedDir });
  // Pin the shape — drift here means an admin loses a capability
  // mid-deploy and gets surprise refusals.
  assert.ok(r.capabilities.includes("bot.roster.read"));
  assert.ok(r.capabilities.includes("bot.roster.mutate"));
  assert.ok(r.capabilities.includes("bot.roles.bind"));
  assert.ok(r.capabilities.includes("bot.channel.config"));
  assert.ok(r.capabilities.includes("bot.config.modify"));
  assert.ok(r.capabilities.includes("bot.app.install"));
  assert.ok(r.capabilities.includes("bot.code.modify"));
  assert.ok(r.capabilities.includes("bot.send_external"));
});


test("primary_user excludes the pod-level capabilities", () => {
  const sharedDir = mkTmpDir();
  writeNetwork(sharedDir, {
    bots: { atlas: { primary_user: { external_ids: { telegram: "222" } } } },
  });
  const r = resolveSpeakerRole("atlas", "telegram", "222", { sharedDir });
  // Pinned excludes — primary_user must NOT have these.
  assert.equal(r.capabilities.includes("bot.roles.bind"), false);
  assert.equal(r.capabilities.includes("bot.app.install"), false);
  assert.equal(r.capabilities.includes("bot.code.modify"), false);
  // But MUST have the bot-scoped ones.
  assert.ok(r.capabilities.includes("bot.roster.mutate"));
  assert.ok(r.capabilities.includes("bot.channel.config"));
});


// ── Context block rendering ──────────────────────────────────────────


test("admin speaker block tells the LLM to invoke the tools", () => {
  const resolution = {
    role: "admin",
    capabilities: ["bot.roster.mutate", "bot.channel.config"],
    isOwner: false,
    resolvedBy: "admin_claim",
  };
  const block = buildSpeakerContextBlock(resolution, {
    platform: "telegram", stableId: "999",
  });
  assert.match(block, /role=admin/);
  assert.match(block, /can_mutate_roster=yes/);
  assert.match(block, /invoke the matching roster_/);
  assert.equal(block.includes("decline politely"), false);
});


test("participant speaker block tells the LLM to decline", () => {
  const resolution = {
    role: "participant",
    capabilities: [],
    isOwner: false,
    resolvedBy: "default_participant",
  };
  const block = buildSpeakerContextBlock(resolution, {
    platform: "telegram", stableId: "111",
  });
  assert.match(block, /role=participant/);
  assert.match(block, /can_mutate_roster=no/);
  assert.match(block, /decline politely/);
  assert.equal(block.includes("invoke the matching"), false);
});


test("blocked speaker block matches participant shape (no special-case)", () => {
  // Blocked identities can't reach the bot anyway (channel-ingress
  // gate denies). If they somehow do (e.g. transient inconsistency),
  // the block tells the LLM they have no permissions.
  const resolution = {
    role: "blocked",
    capabilities: [],
    isOwner: false,
    resolvedBy: "blocked",
  };
  const block = buildSpeakerContextBlock(resolution, {
    platform: "telegram", stableId: "555",
  });
  assert.match(block, /role=blocked/);
  assert.match(block, /can_mutate_roster=no/);
  assert.match(block, /decline politely/);
});


test("block includes display name when supplied", () => {
  const resolution = {
    role: "admin",
    capabilities: ["bot.roster.mutate"],
    isOwner: false,
    resolvedBy: "admin_claim",
  };
  const block = buildSpeakerContextBlock(resolution, {
    platform: "telegram", stableId: "999", displayName: "Pod-Admin Person",
  });
  assert.match(block, /Pod-Admin Person \(telegram:999\)/);
});
