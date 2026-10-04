/**
 * Tests for the plugin's per-call price rule (ModelPricing, re-exported from
 * TurnObserver as estimateCost / priceCall).
 *
 * Why the shape changed (2026-09-04,
 * internal/finding-cost-forensics-power-bot-2026-09-04.md §1): the previous
 * table matched "opus" / "sonnet" / "haiku" as SUBSTRINGS, so a
 * current-generation id was priced at the previous generation's rates. On
 * the PoC bot for UTC 2026-09-04 that read $32.09 against a provider console
 * figure of $11.08. The rule is now catalog-by-exact-id → exact-id offline
 * table → honestly unpriced; a family fragment never prices anything.
 *
 * The earlier regression this file was written for (2026-08-31: xai/grok-4
 * priced $0 because the table held only the Anthropic families) is still
 * covered — by the catalog, which is where a live pod gets grok's price.
 *
 * Run from packages/plugin (after `npm run build`):
 *   node --test tests/turnObserver.estimateCost.test.mjs
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  estimateCost, priceCall, _resetPricingCatalogCache, _buildEvolveSubagentTurn,
} from "../dist/observer/TurnObserver.js";

/** A shared dir holding a model-pricing.json with the given rows.
 *  `rows` are [provider, model_id, inPerMTok, outPerMTok, cacheWritePerMTok?, cacheReadPerMTok?]. */
function sharedDirWithCatalog(rows, refreshedAt = "2026-09-04T00:00:00Z") {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-pricing-"));
  writeCatalog(dir, rows, refreshedAt);
  return dir;
}

function writeCatalog(dir, rows, refreshedAt = "2026-09-04T00:00:00Z") {
  const models = rows.map(([provider, model_id, i, o, cw, cr]) => ({
    provider,
    model_id,
    input_cost_per_token: i / 1e6,
    output_cost_per_token: o / 1e6,
    cache_write_cost_per_token: cw === undefined ? null : cw / 1e6,
    cache_read_cost_per_token: cr === undefined ? null : cr / 1e6,
  }));
  fs.writeFileSync(
    path.join(dir, "model-pricing.json"),
    JSON.stringify({ refreshed_at: refreshedAt, models }),
  );
  _resetPricingCatalogCache();
}

test("a current-generation id prices from its catalog row, tagged catalog", () => {
  // The exact defect: claude-opus-5 used to be priced at the previous
  // generation's $15/$75. Its own row says $5/$25.
  const dir = sharedDirWithCatalog([
    ["anthropic", "claude-opus-5", 5.0, 25.0, 6.25, 0.5],
  ]);
  const priced = priceCall("claude-opus-5", 1_000_000, 0, 0, 0, "anthropic", dir);
  assert.equal(priced.costSource, "catalog");
  assert.ok(Math.abs(priced.costUsd - 5.0) < 1e-9, `got ${priced.costUsd}`);
  // Cache rates come off the same row.
  const cached = priceCall("claude-opus-5", 0, 0, 0, 1_000_000, "anthropic", dir);
  assert.ok(Math.abs(cached.costUsd - 0.5) < 1e-9, `got ${cached.costUsd}`);
  // And the qualified spelling resolves to the same row.
  const qualified = priceCall(
    "anthropic/claude-opus-5", 1_000_000, 0, 0, 0, "anthropic", dir,
  );
  assert.equal(qualified.costUsd, priced.costUsd);
});

test("no catalog row and no exact table row ⇒ unpriced, never the family rate", () => {
  const dir = sharedDirWithCatalog([["anthropic", "claude-sonnet-4-6", 3.0, 15.0]]);
  const priced = priceCall("claude-opus-5", 1_000_000, 0, 0, 0, "anthropic", dir);
  assert.equal(priced.costSource, "unpriced");
  assert.equal(priced.costUsd, null);
  // The old substring table would have charged the opus family rate here.
  assert.notEqual(priced.costUsd, 15.0);
  // The numeric shim reports 0 = "could not price", which the in-process
  // breakers already treat as contributing nothing.
  assert.equal(estimateCost("claude-opus-5", 1_000_000, 0, 0, 0, "anthropic", dir), 0);
});

test("a gateway-recorded model resolves on its vendor row", () => {
  // provider is the gateway; the model id carries its native vendor. LiteLLM
  // skips re-host surfaces, so the vendor row is the only price there is.
  const dir = sharedDirWithCatalog([["anthropic", "claude-opus-5", 5.0, 25.0]]);
  const priced = priceCall(
    "anthropic/claude-opus-5", 1_000_000, 0, 0, 0, "openrouter", dir,
  );
  assert.equal(priced.costSource, "catalog");
  assert.ok(Math.abs(priced.costUsd - 5.0) < 1e-9, `got ${priced.costUsd}`);
});

test("xai grok-4 is priced from the catalog (the 2026-08-31 PoC-bot regression)", () => {
  const dir = sharedDirWithCatalog([["xai", "grok-4", 3.0, 15.0]]);
  // 50k input + 1k output at $3/$15 per MTok = 0.15 + 0.015
  const priced = priceCall("grok-4", 50_000, 1_000, 0, 0, "xai", dir);
  assert.equal(priced.costSource, "catalog");
  assert.ok(Math.abs(priced.costUsd - 0.165) < 1e-9, `got ${priced.costUsd}`);
});

test("the offline table is exact-id only, and covers a pod with no catalog", () => {
  _resetPricingCatalogCache();
  const table = priceCall("claude-sonnet-4-6", 1_000_000, 0, 0, 0, "anthropic", null);
  assert.equal(table.costSource, "table");
  assert.ok(Math.abs(table.costUsd - 3.0) < 1e-9, `got ${table.costUsd}`);
  // A bare exact id in the table still resolves without a provider.
  assert.equal(priceCall("gpt-4o-mini", 1_000_000, 0, 0, 0).costSource, "table");
  // A family fragment is not an id and prices nothing.
  for (const fragment of ["opus", "sonnet", "haiku", "claude-opus-5"]) {
    const p = priceCall(fragment, 1_000_000, 0, 0, 0, "anthropic", null);
    assert.equal(p.costSource, "unpriced", `${fragment} should not price`);
    assert.equal(p.costUsd, null);
  }
});

test("the catalog wins over the offline table for the same id", () => {
  const dir = sharedDirWithCatalog([["anthropic", "claude-sonnet-4-6", 1.0, 2.0]]);
  const priced = priceCall("claude-sonnet-4-6", 1_000_000, 0, 0, 0, "anthropic", dir);
  assert.equal(priced.costSource, "catalog");
  assert.ok(Math.abs(priced.costUsd - 1.0) < 1e-9, `got ${priced.costUsd}`);
});

test("a catalog row with no cache rates borrows the table's, not a zero", () => {
  const dir = sharedDirWithCatalog([["anthropic", "claude-sonnet-4-6", 3.0, 15.0]]);
  const priced = priceCall("claude-sonnet-4-6", 0, 0, 0, 1_000_000, "anthropic", dir);
  // Offline table's cache-read rate for this model is $0.30/MTok — a
  // published rate. A silent 0 here understates a cache-heavy turn ~10x.
  assert.ok(Math.abs(priced.costUsd - 0.30) < 1e-9, `got ${priced.costUsd}`);
});

test("a refreshed catalog is picked up without a restart", () => {
  const dir = sharedDirWithCatalog([["anthropic", "claude-opus-5", 5.0, 25.0]]);
  assert.ok(
    Math.abs(priceCall("claude-opus-5", 1_000_000, 0, 0, 0, "anthropic", dir).costUsd - 5.0) < 1e-9,
  );
  // Rewrite in place and age the mtime forward — the memo is keyed on it.
  const file = path.join(dir, "model-pricing.json");
  fs.writeFileSync(file, JSON.stringify({
    refreshed_at: "2026-09-11T00:00:00Z",
    models: [{
      provider: "anthropic", model_id: "claude-opus-5",
      input_cost_per_token: 7 / 1e6, output_cost_per_token: 25 / 1e6,
      cache_write_cost_per_token: null, cache_read_cost_per_token: null,
    }],
  }));
  const future = new Date(Date.now() + 60_000);
  fs.utimesSync(file, future, future);
  const after = priceCall("claude-opus-5", 1_000_000, 0, 0, 0, "anthropic", dir);
  assert.ok(Math.abs(after.costUsd - 7.0) < 1e-9, `got ${after.costUsd}`);
});

test("a missing or malformed catalog falls through, it does not throw", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "evolve-pricing-"));
  _resetPricingCatalogCache();
  assert.equal(
    priceCall("claude-sonnet-4-6", 1_000_000, 0, 0, 0, "anthropic", dir).costSource,
    "table",
  );
  fs.writeFileSync(path.join(dir, "model-pricing.json"), "{not json");
  _resetPricingCatalogCache();
  assert.equal(
    priceCall("claude-sonnet-4-6", 1_000_000, 0, 0, 0, "anthropic", dir).costSource,
    "table",
  );
});

test("_buildEvolveSubagentTurn carries the priced call and its source", () => {
  const dir = sharedDirWithCatalog([["xai", "grok-4", 3.0, 15.0]]);
  const built = _buildEvolveSubagentTurn("summarizer", {
    model: "grok-4",
    provider: "xai",
    usage: { input: 50_000, output: 1_000 },
  }, dir);
  assert.ok(built);
  assert.equal(built.priced.costSource, "catalog");
  assert.ok(
    built.costEstimated > 0.16 && built.costEstimated < 0.17,
    `cost ${built.costEstimated} out of expected grok range`,
  );
  // Unpriced subagent call: the record says so instead of reporting $0.
  const unpriced = _buildEvolveSubagentTurn("summarizer", {
    model: "mystery-model",
    provider: "mystery-cloud",
    usage: { input: 10_000, output: 500 },
  }, dir);
  assert.equal(unpriced.priced.costSource, "unpriced");
  assert.equal(unpriced.priced.costUsd, null);
  assert.equal(unpriced.costEstimated, 0);
});
