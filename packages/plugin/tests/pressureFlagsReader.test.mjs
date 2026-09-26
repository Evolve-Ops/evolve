/**
 * Tests for PressureFlagsReader — the bridge between the
 * pressure_watchdog daemon's JSON output and CascadeController.decide().
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/pressureFlagsReader.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { PressureFlagsReader } from "../dist/observer/PressureFlagsReader.js";

function tmpShared() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "pfr-test-"));
}

function _writeFlags(shared, payload) {
  const dir = path.join(shared, "cascade");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "pressure_flags.json"), JSON.stringify(payload));
}

const FRESH_HEARTBEAT = () => new Date().toISOString();
const STALE_HEARTBEAT = () => new Date(Date.now() - 300_000).toISOString();  // 5 min ago


// ── Missing file path ───────────────────────────────────────────────────────


test("file missing → returns null (brand-new pod / watchdog not installed)", () => {
  const shared = tmpShared();
  try {
    const r = new PressureFlagsReader(shared);
    assert.equal(r.read(), null);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("malformed JSON → returns null (fail open)", () => {
  const shared = tmpShared();
  try {
    fs.mkdirSync(path.join(shared, "cascade"), { recursive: true });
    fs.writeFileSync(path.join(shared, "cascade", "pressure_flags.json"), "{ not json");
    const r = new PressureFlagsReader(shared);
    assert.equal(r.read(), null);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


// ── Fresh heartbeat path ────────────────────────────────────────────────────


test("fresh heartbeat → returns flags as-is, no _watchdog_dead", () => {
  const shared = tmpShared();
  try {
    _writeFlags(shared, {
      pod_tier1_concurrency_cap: true,
      escalation_storm: false,
      tier1_pod_spend_burst: false,
      watchdog_heartbeat: FRESH_HEARTBEAT(),
      watchdog_ttl_seconds: 180,
    });
    const r = new PressureFlagsReader(shared);
    const flags = r.read();
    assert.equal(flags.pod_tier1_concurrency_cap, true);
    assert.notEqual(flags._watchdog_dead, true);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


// ── Stale heartbeat → _watchdog_dead ───────────────────────────────────────


test("stale heartbeat (>ttl_seconds) → _watchdog_dead: true", () => {
  // 5 min ago > 180s TTL → watchdog considered dead. Controller will
  // apply conservative defaults (block tier1 + hold tier).
  const shared = tmpShared();
  try {
    _writeFlags(shared, {
      pod_tier1_concurrency_cap: false,
      escalation_storm: false,
      tier1_pod_spend_burst: false,
      watchdog_heartbeat: STALE_HEARTBEAT(),
      watchdog_ttl_seconds: 180,
    });
    const r = new PressureFlagsReader(shared);
    const flags = r.read();
    assert.equal(flags._watchdog_dead, true);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("unparseable heartbeat string → _watchdog_dead: true (defensive)", () => {
  // A future watchdog version might emit a heartbeat in a new shape.
  // Better to treat as dead and stay conservative than to silently
  // ignore the field.
  const shared = tmpShared();
  try {
    _writeFlags(shared, {
      pod_tier1_concurrency_cap: false,
      watchdog_heartbeat: "not-a-date",
      watchdog_ttl_seconds: 180,
    });
    const r = new PressureFlagsReader(shared);
    const flags = r.read();
    assert.equal(flags._watchdog_dead, true);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


// ── Cache TTL ───────────────────────────────────────────────────────────────


test("cache returns same object within TTL (no re-read)", () => {
  // 30s TTL is well under the watchdog's 60s cadence. Reads within
  // the TTL window must hit the cache to avoid per-turn FS thrash on
  // busy bots.
  const shared = tmpShared();
  try {
    _writeFlags(shared, {
      pod_tier1_concurrency_cap: true,
      watchdog_heartbeat: FRESH_HEARTBEAT(),
      watchdog_ttl_seconds: 180,
    });
    const r = new PressureFlagsReader(shared);
    const t0 = Date.now();
    const a = r.read(t0);
    // Mutate the file — cache should hide the change.
    _writeFlags(shared, {
      pod_tier1_concurrency_cap: false,
      watchdog_heartbeat: FRESH_HEARTBEAT(),
      watchdog_ttl_seconds: 180,
    });
    const b = r.read(t0 + 5_000);  // 5s later, still within TTL
    assert.equal(a.pod_tier1_concurrency_cap, true);
    assert.equal(b.pod_tier1_concurrency_cap, true,
      "cache hit must return original (mutated file invisible)");
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


test("cache expires after TTL, re-reads file", () => {
  const shared = tmpShared();
  try {
    _writeFlags(shared, {
      pod_tier1_concurrency_cap: true,
      watchdog_heartbeat: FRESH_HEARTBEAT(),
      watchdog_ttl_seconds: 180,
    });
    const r = new PressureFlagsReader(shared);
    const t0 = Date.now();
    r.read(t0);
    // Mutate the file.
    _writeFlags(shared, {
      pod_tier1_concurrency_cap: false,
      watchdog_heartbeat: FRESH_HEARTBEAT(),
      watchdog_ttl_seconds: 180,
    });
    // Past the 30s TTL — cache should expire and re-read.
    const after = r.read(t0 + 31_000);
    assert.equal(after.pod_tier1_concurrency_cap, false,
      "post-TTL read must see updated file");
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});


// ── _invalidateCache test hook ──────────────────────────────────────────────


test("_invalidateCache forces re-read on next call", () => {
  const shared = tmpShared();
  try {
    _writeFlags(shared, {
      pod_tier1_concurrency_cap: true,
      watchdog_heartbeat: FRESH_HEARTBEAT(),
      watchdog_ttl_seconds: 180,
    });
    const r = new PressureFlagsReader(shared);
    r.read();
    _writeFlags(shared, {
      pod_tier1_concurrency_cap: false,
      watchdog_heartbeat: FRESH_HEARTBEAT(),
      watchdog_ttl_seconds: 180,
    });
    r._invalidateCache();
    const after = r.read();
    assert.equal(after.pod_tier1_concurrency_cap, false);
  } finally {
    fs.rmSync(shared, { recursive: true, force: true });
  }
});
