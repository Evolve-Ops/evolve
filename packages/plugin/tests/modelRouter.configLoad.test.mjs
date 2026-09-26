/**
 * Tests for ModelRouter's tier-config file lookup.
 *
 * The bug this pins:
 *   Pre-2026-05-28 the plugin read {sharedDir}/{botId}/tiers.json, but
 *   the admin UI's AI Optimization page wrote ~/.openclaw/evolve-tiers.json.
 *   Two files, same shape, plugin never saw the operator's configuration.
 *   Every tier-lookup returned null and every routing decision silently
 *   no-op'd to the bot default model.
 *
 * The contract this enforces:
 *   1. ~/.openclaw/evolve-tiers.json is the canonical location (admin UI
 *      writes here)
 *   2. {sharedDir}/{botId}/tiers.json is a legacy fallback (preserves
 *      back-compat for hand-rolled pods)
 *   3. ~/ wins when both files exist (UI canonical beats legacy)
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.configLoad.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

/**
 * Stage a fake HOME so the test can write `.openclaw/evolve-tiers.json`
 * without touching the real home dir. Returns a teardown callback.
 *
 * Node's `os.homedir()` reads from $HOME on POSIX, so just point it at
 * a tmp tree.
 */
function _withFakeHome(content) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "mr-cfg-home-"));
  const realHome = process.env.HOME;
  process.env.HOME = home;
  if (content !== undefined) {
    const ocDir = path.join(home, ".openclaw");
    fs.mkdirSync(ocDir, { recursive: true });
    fs.writeFileSync(
      path.join(ocDir, "evolve-tiers.json"),
      JSON.stringify(content),
    );
  }
  return () => {
    if (realHome === undefined) {
      delete process.env.HOME;
    } else {
      process.env.HOME = realHome;
    }
    fs.rmSync(home, { recursive: true, force: true });
  };
}

function _mkSharedDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "mr-cfg-shared-"));
}

const TIER_FIXTURE = {
  rungs: [
    { id: "haiku-class", models: ["grunt/v1"], costClass: "low" },
    { id: "sonnet-class", models: ["workhorse/v1"], costClass: "medium" },
    { id: "opus-class", models: ["power/v1"], costClass: "high" },
  ],
  roles: {
    fast: "haiku-class",
    standard: "sonnet-class",
    power: "opus-class",
  },
  routing: { enabled: true },
};

const LEGACY_FIXTURE = {
  rungs: [
    { id: "haiku-class", models: ["legacy-grunt/v0"], costClass: "low" },
    { id: "sonnet-class", models: ["legacy-workhorse/v0"], costClass: "medium" },
    { id: "opus-class", models: ["legacy-power/v0"], costClass: "high" },
  ],
  roles: {
    fast: "haiku-class",
    standard: "sonnet-class",
    power: "opus-class",
  },
  routing: { enabled: true },
};

// ── Path resolution ─────────────────────────────────────────────────────────

test("reloadConfig: reads ~/.openclaw/evolve-tiers.json when present", () => {
  const teardown = _withFakeHome(TIER_FIXTURE);
  const shared = _mkSharedDir();
  try {
    fs.writeFileSync(path.join(shared, "network.json"), "{}");
    const r = new ModelRouter({ rungs: [], roles: {}, routing: { enabled: true } }, shared, "team_bot_a");
    r.reloadConfig(path.join(shared, "network.json"), undefined, shared, "team_bot_a");

    // Verify by exercising a tier lookup. setUserTier("power") routes to
    // tier1; tier1's first model should be "power/v1" per the fixture.
    r.setUserTier("s1", "power");
    assert.equal(r.resolveModelOverride("s1"), "power/v1");
  } finally {
    teardown();
    fs.rmSync(shared, { recursive: true, force: true });
  }
});

test("reloadConfig: falls back to {sharedDir}/{botId}/tiers.json when home file absent", () => {
  const teardown = _withFakeHome(undefined);  // no home file
  const shared = _mkSharedDir();
  try {
    fs.writeFileSync(path.join(shared, "network.json"), "{}");
    const botDir = path.join(shared, "team_bot_a");
    fs.mkdirSync(botDir, { recursive: true });
    fs.writeFileSync(path.join(botDir, "tiers.json"), JSON.stringify(LEGACY_FIXTURE));

    const r = new ModelRouter({ rungs: [], roles: {}, routing: { enabled: true } }, shared, "team_bot_a");
    r.reloadConfig(path.join(shared, "network.json"), undefined, shared, "team_bot_a");

    r.setUserTier("s1", "power");
    assert.equal(r.resolveModelOverride("s1"), "legacy-power/v0");
  } finally {
    teardown();
    fs.rmSync(shared, { recursive: true, force: true });
  }
});

test("reloadConfig: ~/.openclaw/evolve-tiers.json wins over legacy file", () => {
  // Both files exist with conflicting content. Canonical (home) must win.
  // Otherwise an operator who configures via UI but has a leftover legacy
  // file gets bimodal behavior depending on which path the plugin picks.
  const teardown = _withFakeHome(TIER_FIXTURE);
  const shared = _mkSharedDir();
  try {
    fs.writeFileSync(path.join(shared, "network.json"), "{}");
    const botDir = path.join(shared, "team_bot_a");
    fs.mkdirSync(botDir, { recursive: true });
    fs.writeFileSync(path.join(botDir, "tiers.json"), JSON.stringify(LEGACY_FIXTURE));

    const r = new ModelRouter({ rungs: [], roles: {}, routing: { enabled: true } }, shared, "team_bot_a");
    r.reloadConfig(path.join(shared, "network.json"), undefined, shared, "team_bot_a");

    r.setUserTier("s1", "power");
    // Canonical (UI-written home file) wins, not legacy.
    assert.equal(r.resolveModelOverride("s1"), "power/v1");
  } finally {
    teardown();
    fs.rmSync(shared, { recursive: true, force: true });
  }
});

test("reloadConfig: neither file → empty config, fall through to network.json", () => {
  const teardown = _withFakeHome(undefined);
  const shared = _mkSharedDir();
  try {
    fs.writeFileSync(path.join(shared, "network.json"), JSON.stringify({
      models: {
        rungs: [
          { id: "haiku-class", models: ["network-grunt/v0"], costClass: "low" },
          { id: "sonnet-class", models: ["network-workhorse/v0"], costClass: "medium" },
          { id: "opus-class", models: ["network-power/v0"], costClass: "high" },
        ],
        roles: {
          fast: "haiku-class",
          standard: "sonnet-class",
          power: "opus-class",
        },
        routing: { enabled: true },
      },
    }));

    const r = new ModelRouter({ rungs: [], roles: {}, routing: { enabled: true } }, shared, "team_bot_a");
    r.reloadConfig(path.join(shared, "network.json"), undefined, shared, "team_bot_a");

    r.setUserTier("s1", "power");
    assert.equal(r.resolveModelOverride("s1"), "network-power/v0");
  } finally {
    teardown();
    fs.rmSync(shared, { recursive: true, force: true });
  }
});

test("reloadConfig: nothing configured anywhere → roles resolve from the code defaults (Phase 6)", () => {
  // Pre-Phase-6 this returned null (silent no-op → OC bot default). Post
  // spec §Addendum 2, DEFAULT_MODEL_CATALOG ships in code as the base layer,
  // so EVERY role resolves even with no pod/bot config — Max ships armed, not
  // dormant. A bare pod's `power` role resolves to the default opus-class.
  const teardown = _withFakeHome(undefined);
  const shared = _mkSharedDir();
  try {
    fs.writeFileSync(path.join(shared, "network.json"), "{}");
    const r = new ModelRouter({ rungs: [], roles: {}, routing: { enabled: true } }, shared, "team_bot_a");
    r.reloadConfig(path.join(shared, "network.json"), undefined, shared, "team_bot_a");

    // power → opus-class (default); max → fable-class (default) — both armed.
    assert.equal(r.resolveRoleToModel("power"), "anthropic/claude-opus-4-8");
    assert.equal(r.resolveRoleToModel("max"), "anthropic/claude-fable-5");
  } finally {
    teardown();
    fs.rmSync(shared, { recursive: true, force: true });
  }
});

test("reloadConfig: explicit tiersJsonPath override still wins for tests/programmatic use", () => {
  // The reloadConfig signature still accepts an explicit path for
  // tests + future programmatic reloads (e.g., admin server hot-
  // reload triggers). That path must beat both home and legacy.
  const teardown = _withFakeHome(TIER_FIXTURE);
  const shared = _mkSharedDir();
  const explicit = _mkSharedDir();
  try {
    fs.writeFileSync(path.join(shared, "network.json"), "{}");
    const explicitFile = path.join(explicit, "explicit-tiers.json");
    fs.writeFileSync(explicitFile, JSON.stringify({
      rungs: [
        { id: "haiku-class", models: ["explicit-grunt/v0"], costClass: "low" },
        { id: "sonnet-class", models: ["explicit-workhorse/v0"], costClass: "medium" },
        { id: "opus-class", models: ["explicit-power/v0"], costClass: "high" },
      ],
      roles: {
        fast: "haiku-class",
        standard: "sonnet-class",
        power: "opus-class",
      },
      routing: { enabled: true },
    }));

    const r = new ModelRouter({ rungs: [], roles: {}, routing: { enabled: true } }, shared, "team_bot_a");
    r.reloadConfig(path.join(shared, "network.json"), explicitFile, shared, "team_bot_a");

    r.setUserTier("s1", "power");
    assert.equal(r.resolveModelOverride("s1"), "explicit-power/v0");
  } finally {
    teardown();
    fs.rmSync(shared, { recursive: true, force: true });
    fs.rmSync(explicit, { recursive: true, force: true });
  }
});

// ── cascade flag preservation across path migration ─────────────────────────


test("reloadConfig: cascade.enabled in home file gets picked up", () => {
  // The Phase 3 cascade-enabled flag (cascade.enabled: true) must
  // survive the path change. Operators flip this in evolve-tiers.json
  // via the AI Optimization page — losing it on path migration would
  // be a real regression.
  const fixture = {
    ...TIER_FIXTURE,
    cascade: { enabled: true },
  };
  const teardown = _withFakeHome(fixture);
  const shared = _mkSharedDir();
  try {
    fs.writeFileSync(path.join(shared, "network.json"), "{}");
    const r = new ModelRouter({ rungs: [], roles: {}, routing: { enabled: true } }, shared, "team_bot_a");
    r.reloadConfig(path.join(shared, "network.json"), undefined, shared, "team_bot_a");
    assert.equal(r.isCascadeEnabled(), true);
  } finally {
    teardown();
    fs.rmSync(shared, { recursive: true, force: true });
  }
});
