/**
 * Tests for _manifestStatusAllowsTriggers — the Layer C lifecycle gate.
 *
 * Background (base-spec §8.4 steps 3/4, docs/spec-manifest-v7-2026-05-20.md):
 *   The admin's pause/archive lifecycle path flips the manifest's
 *   ``status`` to paused/hidden and disables crons, but pre-fix the
 *   plugin's _getManifestTriggers compiled event_triggers[] from every
 *   plugin_intercept manifest regardless of status — a paused app's
 *   structural triggers stayed live and kept invoking its scripts.
 *
 *   Pause is reversible, so the uninstall-time unwire_event_triggers
 *   (destructive: clears the wiring) is the wrong tool; the plugin
 *   instead skips inactive-status manifests at trigger-compile time.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.manifestTriggerStatus.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { _manifestStatusAllowsTriggers } from "../dist/observer/TurnObserver.js";


// ── Inactive lifecycle states go dark ──────────────────────────────────────

for (const status of ["paused", "hidden", "dormant", "deprecated"]) {
  test(`status=${status} suppresses triggers`, () => {
    assert.equal(
      _manifestStatusAllowsTriggers({
        invocation_mode: "plugin_intercept",
        status,
      }),
      false,
      `${status} app must not intercept (§8.4 steps 3/4 symmetry)`,
    );
  });
}


// ── Live states keep intercepting ──────────────────────────────────────────

for (const status of ["active", "draft"]) {
  test(`status=${status} keeps triggers live`, () => {
    assert.equal(
      _manifestStatusAllowsTriggers({
        invocation_mode: "plugin_intercept",
        status,
      }),
      true,
    );
  });
}


// ── Fail-open shapes: pre-v7 / malformed manifests stay live ───────────────

test("missing status stays live (pre-v7 manifests)", () => {
  assert.equal(
    _manifestStatusAllowsTriggers({ invocation_mode: "plugin_intercept" }),
    true,
  );
});

test("non-string status stays live", () => {
  assert.equal(_manifestStatusAllowsTriggers({ status: 7 }), true);
  assert.equal(_manifestStatusAllowsTriggers({ status: null }), true);
  assert.equal(_manifestStatusAllowsTriggers({ status: ["paused"] }), true);
});

test("unknown future status stays live (fail-open)", () => {
  assert.equal(_manifestStatusAllowsTriggers({ status: "experimental" }), true);
});

test("null / undefined / non-object manifest stays live", () => {
  assert.equal(_manifestStatusAllowsTriggers(null), true);
  assert.equal(_manifestStatusAllowsTriggers(undefined), true);
  assert.equal(_manifestStatusAllowsTriggers("paused"), true);
});

test("status match is exact — no case folding (admin writes lowercase)", () => {
  // The admin lifecycle path writes machine values; "Paused" would be a
  // corrupt manifest, and corrupt shapes fail open like every other
  // unknown value.
  assert.equal(_manifestStatusAllowsTriggers({ status: "Paused" }), true);
});
