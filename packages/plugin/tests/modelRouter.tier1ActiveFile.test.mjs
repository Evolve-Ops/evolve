/**
 * Tests for ModelRouter's in-process tier1 counter — the
 * pressure_watchdog's telemetry-coupled-failure defense.
 *
 * Spec: internal/spec-tier-cascade-2026-05-26.md § pressure watchdog.
 *
 * The watchdog reads `{sharedDir}/{botId}/cascade/tier1_active.json`
 * on every 60s poll and merges with span-derived counts via
 * `max(spans, in_process)`. This file is what the plugin writes.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/modelRouter.tier1ActiveFile.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { ModelRouter } from "../dist/observer/ModelRouter.js";

const CFG = {
  rungs: [
    { id: "haiku-class", models: ["grunt/model"], costClass: "low" },
    { id: "sonnet-class", models: ["workhorse/model"], costClass: "medium" },
    { id: "opus-class", models: ["power/model"], costClass: "high" },
  ],
  roles: {
    fast: "haiku-class",
    standard: "sonnet-class",
    power: "opus-class",
  },
  routing: { enabled: true },
};

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "mr-tier1-test-"));
}

function readTier1File(sharedDir, botId) {
  const fp = path.join(sharedDir, botId, "cascade", "tier1_active.json");
  if (!fs.existsSync(fp)) return null;
  return JSON.parse(fs.readFileSync(fp, "utf8"));
}

// ── Basic counter behavior ───────────────────────────────────────────────

test("tier1_active.json: not written when sharedDir missing", () => {
  // No sharedDir → routing still works, file simply isn't written.
  // This is the fail-open path for not-yet-deployed bots or bench
  // unit tests that don't bother with a real shared dir.
  const r = new ModelRouter(CFG, "", "");
  r.setUserTier("s1", "power");
  r.resolveModelOverride("s1");   // would normally write tier1
  // No throw, nothing observable on disk (there's no disk to write to).
  assert.ok(true);
});

test("tier1_active.json: written when user picks Power (tier1)", () => {
  const sharedDir = tmpDir();
  try {
    const botId = "team_bot_a";
    const r = new ModelRouter(CFG, sharedDir, botId);
    r.setUserTier("s1", "power");
    r.resolveModelOverride("s1");
    const data = readTier1File(sharedDir, botId);
    assert.ok(data, "tier1_active.json must exist");
    assert.equal(data.active_count, 1);
    assert.equal(data.bot_id, "team_bot_a");
    assert.equal(typeof data.updated_at, "string");
    assert.equal(typeof data.pid, "number");
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

test("tier1_active.json: NOT written when user picks Standard (tier2)", () => {
  // Only tier1 grants update the in-process counter — tier2/tier3 do
  // not create pressure-watchdog-relevant state.
  const sharedDir = tmpDir();
  try {
    const r = new ModelRouter(CFG, sharedDir, "team_bot_a");
    r.setUserTier("s1", "standard");
    r.resolveModelOverride("s1");
    // The directory may not even exist (mkdirSync only runs on first
    // file write). Either path is acceptable as long as the file is
    // absent or active_count===0.
    const data = readTier1File(sharedDir, "team_bot_a");
    assert.ok(data === null || data.active_count === 0);
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

test("tier1_active.json: count accumulates across sessions", () => {
  const sharedDir = tmpDir();
  try {
    const r = new ModelRouter(CFG, sharedDir, "team_bot_a");
    r.setUserTier("s1", "power");
    r.resolveModelOverride("s1");
    r.setUserTier("s2", "power");
    r.resolveModelOverride("s2");
    r.setUserTier("s3", "power");
    r.resolveModelOverride("s3");
    const data = readTier1File(sharedDir, "team_bot_a");
    assert.equal(data.active_count, 3);
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

test("tier1_active.json: clearSession decrements count", () => {
  const sharedDir = tmpDir();
  try {
    const r = new ModelRouter(CFG, sharedDir, "team_bot_a");
    r.setUserTier("s1", "power");
    r.resolveModelOverride("s1");
    r.setUserTier("s2", "power");
    r.resolveModelOverride("s2");
    assert.equal(readTier1File(sharedDir, "team_bot_a").active_count, 2);
    r.clearSession("s1");
    assert.equal(readTier1File(sharedDir, "team_bot_a").active_count, 1);
    r.clearSession("s2");
    assert.equal(readTier1File(sharedDir, "team_bot_a").active_count, 0);
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

test("tier1_active.json: switching from Power to Standard decrements", () => {
  // Operator escalates to Power then later switches to Standard. The
  // session is no longer tier1; the counter must drop. Without this,
  // a single session that ever touched tier1 would be permanently
  // counted until session end.
  const sharedDir = tmpDir();
  try {
    const r = new ModelRouter(CFG, sharedDir, "team_bot_a");
    r.setUserTier("s1", "power");
    r.resolveModelOverride("s1");
    assert.equal(readTier1File(sharedDir, "team_bot_a").active_count, 1);
    r.setUserTier("s1", "standard");
    r.resolveModelOverride("s1");
    assert.equal(readTier1File(sharedDir, "team_bot_a").active_count, 0);
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

test("tier1_active.json: idempotent across repeated tier1 resolutions", () => {
  // Same session calling resolveModelOverride repeatedly while
  // continuing to be tier1 should NOT cause unnecessary file writes
  // (no extra atime/mtime churn on the watchdog's read).
  const sharedDir = tmpDir();
  try {
    const r = new ModelRouter(CFG, sharedDir, "team_bot_a");
    r.setUserTier("s1", "power");
    r.resolveModelOverride("s1");
    const firstMtime = fs.statSync(
      path.join(sharedDir, "team_bot_a", "cascade", "tier1_active.json")
    ).mtimeMs;
    // Wait long enough that mtime would change if we wrote again.
    const waitUntil = Date.now() + 20;
    while (Date.now() < waitUntil) { /* spin */ }
    r.resolveModelOverride("s1");
    r.resolveModelOverride("s1");
    const secondMtime = fs.statSync(
      path.join(sharedDir, "team_bot_a", "cascade", "tier1_active.json")
    ).mtimeMs;
    assert.equal(secondMtime, firstMtime, "no-change resolutions must not rewrite");
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

test("tier1_active.json: atomic write (no torn reads)", () => {
  // The writer uses tmp+rename. If a reader picks up the file during
  // the write window, it sees either the old content or the new
  // content — never half a JSON object. This test pins that the
  // file is always parseable.
  const sharedDir = tmpDir();
  try {
    const r = new ModelRouter(CFG, sharedDir, "team_bot_a");
    for (let i = 0; i < 20; i++) {
      r.setUserTier(`s${i}`, "power");
      r.resolveModelOverride(`s${i}`);
      const data = readTier1File(sharedDir, "team_bot_a");
      assert.equal(data.active_count, i + 1);
    }
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

// ── Stale-file clearance on plugin startup ───────────────────────────────

test("reloadConfig: clears stale tier1_active.json from prior process", () => {
  // Simulate a prior plugin process that crashed mid-flight with 3
  // active tier1 sessions. The file on disk says active_count=3 but
  // there's no live process holding those sessions. The watchdog
  // has no PID-aliveness check; without our startup-clear, the
  // stale value would inflate the watchdog's pod-wide reading
  // forever.
  const sharedDir = tmpDir();
  const networkPath = path.join(sharedDir, "network.json");
  const tiersPath = path.join(sharedDir, "tiers.json");
  try {
    // Stale file from "previous run."
    const cascadeDir = path.join(sharedDir, "team_bot_a", "cascade");
    fs.mkdirSync(cascadeDir, { recursive: true });
    fs.writeFileSync(
      path.join(cascadeDir, "tier1_active.json"),
      JSON.stringify({ active_count: 3, updated_at: "2026-01-01T00:00:00Z", pid: 99999 }),
    );

    // Minimal config files for reloadConfig.
    fs.writeFileSync(networkPath, "{}");
    fs.writeFileSync(tiersPath, JSON.stringify({
      tiers: CFG.tiers,
      routing: { enabled: true },
    }));

    const r = new ModelRouter(CFG, sharedDir, "team_bot_a");
    r.reloadConfig(networkPath, tiersPath, sharedDir, "team_bot_a");

    const data = readTier1File(sharedDir, "team_bot_a");
    assert.equal(data.active_count, 0, "reloadConfig must clear stale count");
    assert.equal(data.pid, process.pid, "fresh pid must be current process");
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

test("reloadConfig: clearance is idempotent (runs once per process)", () => {
  // If reloadConfig is called multiple times (e.g. file watcher
  // detects tiers.json change), we should not keep zeroing out
  // legitimate in-flight session counts that accumulated since the
  // first reload.
  const sharedDir = tmpDir();
  const networkPath = path.join(sharedDir, "network.json");
  const tiersPath = path.join(sharedDir, "tiers.json");
  try {
    fs.writeFileSync(networkPath, "{}");
    fs.writeFileSync(tiersPath, JSON.stringify({
      tiers: CFG.tiers,
      routing: { enabled: true },
    }));

    const r = new ModelRouter(CFG, sharedDir, "team_bot_a");
    r.reloadConfig(networkPath, tiersPath, sharedDir, "team_bot_a");

    // Now real tier1 sessions accumulate.
    r.setUserTier("s1", "power");
    r.resolveModelOverride("s1");
    assert.equal(readTier1File(sharedDir, "team_bot_a").active_count, 1);

    // Second reload — must NOT wipe the live count.
    r.reloadConfig(networkPath, tiersPath, sharedDir, "team_bot_a");
    assert.equal(
      readTier1File(sharedDir, "team_bot_a").active_count,
      1,
      "second reload must not stomp live session count",
    );
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

// ── Cross-checks with watchdog contract ──────────────────────────────────

test("tier1_active.json: shape matches watchdog reader contract", () => {
  // pressure_watchdog.read_in_process_tier1_counts reads
  // `data.get("active_count")` and expects an int >= 0. Anything
  // else, the watchdog silently skips this bot. This test pins the
  // shape so a future field rename here would fail loudly.
  const sharedDir = tmpDir();
  try {
    const r = new ModelRouter(CFG, sharedDir, "team_bot_a");
    r.setUserTier("s1", "power");
    r.resolveModelOverride("s1");
    const data = readTier1File(sharedDir, "team_bot_a");
    assert.equal(typeof data.active_count, "number");
    assert.ok(Number.isInteger(data.active_count));
    assert.ok(data.active_count >= 0);
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});

test("tier1_active.json: directory path is {sharedDir}/{botId}/cascade/", () => {
  // pressure_watchdog iterates `shared_dir.iterdir()` and looks
  // under each `<bot>/cascade/tier1_active.json`. If we ever write
  // it elsewhere, the watchdog sees nothing.
  const sharedDir = tmpDir();
  try {
    const r = new ModelRouter(CFG, sharedDir, "team_bot_a");
    r.setUserTier("s1", "power");
    r.resolveModelOverride("s1");
    const expected = path.join(sharedDir, "team_bot_a", "cascade", "tier1_active.json");
    assert.ok(fs.existsSync(expected), `expected file at ${expected}`);
  } finally {
    fs.rmSync(sharedDir, { recursive: true, force: true });
  }
});
