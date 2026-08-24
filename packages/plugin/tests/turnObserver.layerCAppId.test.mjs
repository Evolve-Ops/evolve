/**
 * AL-1.4b area 4c — the Layer C attribution id resolves ONE way.
 *
 * internal/build-AL-1.4-app-id-canonical.md §3 (the reader sweep) and §7 ("and
 * `TurnObserver.ts` is the per-turn hot path on every bot — behavior-
 * neutrality there must be demonstrated, not asserted").
 *
 * What changed. `_compileTrigger` hand-rolled
 *
 *     pkg_id || id || spec_id || "unknown"
 *
 * which is the canonical chain with `app_id` missing from the head,
 * `instance_id` missing from the tail, and no trim. That id is not a label:
 * it is handed to `AppAttribution.recordExplicit`, so it becomes the turn's
 * `app_id` annotation and, downstream, the per-app cost rollup key. The
 * registry on the other side of that comparison has always used `appIdOf`,
 * so the two halves of the attribution could disagree about the same
 * manifest — the D4 class this sweep closes.
 *
 * How neutrality is DEMONSTRATED rather than asserted. This file replays the
 * OLD chain, verbatim, over the whole shared D4 vector plus the manifest
 * shapes Layer C actually sees, and enumerates every case where the two
 * disagree. The delta is not claimed to be empty — it is listed, and each
 * entry is a case where the old chain was the one out of step.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.layerCAppId.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { _layerCAppId } from "../dist/observer/TurnObserver.js";
import { appIdOf } from "../dist/apps/appIdentity.js";

const fixturePath = join(
  dirname(fileURLToPath(import.meta.url)), "fixtures", "app-id-resolution.json",
);
const fixture = JSON.parse(readFileSync(fixturePath, "utf-8"));

/** The chain `_compileTrigger` carried before this sweep, character for character. */
function legacyLayerCChain(manifest) {
  return (typeof manifest?.pkg_id === "string" && manifest.pkg_id)
    || (typeof manifest?.id === "string" && manifest.id)
    || (typeof manifest?.spec_id === "string" && manifest.spec_id)
    || "unknown";
}


// ── The observer path now returns exactly what the ONE resolver returns ─────

test("fixture is well-formed and non-trivial", () => {
  assert.ok(Array.isArray(fixture.cases));
  assert.ok(fixture.cases.length >= 10, "vector must stay substantive");
});

for (const c of fixture.cases) {
  test(`_layerCAppId matches appIdOf: ${c.name}`, () => {
    // Not "matches a hardcoded string" — matches the resolver itself, on the
    // same vector that pins the resolver against its Python twin. If appIdOf
    // moves, the observer moves with it, by construction.
    assert.equal(_layerCAppId(c.manifest), appIdOf(c.manifest));
  });
}

for (const c of fixture.cases) {
  test(`_layerCAppId satisfies the D4 vector: ${c.name}`, () => {
    const expected = c.expected_id === null ? "unknown" : c.expected_id;
    assert.equal(_layerCAppId(c.manifest), expected);
  });
}


// ── The delta, enumerated rather than asserted away ─────────────────────────

/**
 * Exactly which fixture cases the old chain got wrong. Any case NOT listed
 * here is proven unchanged by the assertion below, so this list is the
 * complete blast radius of the sweep on the pinned vector.
 */
const KNOWN_DELTAS = new Map([
  // app_id was missing from the head of the old chain.
  ["conforming_app_id_still_beats_legacy", ["p-9bfa1c84", "task-manager"]],
  ["app_id_alone", ["unknown", "app-z"]],
  ["app_id_beats_pkg_id", ["app-a", "app-z"]],
  ["app_id_beats_all_four", ["app-a", "app-z"]],
  ["app_id_is_trimmed", ["unknown", "app-z"]],
  // instance_id was missing from the tail.
  ["instance_id_alone", ["unknown", "app-d"]],
  ["draft_id_does_not_beat_a_legacy_id", ["unknown", "app-d"]],
  ["nested_ids_are_ignored_top_level_only", ["unknown", "app-d"]],
  // The D4 trim divergence — and the sharper half of it. The old chain
  // tested TRUTHINESS, so a whitespace-only legacy id was both accepted and
  // returned verbatim: Layer C attributed those turns to "   ". The resolver
  // treats a whitespace-only value as absent and trims what it does return.
  ["legacy_id_is_trimmed", ["  app-a  ", "app-a"]],
  ["whitespace_pkg_id_falls_through_to_id", ["   ", "app-b"]],
]);

test("the enumerated deltas all name a real fixture case", () => {
  // Without this a typo'd or removed case name would sit in the list forever
  // looking like coverage. The staleness check below only fires for a listed
  // case that still exists and no longer changes.
  const names = new Set(fixture.cases.map((c) => c.name));
  const missing = [...KNOWN_DELTAS.keys()].filter((n) => !names.has(n));
  assert.deepEqual(missing, [], "delta list names a case the fixture does not have");
});

test("every behavior change is one of the enumerated deltas", () => {
  const surprises = [];
  for (const c of fixture.cases) {
    const before = legacyLayerCChain(c.manifest);
    const after = _layerCAppId(c.manifest);
    if (before === after) {
      assert.ok(
        !KNOWN_DELTAS.has(c.name),
        `${c.name} is listed as a delta but did not change — stale list`,
      );
      continue;
    }
    const expected = KNOWN_DELTAS.get(c.name);
    if (!expected) { surprises.push(`${c.name}: ${before} -> ${after}`); continue; }
    assert.deepEqual([before, after], expected, `${c.name} changed differently`);
  }
  assert.deepEqual(surprises, [], "unenumerated behavior change in the hot path");
});


// ── The shapes Layer C actually sees on a live pod ──────────────────────────

test("a gallery-installed manifest keeps answering to its package key", () => {
  // The common Layer C manifest: pkg_id + id both present. pkg_id led the old
  // chain and leads the canonical one, so the attribution key does NOT move
  // for the overwhelming majority of triggers on a live pod. This is the
  // neutrality that matters for the hot path.
  const m = { pkg_id: "p-9bfa1c84", id: "app-task-manager", app_id: "p-9bfa1c84" };
  assert.equal(legacyLayerCChain(m), "p-9bfa1c84");
  assert.equal(_layerCAppId(m), "p-9bfa1c84");
});

test("a scanner-minted v7-arc Instance stops attributing to \"unknown\"", () => {
  // The delta that is a straight fix: an Instance carries instance_id and
  // neither pkg_id nor id, so every Layer C invocation it made was attributed
  // to the literal string "unknown" — colliding with every other id-less
  // manifest on the same bot in the cost rollup.
  const m = { instance_id: "i-9f2c1a44", manifest_shape: "v7-arc" };
  assert.equal(legacyLayerCChain(m), "unknown");
  assert.equal(_layerCAppId(m), "i-9f2c1a44");
});

test("a discovered draft still refuses to attribute", () => {
  // Design §3: a draft's id is explicitly unstable and must never appear in
  // attribution. draft_id is not on the resolution chain, so a draft-only
  // manifest lands on the "unknown" sentinel exactly as before.
  const m = { draft_id: "draft-9f2c1a", manifest_shape: "v7-arc" };
  assert.equal(legacyLayerCChain(m), "unknown");
  assert.equal(_layerCAppId(m), "unknown");
});

test("the compiled-trigger id is the sentinel, never empty", () => {
  // recordExplicit drops a falsey id on the floor, so the "unknown" sentinel
  // (not appIdOf's Python twin's "") is what keeps an id-less Layer C
  // invocation visible as a conflict rather than silently unattributed.
  for (const m of [null, undefined, {}, [], 7, { meta: { pkg_id: "nested" } }]) {
    assert.equal(_layerCAppId(m), "unknown");
  }
});
